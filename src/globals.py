from PySide6.QtCore import QObject

from ok import Logger, og

logger = Logger.get_logger(__name__)


class Globals(QObject):

    def __init__(self, exit_event):
        super().__init__()
        from ok.core.events import communicate
        from src.rwtd.editor_lifecycle import install
        install(og.app, og.executor, communicate.starting_emulator.emit, logger)
