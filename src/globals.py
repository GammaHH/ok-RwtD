"""Shared startup hooks for both Qt and Web; no GUI toolkit dependency."""

from ok import Logger, og

logger = Logger.get_logger(__name__)


class Globals:

    def __init__(self, exit_event):
        from ok.core.events import communicate
        from src.rwtd.editor_lifecycle import install
        install(og.app, og.executor, communicate.starting_emulator.emit, logger)
        from src.rwtd.device_connection import install as install_connection
        install_connection(og.device_manager, communicate.adb_devices.emit, logger)

    def on_show_main_window(self, window):
        from src.rwtd.native_window import install as install_native_window
        install_native_window(logger)
        from src.rwtd.native_controls import install as install_controls
        install_controls(window, og.app, og.executor, logger)
        from src.rwtd.theme import install_native_theme
        install_native_theme()
        from src.rwtd.native_pointer_diagnostics import install as install_pointer_diagnostics
        install_pointer_diagnostics(window, logger)
