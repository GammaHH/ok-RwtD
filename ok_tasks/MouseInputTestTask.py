import ctypes
import time

import win32api
import win32con

from ok.device.interaction_methods import (
    ForegroundPostMessageInteraction,
    GenshinInteraction,
    PostMessageInteraction,
    PyDirectInteraction,
    PynputInteraction,
)

from src.tasks.BaseTBHTask import BaseTBHTask


class _MOUSEINPUT(ctypes.Structure):
    _fields_ = [
        ("dx", ctypes.c_long),
        ("dy", ctypes.c_long),
        ("mouseData", ctypes.c_ulong),
        ("dwFlags", ctypes.c_ulong),
        ("time", ctypes.c_ulong),
        ("dwExtraInfo", ctypes.POINTER(ctypes.c_ulong)),
    ]


class _INPUT(ctypes.Structure):
    _fields_ = [
        ("type", ctypes.c_ulong),
        ("mi", _MOUSEINPUT),
    ]


class MouseInputTestTask(BaseTBHTask):
    """
    TBH 滑鼠輸入診斷。

    依序測試多種滑鼠輸入管道。
    每次送出後都重新辨識 synthesis_on；
    只有 Cube UI 狀態真的改變才算成功。
    """

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)

        self.name = "滑鼠輸入 TEST"
        self.description = (
            "依序測試 Current/WOG、Genshin、PostMessage、"
            "ForegroundPostMessage、Pynput、PyDirect、Win32 SendInput；"
            "每次都用 synthesis_on 驗證 Cube 是否真的切換。"
        )

        self.default_config = {
            "CUBE開啟閾值": 0.70,
            "每管道等待秒數": 1.8,
            "點擊按住秒數": 0.05,
        }

        self.config_description = {
            "CUBE開啟閾值":
                "synthesis_on 全畫面模板達此值視為 Cube 已開啟。",
            "每管道等待秒數":
                "每種輸入送出後，等待 synthesis_on 狀態改變的最長秒數。",
            "點擊按住秒數":
                "滑鼠 Left Down 到 Left Up 的時間。",
        }

    # ---------------------------------------------------------
    # Cube / menu detection
    # ---------------------------------------------------------

    @staticmethod
    def _confidence(box):
        if not box:
            return 0.0
        try:
            return float(
                getattr(
                    box,
                    "confidence",
                    0.0
                )
                or 0.0
            )
        except (TypeError, ValueError):
            return 0.0

    def cube_open_state(self, verbose=False):
        """
        核心判定 = synthesis_on。
        另外記錄 autoadd_synthesis / synthesis_start，
        幫助確認不是 synthesis_on 假陽性。
        """
        self.next_frame()

        threshold = self.tbh_number(
            "CUBE開啟閾值",
            0.70,
            0.30,
            0.99
        )

        anchor = self.tbh_find_fullscreen(
            "synthesis_on",
            threshold=threshold
        )

        if not anchor:
            if verbose:
                self.log_info(
                    "Cube 狀態：CLOSED（找不到 synthesis_on）"
                )
            return False

        fill = self.tbh_find_relative(
            "autoadd_synthesis",
            anchor,
            "synthesis_on",
            threshold=0.65
        )

        start = self.tbh_find_relative(
            "synthesis_start",
            anchor,
            "synthesis_on",
            threshold=0.65
        )

        if verbose:
            self.log_info(
                "Cube 狀態：OPEN "
                f"(synthesis_on={self._confidence(anchor):.2f}, "
                f"autoadd={'Y' if fill else 'N'}, "
                f"start={'Y' if start else 'N'})"
            )

        return True

    def find_cube_button(self):
        self.next_frame()

        cube = self.tbh_find_fullscreen(
            "cube",
            threshold=0.65
        )

        if not cube:
            return None

        self.log_info(
            "Cube 按鈕："
            f"x={cube.x}, y={cube.y}, "
            f"w={cube.width}, h={cube.height}, "
            f"confidence={self._confidence(cube):.2f}"
        )

        return cube

    def ensure_menu_on_for_test(self):
        self.next_frame()
        state, _ = self.tbh_menu_state()

        if state == "on":
            return True

        if state != "off":
            self.log_info(
                "無法辨認 menu_on/menu_off，TEST 中止"
            )
            return False

        self.log_info(
            "Menu OFF：使用目前已驗證成功的鍵盤路徑送 TAB"
        )

        if not self.tbh_send_key("tab"):
            self.log_info(
                "TAB 發送失敗"
            )
            return False

        deadline = time.monotonic() + 2.0

        while time.monotonic() < deadline:
            self.sleep(0.10)
            self.next_frame()
            state, _ = self.tbh_menu_state()

            if state == "on":
                self.log_info(
                    "Menu ON：確認完成"
                )
                return True

        self.log_info(
            "TAB 後仍未確認 menu_on"
        )
        return False

    # ---------------------------------------------------------
    # Generic state verification
    # ---------------------------------------------------------

    def wait_cube_state(self, expected, timeout):
        deadline = time.monotonic() + timeout

        while time.monotonic() < deadline:
            if self.cube_open_state() == expected:
                return True
            self.sleep(0.10)

        return False

    def verify_channel(
        self,
        channel_name,
        click_func
    ):
        """
        不信任 API return value。
        唯一成功條件 = synthesis_on 狀態真的切換。

        若開始時 Cube 已開：
        先測試是否能關，再用同一管道重新開啟。
        """
        before = self.cube_open_state(
            verbose=True
        )

        self.log_info(
            f"[TEST] {channel_name}："
            f"點擊前={'OPEN' if before else 'CLOSED'}"
        )

        try:
            click_func()
        except Exception as exc:
            self.log_info(
                f"[TEST] {channel_name}："
                f"送出時 exception: {exc}"
            )
            return False

        timeout = self.tbh_number(
            "每管道等待秒數",
            1.8,
            0.5,
            10.0
        )

        changed = self.wait_cube_state(
            not before,
            timeout
        )

        after = self.cube_open_state(
            verbose=True
        )

        if not changed:
            self.log_info(
                f"[TEST] {channel_name}：FAIL，"
                "Cube 狀態沒有改變"
            )
            return False

        if after:
            self.log_info(
                f"[TEST] {channel_name}：SUCCESS，"
                "已成功開啟 Cube"
            )
            return True

        # OPEN -> CLOSED happened. Prove the same channel can reopen.
        self.log_info(
            f"[TEST] {channel_name}：第一次成功關閉 Cube；"
            "同一管道再點一次確認可重新開啟"
        )

        try:
            click_func()
        except Exception as exc:
            self.log_info(
                f"[TEST] {channel_name}："
                f"重新開啟時 exception: {exc}"
            )
            return False

        if self.wait_cube_state(
            True,
            timeout
        ):
            self.cube_open_state(
                verbose=True
            )
            self.log_info(
                f"[TEST] {channel_name}：SUCCESS，"
                "關閉與重新開啟皆成功"
            )
            return True

        self.log_info(
            f"[TEST] {channel_name}："
            "能切換一次，但未能重新開啟"
        )
        return False

    # ---------------------------------------------------------
    # Interaction helpers
    # ---------------------------------------------------------

    def interaction_context(self):
        current = self.executor.interaction
        capture = getattr(
            current,
            "capture",
            None
        )
        hwnd_window = getattr(
            current,
            "hwnd_window",
            None
        )

        if capture is None or hwnd_window is None:
            raise RuntimeError(
                "目前 interaction 缺少 capture / hwnd_window"
            )

        return current, capture, hwnd_window

    @staticmethod
    def box_center(box):
        return (
            int(
                round(
                    box.x
                    + box.width / 2
                )
            ),
            int(
                round(
                    box.y
                    + box.height / 2
                )
            ),
        )

    def try_foreground(
        self,
        hwnd_window,
        reason
    ):
        try:
            result = (
                hwnd_window
                .bring_to_front()
            )

            self.sleep(0.15)

            self.log_info(
                f"[TEST] {reason} "
                f"bring_to_front={result}, "
                f"foreground="
                f"{hwnd_window.is_foreground()}"
            )
        except Exception as exc:
            self.log_info(
                f"[TEST] {reason} "
                f"bring_to_front exception: {exc}"
            )

    # ---------------------------------------------------------
    # Raw Win32 SendInput
    # ---------------------------------------------------------

    def raw_sendinput_click(
        self,
        cube,
        capture,
        hwnd_window
    ):
        self.try_foreground(
            hwnd_window,
            "Win32 SendInput"
        )

        x, y = self.box_center(
            cube
        )

        abs_x, abs_y = (
            capture.get_abs_cords(
                x,
                y
            )
        )

        win32api.SetCursorPos(
            (
                int(abs_x),
                int(abs_y)
            )
        )

        time.sleep(0.05)

        down = _INPUT(
            type=0,
            mi=_MOUSEINPUT(
                0,
                0,
                0,
                win32con.MOUSEEVENTF_LEFTDOWN,
                0,
                None,
            ),
        )

        up = _INPUT(
            type=0,
            mi=_MOUSEINPUT(
                0,
                0,
                0,
                win32con.MOUSEEVENTF_LEFTUP,
                0,
                None,
            ),
        )

        user32 = ctypes.windll.user32

        sent_down = user32.SendInput(
            1,
            ctypes.byref(down),
            ctypes.sizeof(_INPUT),
        )

        time.sleep(
            self.tbh_number(
                "點擊按住秒數",
                0.05,
                0.01,
                0.5
            )
        )

        sent_up = user32.SendInput(
            1,
            ctypes.byref(up),
            ctypes.sizeof(_INPUT),
        )

        self.log_info(
            "[TEST] Win32 SendInput raw："
            f"screen=({abs_x},{abs_y}), "
            f"down={sent_down}, "
            f"up={sent_up}"
        )

    # ---------------------------------------------------------
    # Main
    # ---------------------------------------------------------

    def run(self):
        if not self.ensure_menu_on_for_test():
            return False

        cube = self.find_cube_button()

        if not cube:
            self.log_info(
                "找不到 cube 按鈕；"
                "無法進行滑鼠輸入 TEST"
            )
            return False

        current, capture, hwnd_window = (
            self.interaction_context()
        )

        x, y = self.box_center(
            cube
        )

        self.log_info(
            "========== TBH MOUSE INPUT TEST START =========="
        )

        self.log_info(
            "目前 UI interaction = "
            f"{type(current).__name__}"
        )

        self.cube_open_state(
            verbose=True
        )

        # Build explicit ok-script interaction implementations.
        explicit_channels = []

        builders = [
            (
                "GenshinInteraction",
                GenshinInteraction
            ),
            (
                "PostMessageInteraction",
                PostMessageInteraction
            ),
            (
                "ForegroundPostMessageInteraction",
                ForegroundPostMessageInteraction
            ),
            (
                "PynputInteraction",
                PynputInteraction
            ),
            (
                "PyDirectInteraction",
                PyDirectInteraction
            ),
        ]

        for name, cls in builders:
            try:
                explicit_channels.append(
                    (
                        name,
                        cls(
                            capture,
                            hwnd_window
                        )
                    )
                )
            except Exception as exc:
                self.log_info(
                    f"[TEST] {name} 建立失敗: {exc}"
                )

        # 1. Current selected interaction through normal BaseTBHTask.click()
        if self.verify_channel(
            "CURRENT / BaseTBHTask.click",
            lambda: self.click(
                cube,
                after_sleep=0.05
            ),
        ):
            self.log_info(
                "WINNER = CURRENT / BaseTBHTask.click"
            )
            return True

        # 2..N. Explicit ok-script input channels.
        for name, interaction in explicit_channels:
            if name in {
                "ForegroundPostMessageInteraction",
                "PynputInteraction",
                "PyDirectInteraction",
            }:
                self.try_foreground(
                    hwnd_window,
                    name
                )

            def do_click(
                interaction=interaction
            ):
                interaction.click(
                    x=x,
                    y=y,
                    down_time=self.tbh_number(
                        "點擊按住秒數",
                        0.05,
                        0.01,
                        0.5
                    )
                )

            if self.verify_channel(
                name,
                do_click
            ):
                self.log_info(
                    f"WINNER = {name}"
                )
                return True

        # Final independent OS SendInput test.
        if self.verify_channel(
            "Win32 SendInput",
            lambda: self.raw_sendinput_click(
                cube,
                capture,
                hwnd_window
            ),
        ):
            self.log_info(
                "WINNER = Win32 SendInput"
            )
            return True

        self.log_info(
            "========== ALL MOUSE CHANNELS FAILED =========="
        )

        self.log_info(
            "所有管道都送過輸入，"
            "但 synthesis_on 狀態完全沒有改變。"
        )

        return False
