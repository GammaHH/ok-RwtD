"""Opt-in event diagnostics for the real run-bar button; no input generation."""
import os


def install(window, logger):
    if os.environ.get('RWTD_POINTER_DIAGNOSTICS') != '1' or getattr(window, '_rwtd_pointer_diagnostics', None):
        return
    button = getattr(getattr(window, 'run_bar', None), 'executor_button', None)
    if button is None:
        logger.warning('RWTD_POINTER unsupported run bar')
        return
    from PySide6.QtCore import QEvent, QObject
    from PySide6.QtGui import QCursor
    from PySide6.QtWidgets import QApplication

    def target_name(widget):
        if widget is None:
            return 'None'
        return type(widget).__name__ + ':' + widget.objectName()

    class Events(QObject):
        def eventFilter(self, watched, event):
            if event.type() in (QEvent.Type.MouseButtonPress, QEvent.Type.MouseButtonRelease):
                point = event.globalPosition().toPoint()
                local = button.mapFromGlobal(point)
                if window.isVisible() and button.rect().adjusted(-8, -8, 8, 8).contains(local):
                    rect = button.rect()
                    logger.info(f'RWTD_POINTER event={event.type().name} target={target_name(watched)} '
                                f'local={local.x()},{local.y()} size={rect.width()}x{rect.height()} '
                                f'hit={button.hitButton(local)} enabled={button.isEnabled()} '
                                f'widget_at={target_name(QApplication.widgetAt(point))} '
                                f'dpr={window.devicePixelRatioF()}')
            return False

    receiver = Events(window)
    QApplication.instance().installEventFilter(receiver)
    original = getattr(window, '_hit_test', None)
    if callable(original):
        def hit_test(x, y):
            result = original(x, y)
            local = button.mapFromGlobal(QCursor.pos())
            if button.rect().contains(local) and result != 1:
                logger.info(f'RWTD_POINTER native_hit={result} cursor_local={local.x()},{local.y()} '
                            f'native_screen={x},{y} size={button.width()}x{button.height()} '
                            f'dpr={window.devicePixelRatioF()} maximized={window.isMaximized()}')
            return result
        window._hit_test = hit_test
    button.clicked.connect(lambda: logger.info('RWTD_POINTER button_clicked'))
    window._rwtd_pointer_diagnostics = receiver
    logger.info('RWTD_POINTER installed; click left, middle and right of the run-bar button')
