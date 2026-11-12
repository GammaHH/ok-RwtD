"""Scoped backport of OK-Script's native maximized-state fix.

Upstream: ok-oldking/ok-script d4bdd827917782dfde9f58dc684d8d96bed433cd.
Keep the installed framework intact; skip frameworks already carrying the fix.
"""
from functools import wraps


def install(logger, framework=None):
    if framework is None:
        try:
            from ok.ui.qt.kit import window as framework
        except ImportError:
            return False
    if not framework.IS_WINDOWS:
        return False
    window_class = framework.KitWindow
    if getattr(window_class, '_rwtd_native_frame_repair', False):
        return True
    if hasattr(window_class, '_is_zoomed'):
        logger.info('RWTD_NATIVE_FRAME upstream fix already present')
        return True
    original_event = window_class.nativeEvent
    api = framework.ctypes.windll.user32

    def is_zoomed(self):
        return bool(api.IsZoomed(self._hwnd()))

    def hit_test(self, screen_x, screen_y):
        hwnd = self._hwnd()
        point = framework._Point(screen_x, screen_y)
        framework.ctypes.windll.user32.ScreenToClient(hwnd, framework.ctypes.byref(point))
        client = framework._Rect()
        api.GetClientRect(hwnd, framework.ctypes.byref(client))
        ratio = self.devicePixelRatioF()
        x, y = point.x / ratio, point.y / ratio
        width, height = client.right / ratio, client.bottom / ratio
        if not self._is_zoomed() and not self.isFullScreen():
            border = framework.RESIZE_BORDER
            left, right = x < border, x >= width - border
            top, bottom = y < border, y >= height - border
            if top and left:
                return framework.HTTOPLEFT
            if top and right:
                return framework.HTTOPRIGHT
            if bottom and left:
                return framework.HTBOTTOMLEFT
            if bottom and right:
                return framework.HTBOTTOMRIGHT
            if left:
                return framework.HTLEFT
            if right:
                return framework.HTRIGHT
            if top:
                return framework.HTTOP
            if bottom:
                return framework.HTBOTTOM
        local = framework.QPoint(int(x), int(y))
        bar = self.title_bar
        if bar.maximize_button.isVisible() and bar.maximize_button.geometry().translated(bar.pos()).contains(local):
            return framework.HTMAXBUTTON
        if bar.is_drag_area(bar.mapFrom(self, local)):
            return framework.HTCAPTION
        return framework.HTCLIENT

    @wraps(original_event)
    def native_event(self, event_type, message):
        if event_type == b'windows_generic_MSG':
            msg = framework._Msg.from_address(int(message))
            if msg.message == framework.WM_NCCALCSIZE and msg.wParam:
                if api.IsZoomed(msg.hwnd):
                    params = framework._NcCalcSizeParams.from_address(msg.lParam)
                    frame_x, frame_y = self._frame_thickness()
                    rect = params.rgrc[0]
                    rect.left += frame_x
                    rect.top += frame_y
                    rect.right -= frame_x
                    rect.bottom -= frame_y
                return True, 0
        return original_event(self, event_type, message)

    window_class._is_zoomed = is_zoomed
    window_class._hit_test = hit_test
    window_class.nativeEvent = native_event
    window_class._rwtd_native_frame_repair = True
    logger.info('RWTD_NATIVE_FRAME installed upstream d4bdd82 maximized-state repair')
    return True
