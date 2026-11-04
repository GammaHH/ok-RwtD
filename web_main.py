"""Web profile uses the same RwtD configuration and startup services as Qt."""
import os
import runpy
from pathlib import Path

if __name__ == '__main__':
    os.environ['PYAPPIFY_APP_PROFILE']='web'
    runpy.run_path(str(Path(__file__).with_name('main.py')),run_name='__main__')
