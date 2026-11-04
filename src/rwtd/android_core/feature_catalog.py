"""Read annotation definitions without rewriting original assets or templates."""
import json
import os
from collections import OrderedDict
from copy import deepcopy
from threading import RLock
from pathlib import Path

_JSON_CACHE = OrderedDict()
_JSON_CACHE_LOCK = RLock()
_JSON_CACHE_BYTES = 32 * 1024 * 1024
_JSON_CACHE_FILES = 512


def _read_annotation(path, primary=False):
    """Metadata-checked JSON cache, including negative non-COCO discoveries.

    Stored COCO dictionaries are internal/read-only. Public callers get copies.
    No polling delay is introduced: every discovery checks the current stat.
    """
    path = Path(path).absolute()
    item = path.stat()
    signature = (item.st_dev, item.st_ino, item.st_size, item.st_mtime_ns, item.st_ctime_ns)
    key = (path, primary)
    with _JSON_CACHE_LOCK:
        cached = _JSON_CACHE.get(key)
        if cached and cached[0] == signature:
            _JSON_CACHE.move_to_end(key)
            return cached[1]
        try:
            data = json.loads(path.read_text(encoding='utf-8-sig'))
        except (ValueError, OSError):
            _JSON_CACHE.pop(key, None)
            raise
        if not primary and not (isinstance(data, dict) and all(isinstance(data.get(k), list) for k in ('categories', 'images', 'annotations'))):
            data = None
        # Non-COCO definitions are a negative discovery only, never retained.
        cost = item.st_size if data is not None else 0
        if cost <= _JSON_CACHE_BYTES:
            _JSON_CACHE[key] = (signature, data, cost)
            _JSON_CACHE.move_to_end(key)
        else:
            _JSON_CACHE.pop(key, None)
        while (len(_JSON_CACHE) > _JSON_CACHE_FILES or
               sum(value[2] for value in _JSON_CACHE.values()) > _JSON_CACHE_BYTES):
            _JSON_CACHE.popitem(last=False)
        return data

# Compatibility export: category names do not establish prohibited actions.
FORBIDDEN_FEATURES = set()


def _annotation_sources(root):
    root = Path(root)
    primary = root / 'ok_templates/coco_annotations.json'
    discovered = {primary.absolute()} if primary.is_file() else set()
    paths = [primary] if primary.is_file() else []
    assets = root / 'assets'
    if assets.is_dir():
        paths_in_assets = []
        def linked(path):
            return path.is_symlink() or bool(getattr(path.lstat(), 'st_file_attributes', 0) & 0x400)
        for folder, directories, files in os.walk(assets, followlinks=False):
            directories[:] = [name for name in directories if name not in {'backups', 'runtime'} and not linked(Path(folder) / name)]
            paths_in_assets.extend(Path(folder) / name for name in files if name.endswith('.json'))
        for path in sorted(paths_in_assets):
            relative = path.relative_to(assets)
            if any(part in {'backups', 'runtime'} for part in relative.parts):
                continue
            if linked(path):
                continue
            discovered.add(path.absolute())
            try:
                data = _read_annotation(path)
            except (ValueError, OSError):
                if 'coco' in path.name.lower() and 'annotation' in path.name.lower():
                    raise ValueError('Invalid COCO annotation file: ' + str(relative))
                continue
            if isinstance(data, dict) and all(isinstance(data.get(key), list) for key in ('categories', 'images', 'annotations')):
                paths.append(path)
    absolute_root = root.absolute()
    with _JSON_CACHE_LOCK:
        for key in list(_JSON_CACHE):
            if key[0].is_relative_to(absolute_root) and key[0] not in discovered:
                _JSON_CACHE.pop(key, None)
    sources, claimed = [], set()
    for path in paths:
        data = _read_annotation(path, primary=path == primary)
        names = {item['name'] for item in data['categories']}
        if any(not isinstance(name, str) or not name.strip() for name in names):
            raise ValueError('Invalid COCO feature name')
        # Existing primary definitions win. Newly named asset features are added.
        available = names - claimed
        claimed.update(names)
        sources.append((path, data, available))
    return sources


def annotation_sources(root):
    return deepcopy(_annotation_sources(root))


def feature_names(root):
    return {name for _, _, names in _annotation_sources(root) for name in names}


def reference_png(root, feature):
    """Read an annotated native original as an ROI background, without a device."""
    from io import BytesIO
    from PIL import Image
    if not isinstance(feature, str) or not 1 <= len(feature) <= 100:
        return None
    for annotation_path, data, available in annotation_sources(root):
        if feature not in available:
            continue
        categories = {row['id'] for row in data['categories'] if row['name'] == feature}
        image_ids = {row['image_id'] for row in data['annotations'] if row['category_id'] in categories}
        folder = annotation_path.parent.resolve()
        for meta in data['images']:
            if meta['id'] not in image_ids:
                continue
            candidate = (folder / meta['file_name']).resolve()
            if not candidate.is_relative_to(folder):
                continue
            if not candidate.is_file():
                candidate = (folder / Path(meta['file_name']).name).resolve()
            if not candidate.is_relative_to(folder) or not candidate.is_file() or candidate.stat().st_size > 8*1024**2:
                continue
            with Image.open(candidate) as image:
                if image.size != (1080, 2400):
                    continue
                buffer = BytesIO()
                image.convert('RGB').save(buffer, format='PNG')
                return buffer.getvalue()
    return None
