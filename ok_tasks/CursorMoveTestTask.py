import ctypes
import time
from ctypes import wintypes

import win32api
import win32con
import win32gui

from src.tasks.BaseTBHTask import BaseTBHTask


class CursorMoveTestTask(BaseTBHTask):
    """
    TBH 游標移動診斷。

    只測「滑鼠游標能不能真的移動」，不會 click。

    每種方法都做兩段：
      A. 先移到 waypoint
      B. 再移到 target（優先使用 confirm 模板中心）

    只有 Windows 真實 GetCursorPos() 同時到達 A / B 才算 SUCCESS。
    這樣即使上一個方法已把游標留在 target，也不會造成下一個方法假陽性。
    """

    METHOD_OPTIONS = [
        ("測試 01｜TBH PynputInteraction.move", "tbh_pynput"),
        ("測試 02｜直接 pynput Controller.position", "pynput_direct"),
        ("測試 03｜win32api.SetCursorPos", "win32api_setcursor"),
        ("測試 04｜ctypes user32.SetCursorPos", "ctypes_setcursor"),
        ("測試 05｜user32.SetPhysicalCursorPos", "set_physical_cursor"),
        ("測試 06｜win32api.mouse_event ABSOLUTE", "win32_mouse_abs"),
        ("測試 07｜ctypes mouse_event ABSOLUTE", "ctypes_mouse_abs"),
        ("測試 08｜SendInput ABSOLUTE + VIRTUALDESK", "sendinput_abs"),
        ("測試 09｜SendInput ABSOLUTE + NOCOALESCE", "sendinput_abs_nocoalesce"),
        ("測試 10｜SendInput RELATIVE", "sendinput_rel"),
        ("測試 11｜win32api.mouse_event RELATIVE", "win32_mouse_rel"),
        ("測試 12｜ctypes mouse_event RELATIVE", "ctypes_mouse_rel"),
        ("測試 13｜暫時解除 ClipCursor + SetCursorPos", "unclip_setcursor"),
        ("測試 14｜pyautogui.moveTo（若已安裝）", "pyautogui"),
        ("測試 15｜mouse.move（若已安裝）", "mouse_package"),
        ("測試 16｜pydirectinput.moveTo（若已安裝）", "pydirectinput"),
        ("測試 17｜目前 executor.interaction.move", "current_interaction"),
        ("測試 18｜GenshinInteraction.move（虛擬 WM_MOUSEMOVE）", "genshin_move"),
        ("測試 19｜PostMessageInteraction.move（虛擬）", "postmessage_move"),
        ("測試 20｜raw PostMessage WM_MOUSEMOVE（虛擬）", "raw_postmessage"),
        ("測試 21｜raw SendMessage WM_MOUSEMOVE（虛擬）", "raw_sendmessage"),
    ]

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)

        self.name = "TBH 游標移動 TEST"
        self.description = (
            "依序測試 Pynput、SetCursorPos、mouse_event、SendInput、"
            "ClipCursor、PyAutoGUI、DirectInput 與背景 WM_MOUSEMOVE。"
            "只移動、不點擊；每種方法都用 GetCursorPos 驗證真實游標。"
        )

        self.default_config = {
            "_enabled": True,
            **{label: True for label, _ in self.METHOD_OPTIONS},
            "測試前倒數秒數": 3.0,
            "每段停留秒數": 0.60,
            "成功容許誤差(px)": 8,
            "測試前取得 TBH 前景": True,
            "優先使用 confirm 模板": True,
            "成功後停止": False,
            "成功方法移回原位置": True,
        }

        self.config_description = {
            "測試前倒數秒數":
                "執行後先倒數；可利用這段時間讓 WARNING / Confirm 保持在畫面上。",
            "每段停留秒數":
                "每次移動後停留多久，方便肉眼觀察游標。",
            "成功容許誤差(px)":
                "GetCursorPos 與目標座標的最大容許誤差。",
            "測試前取得 TBH 前景":
                "先使用 BaseTBHTask.tbh_ensure_foreground()。"
                "PynputInteraction.move 需要 TBH foreground。",
            "優先使用 confirm 模板":
                "找到 confirm 時，以 Confirm 中心當 target；"
                "找不到則使用遊戲畫面中央偏右位置。",
            "成功後停止":
                "True 時第一個真正能移動游標的方法成功後就停止；"
                "False 時會把所有方法跑完。",
            "成功方法移回原位置":
                "某方法 MOVE=SUCCESS 後，用同一方法把游標移回該方法開始前的位置；"
                "移回結果只記錄 RESTORE，不改變原本 MOVE 判定。",
        }

        for label, _ in self.METHOD_OPTIONS:
            self.config_description[label] = "是否執行此游標移動方法。"

    # ---------------------------------------------------------
    # Common
    # ---------------------------------------------------------

    @property
    def delay(self):
        try:
            return max(
                0.15,
                min(
                    float(self.config.get("每段停留秒數", 0.60)),
                    5.0
                )
            )
        except Exception:
            return 0.60

    @property
    def tolerance(self):
        try:
            return max(
                1,
                min(
                    int(self.config.get("成功容許誤差(px)", 8)),
                    100
                )
            )
        except Exception:
            return 8

    @staticmethod
    def _cursor_pos():
        try:
            x, y = win32api.GetCursorPos()
            return int(x), int(y)
        except Exception:
            return None

    def _interaction_context(self):
        current = self.executor.interaction
        capture = getattr(current, "capture", None)
        hwnd_window = getattr(current, "hwnd_window", None)

        if capture is None or hwnd_window is None:
            raise RuntimeError(
                "目前 interaction 缺少 capture / hwnd_window"
            )

        return current, capture, hwnd_window

    @staticmethod
    def _clamp(value, low, high):
        return max(low, min(high, value))

    def _target_points(self, capture):
        """
        回傳：
          waypoint_local, waypoint_abs, target_local, target_abs
        """
        self.next_frame()

        target = None

        if self.config.get("優先使用 confirm 模板", True):
            try:
                issue = self.tbh_find_fullscreen(
                    "issue",
                    threshold=0.70
                )
                if issue:
                    target = self.tbh_find_relative(
                        "confirm",
                        issue,
                        "issue",
                        padding=45,
                        threshold=0.65
                    )
                if not target:
                    target = self.tbh_find_fullscreen(
                        "confirm",
                        threshold=0.65
                    )
            except Exception:
                target = None

        if target:
            tx, ty = target.center()
            target_name = "confirm"
        else:
            tx = int(self.width * 0.62)
            ty = int(self.height * 0.58)
            target_name = "fallback"

        tx = self._clamp(int(tx), 20, max(20, self.width - 20))
        ty = self._clamp(int(ty), 20, max(20, self.height - 20))

        # waypoint 必須和 target 明顯不同。
        wx = tx - 220
        wy = ty - 160

        if wx < 30:
            wx = tx + 220
        if wy < 30:
            wy = ty + 160

        wx = self._clamp(int(wx), 20, max(20, self.width - 20))
        wy = self._clamp(int(wy), 20, max(20, self.height - 20))

        waypoint_abs = tuple(
            map(int, capture.get_abs_cords(wx, wy))
        )
        target_abs = tuple(
            map(int, capture.get_abs_cords(tx, ty))
        )

        self.log_info(
            f"[CURSOR TEST] target={target_name} "
            f"local=({tx},{ty}) abs={target_abs}; "
            f"waypoint local=({wx},{wy}) abs={waypoint_abs}"
        )

        return (
            (wx, wy),
            waypoint_abs,
            (tx, ty),
            target_abs,
        )

    def _near(self, actual, expected):
        if actual is None:
            return False
        return (
            abs(actual[0] - expected[0]) <= self.tolerance
            and abs(actual[1] - expected[1]) <= self.tolerance
        )

    def _virtual_metrics(self):
        vx = win32api.GetSystemMetrics(76)   # SM_XVIRTUALSCREEN
        vy = win32api.GetSystemMetrics(77)   # SM_YVIRTUALSCREEN
        vw = win32api.GetSystemMetrics(78)   # SM_CXVIRTUALSCREEN
        vh = win32api.GetSystemMetrics(79)   # SM_CYVIRTUALSCREEN

        if vw <= 0 or vh <= 0:
            vx = 0
            vy = 0
            vw = win32api.GetSystemMetrics(0)
            vh = win32api.GetSystemMetrics(1)

        return int(vx), int(vy), int(vw), int(vh)

    def _normalize_abs(self, x, y):
        vx, vy, vw, vh = self._virtual_metrics()

        nx = round(
            (x - vx) * 65535 / max(1, vw - 1)
        )
        ny = round(
            (y - vy) * 65535 / max(1, vh - 1)
        )

        return (
            self._clamp(nx, 0, 65535),
            self._clamp(ny, 0, 65535),
        )

    def _target_hwnd(self, hwnd_window):
        for name in ("top_hwnd", "hwnd", "real_hwnd", "handle"):
            try:
                value = getattr(hwnd_window, name, 0)
                if callable(value):
                    value = value()
                value = int(value or 0)
                if value > 0:
                    root = int(
                        ctypes.windll.user32.GetAncestor(
                            value,
                            2  # GA_ROOT
                        )
                        or 0
                    )
                    return root or value
            except Exception:
                continue

        raise RuntimeError("無法取得 TBH HWND")

    def _run_one(
        self,
        number,
        label,
        mover,
        waypoint_local,
        waypoint_abs,
        target_local,
        target_abs,
        capture,
    ):
        self.log_info(
            f"---------- {label} ----------"
        )

        before = self._cursor_pos()
        self.log_info(
            f"[{number:02d}] before={before}"
        )

        try:
            mover(
                waypoint_local,
                waypoint_abs
            )
        except ModuleNotFoundError as exc:
            self.log_info(
                f"[{number:02d}] SKIP：缺少套件 {exc.name}"
            )
            return "SKIP"
        except ImportError as exc:
            self.log_info(
                f"[{number:02d}] SKIP：{exc}"
            )
            return "SKIP"
        except Exception as exc:
            self.log_info(
                f"[{number:02d}] ERROR waypoint：{exc!r}"
            )
            return "ERROR"

        time.sleep(self.delay)

        pos_a = self._cursor_pos()
        a_ok = self._near(
            pos_a,
            waypoint_abs
        )

        self.log_info(
            f"[{number:02d}] waypoint expected={waypoint_abs} "
            f"actual={pos_a} ok={a_ok}"
        )

        try:
            mover(
                target_local,
                target_abs
            )
        except Exception as exc:
            self.log_info(
                f"[{number:02d}] ERROR target：{exc!r}"
            )
            return "ERROR"

        time.sleep(self.delay)

        pos_b = self._cursor_pos()
        b_ok = self._near(
            pos_b,
            target_abs
        )

        result = (
            "SUCCESS"
            if a_ok and b_ok
            else "FAIL"
        )

        self.log_info(
            f"[{number:02d}] target expected={target_abs} "
            f"actual={pos_b} ok={b_ok}"
        )
        self.log_info(
            f"[{number:02d}] RESULT = {result}"
        )

        # 只有已證明能真正移動 Windows 游標的方法才做 restore。
        # 判定已經在 waypoint + target 完成，因此 restore 不會改變 RESULT。
        if (
            result == "SUCCESS"
            and self.config.get(
                "成功方法移回原位置",
                True
            )
            and before is not None
        ):
            try:
                # capture.get_abs_cords(local) 在目前 Win32 capture 為 origin + local。
                # 用已知 target local/abs 反推出 capture origin，
                # 讓需要 local 座標的方法也能用「同一 mover」返回。
                origin_x = int(target_abs[0] - target_local[0])
                origin_y = int(target_abs[1] - target_local[1])
                restore_local = (
                    int(before[0] - origin_x),
                    int(before[1] - origin_y),
                )

                mover(
                    restore_local,
                    (
                        int(before[0]),
                        int(before[1]),
                    )
                )

                time.sleep(self.delay)

                restored = self._cursor_pos()
                restore_ok = self._near(
                    restored,
                    before
                )

                self.log_info(
                    f"[{number:02d}] RESTORE expected={before} "
                    f"actual={restored} ok={restore_ok}"
                )
            except Exception as exc:
                self.log_info(
                    f"[{number:02d}] RESTORE ERROR：{exc!r}"
                )

        return result

    # ---------------------------------------------------------
    # Physical movement methods
    # ---------------------------------------------------------

    def _move_tbh_pynput(self, local, absolute):
        interaction = self._get_tbh_pynput_interaction()
        interaction.move(
            int(local[0]),
            int(local[1])
        )

    @staticmethod
    def _move_pynput_direct(local, absolute):
        from pynput import mouse
        mouse.Controller().position = (
            int(absolute[0]),
            int(absolute[1])
        )

    @staticmethod
    def _move_win32_setcursor(local, absolute):
        win32api.SetCursorPos(
            (
                int(absolute[0]),
                int(absolute[1])
            )
        )

    @staticmethod
    def _move_ctypes_setcursor(local, absolute):
        result = ctypes.windll.user32.SetCursorPos(
            int(absolute[0]),
            int(absolute[1])
        )
        if not result:
            raise ctypes.WinError()

    @staticmethod
    def _move_set_physical_cursor(local, absolute):
        user32 = ctypes.windll.user32
        if not hasattr(user32, "SetPhysicalCursorPos"):
            raise RuntimeError(
                "此 Windows 沒有 SetPhysicalCursorPos"
            )
        result = user32.SetPhysicalCursorPos(
            int(absolute[0]),
            int(absolute[1])
        )
        if not result:
            raise ctypes.WinError()

    def _move_win32_mouse_abs(self, local, absolute):
        nx, ny = self._normalize_abs(
            *absolute
        )
        win32api.mouse_event(
            win32con.MOUSEEVENTF_MOVE
            | win32con.MOUSEEVENTF_ABSOLUTE
            | 0x4000,  # MOUSEEVENTF_VIRTUALDESK
            nx,
            ny,
            0,
            0
        )

    def _move_ctypes_mouse_abs(self, local, absolute):
        nx, ny = self._normalize_abs(
            *absolute
        )
        ctypes.windll.user32.mouse_event(
            0x0001 | 0x8000 | 0x4000,
            nx,
            ny,
            0,
            0
        )

    # ---------------------------------------------------------
    # SendInput
    # ---------------------------------------------------------

    @staticmethod
    def _sendinput_mouse(dx, dy, flags):
        ULONG_PTR = (
            ctypes.c_ulonglong
            if ctypes.sizeof(ctypes.c_void_p) == 8
            else ctypes.c_ulong
        )

        class MOUSEINPUT(ctypes.Structure):
            _fields_ = [
                ("dx", wintypes.LONG),
                ("dy", wintypes.LONG),
                ("mouseData", wintypes.DWORD),
                ("dwFlags", wintypes.DWORD),
                ("time", wintypes.DWORD),
                ("dwExtraInfo", ULONG_PTR),
            ]

        class _INPUTUNION(ctypes.Union):
            _fields_ = [
                ("mi", MOUSEINPUT),
            ]

        class INPUT(ctypes.Structure):
            _anonymous_ = ("u",)
            _fields_ = [
                ("type", wintypes.DWORD),
                ("u", _INPUTUNION),
            ]

        event = INPUT(
            type=0,
            mi=MOUSEINPUT(
                dx=int(dx),
                dy=int(dy),
                mouseData=0,
                dwFlags=int(flags),
                time=0,
                dwExtraInfo=0,
            )
        )

        sent = ctypes.windll.user32.SendInput(
            1,
            ctypes.byref(event),
            ctypes.sizeof(INPUT)
        )

        if sent != 1:
            raise ctypes.WinError()

    def _move_sendinput_abs(self, local, absolute):
        nx, ny = self._normalize_abs(
            *absolute
        )
        self._sendinput_mouse(
            nx,
            ny,
            0x0001 | 0x8000 | 0x4000
        )

    def _move_sendinput_abs_nocoalesce(self, local, absolute):
        nx, ny = self._normalize_abs(
            *absolute
        )
        self._sendinput_mouse(
            nx,
            ny,
            0x0001 | 0x8000 | 0x4000 | 0x2000
        )

    def _move_sendinput_rel(self, local, absolute):
        current = self._cursor_pos()
        if current is None:
            raise RuntimeError(
                "GetCursorPos 失敗"
            )

        self._sendinput_mouse(
            int(absolute[0] - current[0]),
            int(absolute[1] - current[1]),
            0x0001
        )

    @staticmethod
    def _move_win32_mouse_rel(local, absolute):
        current = win32api.GetCursorPos()
        win32api.mouse_event(
            win32con.MOUSEEVENTF_MOVE,
            int(absolute[0] - current[0]),
            int(absolute[1] - current[1]),
            0,
            0
        )

    @staticmethod
    def _move_ctypes_mouse_rel(local, absolute):
        current = win32api.GetCursorPos()
        ctypes.windll.user32.mouse_event(
            0x0001,
            int(absolute[0] - current[0]),
            int(absolute[1] - current[1]),
            0,
            0
        )

    # ---------------------------------------------------------
    # Cursor clipping / optional packages
    # ---------------------------------------------------------

    @staticmethod
    def _move_unclip_setcursor(local, absolute):
        class RECT(ctypes.Structure):
            _fields_ = [
                ("left", wintypes.LONG),
                ("top", wintypes.LONG),
                ("right", wintypes.LONG),
                ("bottom", wintypes.LONG),
            ]

        # 使用獨立 WinDLL 實例，不修改 ctypes.windll.user32 的 argtypes。
        user32 = ctypes.WinDLL(
            "user32",
            use_last_error=True
        )

        rect = RECT()
        got_rect = bool(
            user32.GetClipCursor(
                ctypes.byref(rect)
            )
        )

        # 暫時解除 cursor clipping。
        user32.ClipCursor(None)

        try:
            if not user32.SetCursorPos(
                int(absolute[0]),
                int(absolute[1])
            ):
                raise ctypes.WinError(
                    ctypes.get_last_error()
                )
        finally:
            if got_rect:
                user32.ClipCursor(
                    ctypes.byref(rect)
                )

    @staticmethod
    def _move_pyautogui(local, absolute):
        import pyautogui

        old_failsafe = pyautogui.FAILSAFE
        try:
            pyautogui.FAILSAFE = False
            pyautogui.moveTo(
                int(absolute[0]),
                int(absolute[1]),
                duration=0
            )
        finally:
            pyautogui.FAILSAFE = old_failsafe

    @staticmethod
    def _move_mouse_package(local, absolute):
        import mouse

        mouse.move(
            int(absolute[0]),
            int(absolute[1]),
            absolute=True,
            duration=0
        )

    @staticmethod
    def _move_pydirectinput(local, absolute):
        import pydirectinput

        old_pause = getattr(
            pydirectinput,
            "PAUSE",
            0
        )
        try:
            pydirectinput.PAUSE = 0
            pydirectinput.moveTo(
                int(absolute[0]),
                int(absolute[1]),
                duration=0
            )
        finally:
            pydirectinput.PAUSE = old_pause

    # ---------------------------------------------------------
    # ok-script / virtual background methods
    # ---------------------------------------------------------

    def _move_current_interaction(self, local, absolute):
        self.executor.interaction.move(
            int(local[0]),
            int(local[1])
        )

    def _move_genshin(self, local, absolute):
        from ok.device.interaction_methods import GenshinInteraction

        current, capture, hwnd_window = (
            self._interaction_context()
        )
        interaction = GenshinInteraction(
            capture,
            hwnd_window
        )
        interaction.move(
            int(local[0]),
            int(local[1])
        )

    def _move_postmessage(self, local, absolute):
        from ok.device.interaction_methods import PostMessageInteraction

        current, capture, hwnd_window = (
            self._interaction_context()
        )
        interaction = PostMessageInteraction(
            capture,
            hwnd_window
        )
        interaction.move(
            int(local[0]),
            int(local[1])
        )

    def _move_raw_postmessage(self, local, absolute):
        _, _, hwnd_window = (
            self._interaction_context()
        )
        hwnd = self._target_hwnd(
            hwnd_window
        )
        lparam = win32api.MAKELONG(
            int(local[0]),
            int(local[1])
        )
        win32gui.PostMessage(
            hwnd,
            win32con.WM_MOUSEMOVE,
            0,
            lparam
        )

    def _move_raw_sendmessage(self, local, absolute):
        _, _, hwnd_window = (
            self._interaction_context()
        )
        hwnd = self._target_hwnd(
            hwnd_window
        )
        lparam = win32api.MAKELONG(
            int(local[0]),
            int(local[1])
        )
        win32gui.SendMessage(
            hwnd,
            win32con.WM_MOUSEMOVE,
            0,
            lparam
        )

    # ---------------------------------------------------------
    # Main
    # ---------------------------------------------------------

    def run(self):
        self.log_info(
            "========== TBH CURSOR MOVE TEST START =========="
        )

        try:
            delay = max(
                0.0,
                min(
                    float(
                        self.config.get(
                            "測試前倒數秒數",
                            3.0
                        )
                    ),
                    120.0
                )
            )
        except Exception:
            delay = 3.0

        if delay > 0:
            self.log_info(
                f"[CURSOR TEST] {delay:.1f} 秒後開始；"
                "請讓 WARNING / Confirm 保持在畫面上。"
            )
            time.sleep(delay)

        current, capture, hwnd_window = (
            self._interaction_context()
        )

        self.log_info(
            f"[CURSOR TEST] interaction="
            f"{type(current).__name__}"
        )
        self.log_info(
            f"[CURSOR TEST] initial_cursor="
            f"{self._cursor_pos()}"
        )

        # 記錄目前 ClipCursor。
        try:
            class RECT(ctypes.Structure):
                _fields_ = [
                    ("left", wintypes.LONG),
                    ("top", wintypes.LONG),
                    ("right", wintypes.LONG),
                    ("bottom", wintypes.LONG),
                ]
            user32 = ctypes.WinDLL("user32", use_last_error=True)
            rect = RECT()
            if user32.GetClipCursor(ctypes.byref(rect)):
                self.log_info(
                    "[CURSOR TEST] ClipCursor="
                    f"({rect.left},{rect.top},"
                    f"{rect.right},{rect.bottom})"
                )
        except Exception as exc:
            self.log_info(
                f"[CURSOR TEST] GetClipCursor failed: {exc!r}"
            )

        if self.config.get(
            "測試前取得 TBH 前景",
            True
        ):
            ok = self.tbh_ensure_foreground()
            self.log_info(
                f"[CURSOR TEST] tbh_ensure_foreground={ok}"
            )
            time.sleep(0.35)

        (
            waypoint_local,
            waypoint_abs,
            target_local,
            target_abs,
        ) = self._target_points(
            capture
        )

        methods = {
            "tbh_pynput": self._move_tbh_pynput,
            "pynput_direct": self._move_pynput_direct,
            "win32api_setcursor": self._move_win32_setcursor,
            "ctypes_setcursor": self._move_ctypes_setcursor,
            "set_physical_cursor": self._move_set_physical_cursor,
            "win32_mouse_abs": self._move_win32_mouse_abs,
            "ctypes_mouse_abs": self._move_ctypes_mouse_abs,
            "sendinput_abs": self._move_sendinput_abs,
            "sendinput_abs_nocoalesce": self._move_sendinput_abs_nocoalesce,
            "sendinput_rel": self._move_sendinput_rel,
            "win32_mouse_rel": self._move_win32_mouse_rel,
            "ctypes_mouse_rel": self._move_ctypes_mouse_rel,
            "unclip_setcursor": self._move_unclip_setcursor,
            "pyautogui": self._move_pyautogui,
            "mouse_package": self._move_mouse_package,
            "pydirectinput": self._move_pydirectinput,
            "current_interaction": self._move_current_interaction,
            "genshin_move": self._move_genshin,
            "postmessage_move": self._move_postmessage,
            "raw_postmessage": self._move_raw_postmessage,
            "raw_sendmessage": self._move_raw_sendmessage,
        }

        results = []

        for number, (label, key) in enumerate(
            self.METHOD_OPTIONS,
            start=1
        ):
            if not self.config.get(
                label,
                True
            ):
                self.log_info(
                    f"[{number:02d}] SKIP(UI OFF) {label}"
                )
                continue

            result = self._run_one(
                number,
                label,
                methods[key],
                waypoint_local,
                waypoint_abs,
                target_local,
                target_abs,
                capture,
            )

            results.append(
                (number, label, result)
            )

            if (
                result == "SUCCESS"
                and self.config.get(
                    "成功後停止",
                    False
                )
            ):
                break

        self.log_info(
            "========== CURSOR MOVE TEST SUMMARY =========="
        )

        winners = []

        for number, label, result in results:
            self.log_info(
                f"[{number:02d}] {result} | {label}"
            )
            if result == "SUCCESS":
                winners.append(
                    f"{number:02d}"
                )

        self.log_info(
            "WINNER(S) = "
            + (
                ", ".join(winners)
                if winners
                else "NONE"
            )
        )
        self.log_info(
            "========== TBH CURSOR MOVE TEST END =========="
        )

        return bool(winners)
