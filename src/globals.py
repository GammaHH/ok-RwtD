"""Shared startup hooks for both Qt and Web; no GUI toolkit dependency."""

from ok import Logger, og

logger = Logger.get_logger(__name__)


class Globals:

    def __init__(self, exit_event):
        from ok.core.events import communicate
        from src.rwtd.editor_lifecycle import install
        install(og.app, og.executor, communicate.starting_emulator.emit, logger)
