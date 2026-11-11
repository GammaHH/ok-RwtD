"""Explicit Qt command slots shared by the header button and F9 handler."""


class NativeControls:
    def __init__(self, executor, controller, logger):
        self.executor = executor
        self.controller = controller
        self.logger = logger

    def toggle(self):
        paused = self.executor.paused
        self.logger.info(f'RWTD_NATIVE_CONTROL action=toggle paused={paused}')
        if paused:
            from .editor_lifecycle import is_offline_editor
            current = getattr(self.executor, 'current_task', None)
            if (is_offline_editor(current) and getattr(current, 'enabled', False)
                    or any(is_offline_editor(t) and getattr(t, 'enabled', False)
                           for t in getattr(self.executor, 'onetime_tasks', ()))):
                return self.executor.start()
            return self.controller.start()
        return self.executor.pause()

    def task_start(self, callback, task):
        self.logger.info(f'RWTD_NATIVE_CONTROL action=task_start task={type(task).__name__}')
        return callback()


def replace_callback(signal, previous, replacement, logger):
    """Replace our lifecycle callback without removing other UI subscribers."""
    if not callable(getattr(signal, 'disconnect', None)) or not callable(getattr(signal, 'connect', None)):
        logger.warning('RWTD_NATIVE_CONTROL_BIND_SKIPPED unsupported native signal')
        return False
    try:
        disconnected = signal.disconnect(previous)
        if disconnected is False:
            logger.warning('RWTD_NATIVE_CONTROL_BIND_SKIPPED previous callback was not disconnected')
            return False
    except (RuntimeError, TypeError) as exc:
        # A framework with different bindings must keep its original handler;
        # adding a second one here could toggle start then pause in one click.
        logger.warning(f'RWTD_NATIVE_CONTROL_BIND_SKIPPED {type(exc).__name__}: {exc}')
        return False
    try:
        connected = signal.connect(replacement)
        if connected is False:
            raise RuntimeError('replacement callback was not connected')
    except (RuntimeError, TypeError) as exc:
        try:
            signal.connect(previous)
        except (RuntimeError, TypeError) as restore_error:
            logger.warning(f'RWTD_NATIVE_CONTROL_BIND_RESTORE_FAILED {type(restore_error).__name__}: {restore_error}')
            return False
        logger.warning(f'RWTD_NATIVE_CONTROL_BIND_RESTORED {type(exc).__name__}: {exc}')
        return False
    return True


def install(window, app, executor, logger):
    """Keep native lifecycle and confirmation dialogs; bind concrete Qt slots.

    The framework hotkey polls on a Handler thread. Marshal it to the same Qt
    receiver as mouse clicks, instead of executing UI-affecting callbacks there.
    """
    if getattr(window, '_rwtd_native_controls', None) is not None:
        return True
    card = getattr(getattr(window, 'start_tab', None), 'start_card', None)
    button = getattr(card, 'start_button', None)
    if (card is None or button is None or not callable(getattr(card, 'clicked', None))
            or not callable(getattr(card, 'update_status', None))
            or not callable(getattr(window, 'findChildren', None))
            or getattr(app, 'start_controller', None) is None):
        logger.warning('RWTD_NATIVE_CONTROLS_SKIPPED unsupported native window')
        return False
    try:
        from PySide6.QtCore import QObject, Signal, Slot
        from PySide6.QtWidgets import QWidget
        from ok.core.events import communicate
    except ImportError as exc:
        logger.warning(f'RWTD_NATIVE_CONTROLS_SKIPPED {type(exc).__name__}: {exc}')
        return False

    actions = NativeControls(executor, app.start_controller, logger)

    class Receiver(QObject):
        toggle_requested = Signal()

        def __init__(self):
            super().__init__(window)
            self.toggle_requested.connect(self.toggle)

        def failed(self, exc):
            logger.warning(f'RWTD_NATIVE_CONTROL_FAILED {type(exc).__name__}: {exc}')
            try:
                communicate.notification.emit('操作失敗：' + str(exc)[:240], 'RwtD 控制', True, False)
            except Exception as notify_error:
                logger.warning(f'RWTD_NATIVE_CONTROL_NOTIFICATION_FAILED {type(notify_error).__name__}: {notify_error}')

        @Slot()
        def toggle(self):
            try:
                actions.toggle()
                card.update_status()
            except Exception as exc:
                self.failed(exc)


    class TaskReceiver(QObject):
        def __init__(self, widget, callback):
            super().__init__(receiver)
            self.widget = widget
            self.callback = callback

        @Slot()
        def start(self):
            try:
                actions.task_start(self.callback, self.widget.task)
            except Exception as exc:
                receiver.failed(exc)

    if not isinstance(window, QObject):
        logger.warning('RWTD_NATIVE_CONTROLS_SKIPPED native window is not a QObject')
        return False
    try:
        widgets = window.findChildren(QWidget)
    except (RuntimeError, TypeError) as exc:
        logger.warning(f'RWTD_NATIVE_CONTROLS_SKIPPED {type(exc).__name__}: {exc}')
        return False
    receiver = Receiver()
    original_toggle = card.clicked
    # Disconnect only the original lifecycle callback, preserving other listeners.
    replace_callback(card.start_button.clicked, original_toggle, receiver.toggle, logger)
    # The F9 Handler resolves self.clicked each time; this signal queues to Qt.
    card.clicked = receiver.toggle_requested.emit
    # Mark installation before touching optional task cards so a framework
    # variant cannot rebind the header a second time after a partial install.
    window._rwtd_native_controls = receiver
    for widget in widgets:
        if (getattr(widget, 'start_button', None) is not None
                and callable(getattr(widget, 'start_clicked', None))
                and getattr(widget, 'task', None) is not None):
            original = widget.start_clicked
            task_receiver = TaskReceiver(widget, original)
            replace_callback(widget.start_button.clicked, original, task_receiver.start, logger)
    logger.info('RWTD_NATIVE_CONTROLS installed')
    return True
