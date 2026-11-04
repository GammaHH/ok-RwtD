import ok
from src.config import config
from src.rwtd.discord_control import prepare_config, install
from pathlib import Path

if __name__ == '__main__':
    config = prepare_config(config)
    config['debug'] = True
    ok = ok.OK(config)
    try:
        install(ok,Path(__file__).resolve().parent)
    except Exception as exc:
        import logging
        logging.getLogger(__name__).warning('Discord control unavailable: %s',type(exc).__name__)
    ok.start()
