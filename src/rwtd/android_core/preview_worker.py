"""One bounded CPU-only preview worker. Never receives an Android backend or task."""
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace
import threading

from src.rwtd.android_core.feature_catalog import annotation_sources, feature_names
from src.rwtd.android_core.recognition import consolidate


class PreviewWorker:
    def __init__(self, root, observe, read_image, search_region,
                 geometry_provider=None, mapping_logger=None):
        self.root, self.observe = root, observe
        self.read_image, self.search_region = read_image, search_region
        self.geometry_provider, self.mapping_logger = geometry_provider, mapping_logger
        self.pool = ThreadPoolExecutor(max_workers=1, thread_name_prefix='RwtDPreview')
        self.stopped = threading.Event()
        self.images, self.stamps = {}, {}
        self.matcher = None

    def submit(self, frame, features):
        # Isolated immutable pixels; no executor.frame or live detection cache.
        pixels = frame.copy()
        pixels.flags.writeable = False
        return self.pool.submit(self.analyze, pixels, None if features is None else set(features))

    def analyze(self, frame, features, threshold=.5, coco_roi=None):
        if self.stopped.is_set():
            return None
        if self.matcher is None:
            from ok.feature.FeatureSet import FeatureSet
            self.matcher = FeatureSet(False, str(self.root/'ok_templates/coco_annotations.json'), 0, 0, .5)
        for path in list(self.images):
            stamp = (path.stat().st_mtime_ns,path.stat().st_size) if path.is_file() else None
            if stamp != self.stamps.get(path):
                self.images.pop(path,None)
                self.stamps.pop(path,None)
        requested = feature_names(self.root) if features is None else features
        geometry = self.geometry_provider() if self.geometry_provider is not None else None
        def match(feature_name, frame, template, box, threshold, limit):
            if self.stopped.is_set():
                raise RuntimeError('Preview cancelled')
            return self.matcher.find_feature(frame, feature_name, template=template,
                                             box=box, threshold=threshold, limit=limit)
        adapter = SimpleNamespace(_read_image=self.read_image, _search_region=self.search_region,
                                  find_feature=match)
        observations = []
        for path, coco, available in annotation_sources(self.root):
            if self.stopped.is_set():
                return None
            names = requested & available
            if names:
                options = {'template_root':path.parent,'multi_match':True}
                if coco_roi is not None:
                    options['coco_roi'] = coco_roi
                observations.extend(self.observe(adapter,self.root,frame,coco,names,threshold,
                    'Full frame',6,self.images,screen_geometry=geometry,
                    mapping_logger=self.mapping_logger,**options))
        if not requested.issubset({item['feature'] for item in observations}):
            raise ValueError('Preview feature missing annotation')
        if any(item.get('status') != 'SNAPSHOT_ONLY_NOT_EXECUTION_TARGET' for item in observations):
            from .recognition import incomplete_reason
            raise ValueError(incomplete_reason(observations,'Preview recognition incomplete'))
        for path in self.images:
            stat = path.stat()
            self.stamps[path] = (stat.st_mtime_ns,stat.st_size)
        return consolidate(observations)

    def close(self):
        self.stopped.set()
        self.pool.shutdown(wait=False, cancel_futures=True)
