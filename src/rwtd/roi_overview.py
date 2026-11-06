"""ROI task model: authenticated UI queues work to the native one-time task."""
import base64
import copy
import hashlib
import json
import queue
import threading
from pathlib import Path

from src.rwtd.android_core.feature_catalog import annotation_sources
from src.rwtd.android_core import feature_settings as settings
from src.rwtd.android_core.module_store import ModuleStore, safe_path
from src.rwtd.android_core.decision_graph import DecisionGraphStore
from src.rwtd.profile import rwtd_profile
from src.rwtd.manual_device_tests import validate_parameters


def digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


class RoiOverview:
    def __init__(self, root):
        self.root = Path(root)
        self.lock = threading.RLock()
        self.closing = threading.Event()
        self.jobs = queue.Queue(maxsize=8)
        self.receipts = {}
        self.images = {}
        self.frame = None
        self.frame_geometry = None
        self.selected = None
        self.catalog = []
        self.state = {'status': 'READY', 'message': '', 'evaluation': None}
        self.modules = ModuleStore(rwtd_profile(root, validate_parameters))
        self.graphs = DecisionGraphStore(self.modules.profile, self.modules)
        for path, coco, available in annotation_sources(root):
            categories = {item['id']: item['name'] for item in coco['categories']}
            images = {item['id']: item for item in coco['images']}
            for annotation in coco['annotations']:
                feature = categories[annotation['category_id']]
                if feature not in available:
                    continue
                meta = images[annotation['image_id']]
                source = safe_path(root, str((path.parent / meta['file_name']).relative_to(self.root)))
                if not source.is_file():
                    source = safe_path(root, str((path.parent / Path(meta['file_name']).name).relative_to(self.root)))
                self.catalog.append({'feature': feature, 'annotation_id': annotation['id'],
                    'source': str(source.relative_to(self.root)), 'bbox': annotation['bbox'],
                    'size': [meta['width'], meta['height']]})
        self.state['catalog'] = self.catalog
        self.refresh()

    def refresh(self):
        shared = settings.load(self.root)
        self.state.update(shared=shared['features'], revision=settings.revision(shared), overrides=self.overrides())

    def overrides(self):
        output = []
        definitions = self.modules.profile.definitions_root
        locations = list(definitions.glob('assets/rwtd/modules/system/*.json')) + list(definitions.glob('assets/rwtd/modules/user/*.json'))
        canvas_names = {}
        for entry in self.graphs.catalog():
            store = DecisionGraphStore(self.modules.profile, self.modules, entry['id'])
            path = store.path('decision.json')
            if path.is_file():
                locations.append(path)
                canvas_names[path] = entry['name']
        for path in locations:
            path = safe_path(definitions, path.relative_to(definitions))
            data = json.loads(path.read_text(encoding='utf-8-sig'))
            features = sorted({item['feature'] for item in self.catalog})
            stamp = digest(path)
            owner = canvas_names.get(path, data.get('name', 'Canvas'))

            def walk(value, pointer=(), hints=()):
                if isinstance(value, dict):
                    local = [value[key] for key in ('feature', 'ocr_feature') if isinstance(value.get(key), str)]
                    if isinstance(value.get('features'), list):
                        local.extend(value['features'])
                    hints = tuple(name for name in local if name in features) or hints
                    for key, child in value.items():
                        if key in {'roi', 'coco_roi', 'global_roi'} and isinstance(child, list) and len(child) == 4:
                            # Fixed OCR regions aren't COCO search overrides.
                            if value.get('kind') == 'ocr' and value.get('ocr_region') != 'coco':
                                continue
                            output.append({'source': str(path.relative_to(definitions)), 'pointer': list(pointer+(key,)),
                                'roi': child, 'features': list(hints), 'stamp': stamp,
                                'system': '/system/' in path.as_posix(),
                                'owner': owner + ' / ' + '/'.join(map(str, pointer+(key,)))})
                        elif key in {'feature_rois', 'coco_feature_rois'} and isinstance(child, dict):
                            for feature, roi in child.items():
                                output.append({'source': str(path.relative_to(definitions)), 'pointer': list(pointer+(key,feature)),
                                    'roi': roi, 'features': [feature], 'stamp': stamp, 'system': '/system/' in path.as_posix(),
                                    'owner': owner + ' / ' + '/'.join(map(str, pointer+(key,feature)))})
                        else:
                            walk(child, pointer+(key,), hints)
                elif isinstance(value, list):
                    for index, child in enumerate(value):
                        walk(child, pointer+(index,), hints)
            walk(data)
        return output

    def snapshot(self):
        with self.lock:
            return copy.deepcopy(dict(self.state, receipts=self.receipts, closing=self.closing.is_set()))

    def image(self, which):
        with self.lock:
            return self.images.get(which)

    def _log(self, *args):
        pass

    def enqueue(self, request_id, command, payload):
        if not isinstance(request_id, str) or not 1 <= len(request_id) <= 100 or not isinstance(payload, dict):
            raise ValueError('Invalid request')
        if command not in {'select', 'upload', 'preview', 'save', 'save_override', 'close', 'refresh'}:
            raise ValueError('Unknown ROI command')
        with self.lock:
            if self.closing.is_set():
                raise ValueError('ROI task is closing')
            if request_id in self.receipts:
                return self.receipts[request_id]
            if command == 'close':
                self.closing.set()
                return {'status': 'CLOSING'}
            try:
                self.jobs.put_nowait((request_id, command, payload))
            except queue.Full:
                raise ValueError('ROI task busy; wait for current operation') from None
            self.receipts[request_id] = {'status': 'QUEUED'}
            return self.receipts[request_id]

    @staticmethod
    def read_image(path):
        import cv2
        import numpy as np
        data = np.fromfile(path, dtype=np.uint8)
        if data.size > 8*1024**2:
            raise ValueError('Image exceeds 8 MiB')
        RoiOverview.check_dimensions(data.tobytes())
        image = cv2.imdecode(data, cv2.IMREAD_COLOR)
        if image is None or image.shape[:2] != (2400, 1080):
            raise ValueError('Use a native 1080x2400 image; no implicit coordinate scaling')
        return image

    @staticmethod
    def check_dimensions(raw, *, allow_physical=False):
        from io import BytesIO
        from PIL import Image
        # Check the image header before asking the codec to allocate pixels.
        with Image.open(BytesIO(raw)) as image:
            if allow_physical:
                from src.rwtd.android_core.screen_geometry import ScreenGeometry
                if image.width * image.height > 12_000_000:
                    raise ValueError('Evaluation image exceeds 12 megapixels')
                ScreenGeometry(*image.size)
            elif image.size != (1080, 2400):
                raise ValueError('Use a native 1080x2400 image')

    def publish(self, name, pixels):
        import cv2
        okay, data = cv2.imencode('.png', pixels)
        if not okay:
            raise ValueError('Cannot encode preview')
        self.images[name] = data.tobytes()

    def template(self):
        if self.selected is None:
            raise ValueError('Select a COCO annotation first')
        entry = self.catalog[self.selected]
        image = self.read_image(safe_path(self.root, entry['source']))
        x, y, w, h = map(lambda value: int(round(value)), entry['bbox'])
        from src.rwtd.android_core.workflow_model import validate_roi
        validate_roi([x,y,w,h])
        return entry, image[y:y+h, x:x+w].copy()

    def process_next(self):
        try:
            request_id, command, payload = self.jobs.get_nowait()
        except queue.Empty:
            return
        with self.lock:
            try:
                if self.closing.is_set():
                    raise ValueError('Cancelled before processing')
                result = self.command(command, payload)
                self.receipts[request_id] = {'status': 'SUCCESS', 'result': result}
                self.state.update(status='READY', message='完成：'+command)
            except (ValueError, OSError, TypeError, KeyError, IndexError) as exc:
                self.receipts[request_id] = {'status': 'FAILED', 'reason': str(exc)}
                self.state.update(status='FAILED', message=str(exc))
            except Exception as exc:
                # Failed image codecs must not terminate the server or leak a task.
                self.receipts[request_id] = {'status': 'FAILED', 'reason': type(exc).__name__}
                self.state.update(status='FAILED', message=type(exc).__name__)
            while len(self.receipts) > 20:
                first = next(iter(self.receipts))
                if self.receipts[first]['status'] == 'QUEUED':
                    break
                self.receipts.pop(first)

    def command(self, command, payload):
        if command == 'refresh':
            self.refresh()
            return {'revision':self.state['revision']}
        if command == 'select':
            index = payload['index']
            if type(index) is not int or not 0 <= index < len(self.catalog):
                raise ValueError('Unknown annotation')
            self.selected = index
            self.frame = None
            self.images.clear()
            self.state['evaluation'] = None
            entry, template = self.template()
            self.frame = self.read_image(safe_path(self.root, entry['source']))
            from src.rwtd.android_core.screen_geometry import ScreenGeometry
            self.frame_geometry = ScreenGeometry(1080, 2400)
            self.publish('source', self.frame)
            self.publish('template', template)
            self.images.pop('processed', None)
            self.state.update(selected=index, image_version=self.state.get('image_version',0)+1, evaluation=None)
            return entry
        if command == 'upload':
            import cv2
            import numpy as np
            raw = base64.b64decode(payload['base64'], validate=True)
            if len(raw) > 8*1024**2:
                raise ValueError('Image exceeds 8 MiB')
            self.check_dimensions(raw, allow_physical=True)
            frame = cv2.imdecode(np.frombuffer(raw, dtype=np.uint8), cv2.IMREAD_COLOR)
            if frame is None:
                raise ValueError('Cannot decode evaluation image')
            from src.rwtd.android_core.screen_geometry import ScreenGeometry
            self.frame_geometry = ScreenGeometry(frame.shape[1], frame.shape[0])
            self.frame = self.frame_geometry.normalize(frame)
            self.publish('source', self.frame)
            self.images.pop('processed', None)
            self.state.update(image_version=self.state.get('image_version',0)+1, evaluation=None)
            return {'imported': True}
        if command in {'preview', 'save'}:
            entry, template = self.template()
            options = settings.validate(payload['options'])
            if command == 'save':
                result = settings.save(self.root, entry['feature'], options, payload['revision'])
                self.refresh()
                return {'revision': result}
            threshold = payload.get('threshold', .8)
            self.state['evaluation'] = None
            if type(threshold) not in (int,float) or not 0 <= threshold <= 1:
                raise ValueError('Threshold must be 0..1')
            if self.frame is None:
                raise ValueError('Select or import an image')
            from src.rwtd.android_core.roi_policy import effective_roi, new_resolution
            geometry = self.frame_geometry
            expanded = new_resolution(geometry)
            search_roi = effective_roi(options['roi'], 'coco', enabled=expanded)
            template,_=settings.template_for_geometry(template,
                [int(round(value)) for value in entry['bbox']],geometry,options)
            boxes, best = settings.match(self.frame, template, search_roi, threshold, options)
            self.publish('processed', settings.preprocess(self.frame, options))
            self.publish('template', settings.preprocess(template, options))
            self.state.update(image_version=self.state.get('image_version',0)+1,
                evaluation={'confidence':best, 'threshold':threshold, 'matched': bool(boxes),
                    'boxes': [[box.x,box.y,box.width,box.height] for box in boxes], 'options': options,
                    'configured_roi': options['roi'], 'effective_roi': search_roi,
                    'roi_margin_px': 10 if expanded else 0,
                    'physical_size': [geometry.width, geometry.height] if geometry else [1080,2400]})
            return self.state['evaluation']
        if command == 'save_override':
            source, pointer = payload['source'], payload['pointer']
            row = next((row for row in self.overrides() if row['source']==source and row['pointer']==pointer), None)
            if row is None or row['stamp'] != payload['stamp']:
                raise ValueError('Override changed; reload before saving')
            from src.rwtd.android_core.workflow_model import validate_roi
            roi = validate_roi(payload['roi'])
            path = safe_path(self.modules.profile.definitions_root, source)
            data = json.loads(path.read_text(encoding='utf-8-sig'))
            parent = data
            for key in pointer[:-1]:
                parent = parent[key]
            parent[pointer[-1]] = roi
            canvas = next((entry for entry in self.graphs.catalog()
                if DecisionGraphStore(self.modules.profile, self.modules, entry['id']).path('decision.json') == path), None)
            if canvas is not None:
                if digest(path) != payload['stamp']:
                    raise ValueError('Override changed; reload before saving')
                # A fixed file store saves the original Canvas without switching the active one.
                store = DecisionGraphStore(self.modules.profile, self.modules, canvas['id'])
                result = store.save(data, data.get('revision'))
            else:
                current = self.modules.get(data['id'])
                if digest(path) != payload['stamp']:
                    raise ValueError('Override changed; reload before saving')
                result = self.modules.save(data, current['revision'], allow_system_update=payload.get('allow_system_update',False))
            self.refresh()
            return {'revision': result['revision']}
        raise ValueError('Unknown ROI command')
