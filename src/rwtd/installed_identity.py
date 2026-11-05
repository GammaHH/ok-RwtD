"""Resolve display identity when a PyAppify shortcut omits launcher variables."""
import json
import re
from pathlib import Path


def resolve_identity(project_root, environ):
    version = environ.get('PYAPPIFY_APP_VERSION', '')
    profile = environ.get('PYAPPIFY_APP_PROFILE', '')
    root = Path(project_root).resolve()
    # Never infer a release version from an ordinary development checkout.
    if root.name == 'working' and (root.parent / 'python' / 'python.exe').is_file():
        try:
            metadata = json.loads((root.parent / 'app.json').read_text(encoding='utf-8-sig'))
            if isinstance(metadata, dict) and metadata.get('name') == 'ok-RwtD' and metadata.get('installed') is True:
                candidate = metadata.get('current_version', '')
                if isinstance(candidate, str) and re.fullmatch(
                    r'v?\d+\.\d+\.\d+(?:-[0-9A-Za-z.-]+)?', candidate
                ):
                    version = version or candidate
                    selected = metadata.get('current_profile')
                    profiles = metadata.get('profiles', [])
                    names = {row.get('name') for row in profiles if isinstance(row, dict)
                             and isinstance(row.get('name'), str)} if isinstance(profiles, list) else set()
                    if isinstance(selected, str) and selected in names:
                        profile = profile or selected
        except (OSError, ValueError):
            pass
    return version or 'dev', profile
