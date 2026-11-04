"""Shared read-only template matching for Dashboard and FARM; no task registration."""
from pathlib import Path
import cv2
import numpy as np
from ok.feature.Box import Box


class PreviewMatcher:
    @staticmethod
    def _read_image(path: Path) -> np.ndarray:
        image = cv2.imdecode(np.fromfile(str(path), dtype=np.uint8), cv2.IMREAD_COLOR)
        if image is None:
            raise ValueError(f"Cannot read screenshot or source image: {path}")
        return image

    @staticmethod
    def _search_region(x, y, w, h, frame_w, frame_h, padding_percent):
        pad_x = round(frame_w * padding_percent / 100)
        pad_y = round(frame_h * padding_percent / 100)
        x1, y1 = max(0, x - pad_x), max(0, y - pad_y)
        x2, y2 = min(frame_w, x + w + pad_x), min(frame_h, y + h + pad_y)
        if x2 - x1 < w or y2 - y1 < h:
            return None
        return Box(x1, y1, x2 - x1, y2 - y1, name="coco_search")

    def _observe(self, root, frame, coco, requested, threshold, search_scope, padding, image_cache,
                 coco_roi=None, template_root=None, multi_match=False, screen_geometry=None,
                 mapping_logger=None):
        from src.rwtd.android_core.feature_settings import load as load_feature_settings, match as processed_match
        shared_settings = load_feature_settings(root)['features']
        frame_h, frame_w = frame.shape[:2]
        categories = {x["id"]: x["name"] for x in coco["categories"]}
        images = {x["id"]: x for x in coco["images"]}
        observations = []
        template_root = (template_root or root / "ok_templates").resolve()
        for ann in coco["annotations"]:
            feature = categories[ann["category_id"]]
            if feature not in requested:
                continue
            meta = images[ann["image_id"]]
            src_path = (template_root / meta["file_name"]).resolve()
            if not src_path.is_relative_to(template_root):
                raise ValueError("COCO image reference escaped template folder")
            if src_path not in image_cache:
                if not src_path.is_file():
                    # Some exports use images/0.png while project keeps 0.png.
                    fallback = (template_root / Path(meta["file_name"]).name).resolve()
                    if not fallback.is_file():
                        observations.append({"feature": feature, "annotation_id": ann["id"],
                                             "matched": False, "status": "SOURCE_IMAGE_MISSING",
                                             "source": str(src_path)})
                        continue
                    src_path = fallback
                image_cache[src_path] = self._read_image(src_path)
            if src_path not in image_cache:
                # No image when both paths were absent.
                continue
            source = image_cache[src_path]
            if (source.shape[1], source.shape[0]) != (int(meta["width"]), int(meta["height"])):
                observations.append({"feature": feature, "annotation_id": ann["id"],
                                     "matched": False, "status": "COCO_SOURCE_SIZE_MISMATCH",
                                     "actual": [source.shape[1], source.shape[0]],
                                     "declared": [meta["width"], meta["height"]]})
                continue
            x, y, w, h = map(lambda v: int(round(v)), ann["bbox"])
            if w <= 0 or h <= 0 or x < 0 or y < 0 or x + w > source.shape[1] or y + h > source.shape[0]:
                observations.append({"feature": feature, "annotation_id": ann["id"],
                                     "matched": False, "status": "INVALID_BBOX"})
                continue
            source_bbox=[x,y,w,h]
            processing = shared_settings.get(feature, {'roi': None, 'grayscale': False, 'scale': 1})
            try:
                from src.rwtd.android_core.template_source import extract
                template,reference_bbox,native_source=extract(source,source_bbox,[frame_w,frame_h],
                    warp=processing.get('warp_template',True))
                x,y,w,h=reference_bbox
            except ValueError as exc:
                observations.append({'feature':feature,'annotation_id':ann['id'],'matched':False,
                    'status':'FRAME_SIZE_MISMATCH','actual':[frame_w,frame_h],
                    'required':[source.shape[1],source.shape[0]],'error':str(exc)})
                continue
            mapping = None
            roi = None
            processing = shared_settings.get(feature, {'roi': None, 'grayscale': False, 'scale': 1})
            if processing.get('roi') is not None:
                rx, ry, rw, rh = processing['roi']
                roi = Box(rx, ry, rw, rh)
            if search_scope == "COCO ROI":
                roi = self._search_region(x, y, w, h, frame_w, frame_h, padding)
            if coco_roi is not None:
                rx, ry, rw, rh = coco_roi
                if rx < 0 or ry < 0 or rw <= 0 or rh <= 0 or rx+rw > frame_w or ry+rh > frame_h:
                    raise ValueError("COCO search ROI outside native frame")
                roi = Box(rx, ry, rw, rh)
            if search_scope == "COCO ROI" and roi is None:
                observations.append({"feature": feature, "annotation_id": ann["id"],
                                     "matched": False, "status": "ROI_TOO_SMALL"})
                continue
            # Select exactly one original ROI above, then expand once at the
            # recognition boundary. Both PreviewWorker and live input use it.
            from src.rwtd.android_core.roi_policy import effective_roi, new_resolution
            configured_roi = [roi.x, roi.y, roi.width, roi.height] if roi else None
            search_roi = effective_roi(configured_roi, 'coco', enabled=new_resolution(screen_geometry))
            roi = Box(*search_roi) if search_roi is not None else None
            best_confidence = None
            try:
                from src.rwtd.android_core.feature_settings import template_for_geometry
                template,mapping=template_for_geometry(template,[x,y,w,h],
                    None if native_source else screen_geometry,processing)
                if feature in shared_settings:
                    boxes, best_confidence = processed_match(frame, template,
                        [roi.x, roi.y, roi.width, roi.height] if roi else None,
                        threshold, processing, limit=100 if multi_match else 1)
                elif multi_match:
                    boxes = self.find_feature(feature_name=feature, frame=frame, template=template,
                                              box=roi, threshold=threshold, limit=100)
                else:
                    box = self.find_one(feature_name=feature, frame=frame,
                                        template=template, box=roi, threshold=threshold)
                    boxes = [box] if box is not None else []
                if best_confidence is None and boxes:
                    best_confidence = max(float(box.confidence) for box in boxes)
            except Exception as exc:
                if mapping_logger is not None:
                    try:
                        mapping_logger(feature=feature, annotation_id=ann['id'],
                                       reference_bbox=[x, y, w, h], mapping=mapping,
                                       roi=[roi.x, roi.y, roi.width, roi.height] if roi else None,
                                       processing=processing,
                                       threshold=threshold, best_confidence=None,
                                       error=f'{type(exc).__name__}: {exc}')
                    except Exception:
                        pass
                observations.append({"feature": feature, "annotation_id": ann["id"],
                                     "matched": False, "status": "MATCH_ERROR",
                                     "error": f"{type(exc).__name__}: {exc}"})
                continue
            if mapping_logger is not None:
                try:
                    mapping_logger(feature=feature, annotation_id=ann['id'],
                                   reference_bbox=[x, y, w, h], mapping=mapping,
                                   roi=[roi.x, roi.y, roi.width, roi.height] if roi else None,
                                   processing=processing,
                                   threshold=threshold, best_confidence=best_confidence,
                                   matched=bool(boxes))
                except Exception:
                    pass
            for box in boxes or [None]:
                observations.append({
                "feature": feature,
                "annotation_id": ann["id"],
                "reference_bbox": [x, y, w, h],
                "source_bbox":source_bbox,
                "source_size":[source.shape[1],source.shape[0]],
                "native_source_normalized":native_source,
                "matched": box is not None,
                "best_confidence": best_confidence,
                "bbox": [int(box.x), int(box.y), int(box.width), int(box.height)] if box is not None else None,
                "confidence": float(box.confidence) if box is not None else None,
                "match_offset_px": [int(box.x) - x, int(box.y) - y] if box is not None else None,
                "search_bbox": [roi.x, roi.y, roi.width, roi.height] if roi else None,
                "configured_search_bbox": configured_roi,
                "roi_margin_px": 10 if new_resolution(screen_geometry) else 0,
                "status": "SNAPSHOT_ONLY_NOT_EXECUTION_TARGET",
            })
        return observations

