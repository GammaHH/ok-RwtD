import ctypes
import subprocess
import time

import win32api
import win32con
import win32gui
import win32process

from ok.device.interaction_methods import GenshinInteraction
from src.tasks.BaseTBHTask import BaseTBHTask


class ForegroundFocusTestTask(BaseTBHTask):
    """
    TBH 前景取得方法診斷工具。

    目的：
    - 只測「怎樣讓 TBH 真正成為 Windows foreground window」。
    - 不改變正式 BaseTBHTask。
    - 不使用 Genshin 做實際滑鼠輸入；Genshin 只測 activate()。
    - 每種方法都有獨立 UI 開關。
    - 成功條件一律以 GetForegroundWindow() == TBH top-level HWND 為準。

    建議一次只開一種方法測試。
    若一次開多種，第一種成功後，後面的測試會因 TBH 已經在前景而失去比較意義；
    程式會在這種情況直接略過後續方法，避免產生假陽性。
    """

    METHOD_OPTIONS = [
        ("測試 01｜ok-script bring_to_front", "ok_bring_to_front"),
        ("測試 02｜Genshin activate（只啟用，不送輸入）", "genshin_activate"),
        ("測試 03｜ShowWindow(SW_RESTORE) + SetForegroundWindow", "restore_setforeground"),
        ("測試 04｜BringWindowToTop + SetForegroundWindow", "bringtop_setforeground"),
        ("測試 05｜SetWindowPos(HWND_TOP) + SetForegroundWindow", "setwindowpos_top"),
        ("測試 06｜AttachThreadInput + SetForegroundWindow + SetFocus", "attach_thread_input"),
        ("測試 07｜ALT 解鎖 + SetForegroundWindow", "alt_setforeground"),
        ("測試 08｜AttachThreadInput + ALT + SetForegroundWindow", "attach_alt"),
        ("測試 09｜TOPMOST pulse + SetForegroundWindow", "topmost_pulse"),
        ("測試 10｜SwitchToThisWindow", "switch_to_this_window"),
        ("測試 11｜WScript.Shell AppActivate(PID)", "wscript_appactivate"),
        ("測試 12｜WM_ACTIVATE / WM_NCACTIVATE 軟啟用", "wm_activate"),
        ("測試 13｜AllowSetForegroundWindow(ASFW_ANY) + SetForegroundWindow", "allow_setforeground"),
        ("測試 14｜LockSetForegroundWindow(UNLOCK) + SetForegroundWindow", "unlock_setforeground"),
        ("測試 15｜ShowWindowAsync + SetForegroundWindow", "showwindowasync"),
    ]

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)

        self.name = "前景取得 TEST"
        self.description = (
            "逐一測試 Windows / ok-script / Genshin 的前景取得方法。"
            "每種方法都有獨立 UI 開關；可設定測試前倒數時間，"
            "方便切到其他視窗模擬背景 Trigger。"
            "Genshin 只測 activate，不負責輸入。"
            "成功一律以 Windows GetForegroundWindow 驗證。"
        )

        # 預設只開最基本方法，避免一次全部跑造成後續假陽性。
        self.default_config = {
            "_enabled": True,

            "測試 01｜ok-script bring_to_front": True,
            "測試 02｜Genshin activate（只啟用，不送輸入）": False,
            "測試 03｜ShowWindow(SW_RESTORE) + SetForegroundWindow": False,
            "測試 04｜BringWindowToTop + SetForegroundWindow": False,
            "測試 05｜SetWindowPos(HWND_TOP) + SetForegroundWindow": False,
            "測試 06｜AttachThreadInput + SetForegroundWindow + SetFocus": False,
            "測試 07｜ALT 解鎖 + SetForegroundWindow": False,
            "測試 08｜AttachThreadInput + ALT + SetForegroundWindow": False,
            "測試 09｜TOPMOST pulse + SetForegroundWindow": False,
            "測試 10｜SwitchToThisWindow": False,
            "測試 11｜WScript.Shell AppActivate(PID)": False,
            "測試 12｜WM_ACTIVATE / WM_NCACTIVATE 軟啟用": False,
            "測試 13｜AllowSetForegroundWindow(ASFW_ANY) + SetForegroundWindow": False,
            "測試 14｜LockSetForegroundWindow(UNLOCK) + SetForegroundWindow": False,
            "測試 15｜ShowWindowAsync + SetForegroundWindow": False,

            "測試前倒數秒數": 10.0,
            "方法後等待秒數": 0.35,
            "成功後停止": True,
            "要求開始時 TBH 在背景": True,
        }

        self.config_description = {
            "測試 01｜ok-script bring_to_front":
                "目前 TBH 正式流程使用的方法。會直接呼叫 hwnd_window.bring_to_front()。",
            "測試 02｜Genshin activate（只啟用，不送輸入）":
                "只呼叫 GenshinInteraction.activate()，不做 Genshin click / key；"
                "用來確認 WoG 類似的 activation 是否能讓 TBH 取得真正 foreground。",
            "測試 03｜ShowWindow(SW_RESTORE) + SetForegroundWindow":
                "先 Restore 視窗，再呼叫 Win32 SetForegroundWindow。",
            "測試 04｜BringWindowToTop + SetForegroundWindow":
                "先 BringWindowToTop，再呼叫 SetForegroundWindow。",
            "測試 05｜SetWindowPos(HWND_TOP) + SetForegroundWindow":
                "先把視窗移到 Z-order 頂端，再嘗試取得 foreground。",
            "測試 06｜AttachThreadInput + SetForegroundWindow + SetFocus":
                "把目前 foreground thread 與 TBH thread 暫時 AttachThreadInput，"
                "再 BringWindowToTop / SetForegroundWindow / SetFocus。",
            "測試 07｜ALT 解鎖 + SetForegroundWindow":
                "模擬一次 ALT down/up 後立刻 SetForegroundWindow；"
                "常見 Windows foreground-lock workaround。",
            "測試 08｜AttachThreadInput + ALT + SetForegroundWindow":
                "把 AttachThreadInput 與 ALT workaround 合併。",
            "測試 09｜TOPMOST pulse + SetForegroundWindow":
                "短暫設為 TOPMOST，再取消 TOPMOST，之後 SetForegroundWindow。",
            "測試 10｜SwitchToThisWindow":
                "呼叫 user32.SwitchToThisWindow；屬舊式 Windows API，僅供比較。",
            "測試 11｜WScript.Shell AppActivate(PID)":
                "透過 Windows Script Host 的 AppActivate(PID) 嘗試切換到 TBH。",
            "測試 12｜WM_ACTIVATE / WM_NCACTIVATE 軟啟用":
                "只發送視窗 activation message；通常不等於真正 foreground，"
                "但可確認 Genshin 類軟啟用效果。",
            "測試 13｜AllowSetForegroundWindow(ASFW_ANY) + SetForegroundWindow":
                "先嘗試放寬 foreground permission，再 SetForegroundWindow。"
                "Windows 可能拒絕 AllowSetForegroundWindow，本測試會記錄結果。",
            "測試 14｜LockSetForegroundWindow(UNLOCK) + SetForegroundWindow":
                "先呼叫 LockSetForegroundWindow(LSFW_UNLOCK)，再 SetForegroundWindow。"
                "Windows 可能拒絕，本測試只記錄。",
            "測試 15｜ShowWindowAsync + SetForegroundWindow":
                "用 ShowWindowAsync(SW_RESTORE) 後再 SetForegroundWindow。",
            "測試前倒數秒數":
                "按下 TEST 後先等待這麼多秒才開始真正測試。"
                "利用這段時間切到 Chrome / Explorer / 其他程式並操作幾下，"
                "可模擬 AutoStore / TriggerTask 在背景自行觸發的情境。",
            "方法後等待秒數":
                "每種 foreground 方法送出後，等待 Windows 完成焦點切換的時間。",
            "成功後停止":
                "成功取得 TBH foreground 後停止，不再測後續方法。建議保持開啟。",
            "要求開始時 TBH 在背景":
                "若開始執行時 TBH 已經是 foreground，直接停止測試，避免假陽性。",
        }

    # =========================================================
    # Common helpers
    # =========================================================

    @property
    def pre_test_delay(self):
        try:
            value = float(
                getattr(self, "config", {}).get(
                    "測試前倒數秒數",
                    10.0
                )
            )
        except (TypeError, ValueError):
            value = 10.0

        return max(0.0, min(value, 120.0))

    @property
    def settle_delay(self):
        try:
            value = float(
                getattr(self, "config", {}).get(
                    "方法後等待秒數",
                    0.35
                )
            )
        except (TypeError, ValueError):
            value = 0.35

        return max(0.05, min(value, 3.0))

    def _context(self):
        interaction = self.executor.interaction
        capture = getattr(interaction, "capture", None)
        hwnd_window = getattr(interaction, "hwnd_window", None)

        if hwnd_window is None:
            raise RuntimeError(
                "目前 interaction 沒有 hwnd_window，無法測試 foreground"
            )

        return interaction, capture, hwnd_window

    @staticmethod
    def _safe_int(value):
        try:
            if callable(value):
                value = value()
            if value is None:
                return 0
            return int(value)
        except Exception:
            return 0

    def _get_target_hwnd(self, hwnd_window):
        """
        ok-script 不同版本的 hwnd_window 欄位可能略有差異，
        依序嘗試常見欄位。
        """
        for name in (
            "hwnd",
            "real_hwnd",
            "top_hwnd",
            "window_handle",
            "handle",
        ):
            hwnd = self._safe_int(
                getattr(hwnd_window, name, 0)
            )
            if hwnd:
                break
        else:
            hwnd = 0

        if not hwnd:
            # 最後嘗試從目前 interaction 的 capture / device 物件找 real_hwnd。
            interaction = self.executor.interaction
            for obj in (
                interaction,
                getattr(interaction, "capture", None),
                getattr(self.executor, "device_manager", None),
            ):
                if obj is None:
                    continue
                for name in ("hwnd", "real_hwnd", "top_hwnd"):
                    hwnd = self._safe_int(
                        getattr(obj, name, 0)
                    )
                    if hwnd:
                        break
                if hwnd:
                    break

        if not hwnd:
            raise RuntimeError(
                "無法從 hwnd_window 取得 TBH HWND"
            )

        # 盡量轉成 top-level root window。
        try:
            root = ctypes.windll.user32.GetAncestor(
                hwnd,
                2  # GA_ROOT
            )
            if root:
                hwnd = int(root)
        except Exception:
            pass

        return hwnd

    @staticmethod
    def _title(hwnd):
        try:
            return win32gui.GetWindowText(hwnd)
        except Exception:
            return ""

    @staticmethod
    def _foreground_hwnd():
        try:
            return int(
                win32gui.GetForegroundWindow()
                or 0
            )
        except Exception:
            return 0

    def _is_target_foreground(self, target_hwnd):
        current = self._foreground_hwnd()

        if current == target_hwnd:
            return True

        # 容忍 child/root HWND 差異。
        try:
            current_root = int(
                ctypes.windll.user32.GetAncestor(
                    current,
                    2
                )
                or current
            )
            target_root = int(
                ctypes.windll.user32.GetAncestor(
                    target_hwnd,
                    2
                )
                or target_hwnd
            )
            return current_root == target_root
        except Exception:
            return False

    def _log_state(self, label, target_hwnd):
        fg = self._foreground_hwnd()

        self.log_info(
            f"[FG TEST] {label}: "
            f"target={target_hwnd} "
            f"'{self._title(target_hwnd)}' | "
            f"foreground={fg} "
            f"'{self._title(fg)}' | "
            f"TARGET={'YES' if self._is_target_foreground(target_hwnd) else 'NO'}"
        )

    def _run_method(
        self,
        label,
        target_hwnd,
        func
    ):
        if self._is_target_foreground(target_hwnd):
            self.log_info(
                f"[FG TEST] {label}: SKIP，"
                "TBH 在執行方法前就已經是 foreground，"
                "此結果無法判定該方法是否有效"
            )
            return None

        self.log_info(
            f"========== {label} =========="
        )
        self._log_state(
            "BEFORE",
            target_hwnd
        )

        api_result = None
        exception_text = None

        try:
            api_result = func()
        except Exception as exc:
            exception_text = repr(exc)

        self.sleep(
            self.settle_delay
        )

        success = self._is_target_foreground(
            target_hwnd
        )

        self._log_state(
            "AFTER",
            target_hwnd
        )

        self.log_info(
            f"[FG TEST] {label}: "
            f"{'SUCCESS' if success else 'FAIL'} | "
            f"api_result={api_result!r}"
            + (
                f" | exception={exception_text}"
                if exception_text
                else ""
            )
        )

        return success

    def _enabled_methods(self):
        config = getattr(
            self,
            "config",
            {}
        )

        selected = []

        for label, method_name in self.METHOD_OPTIONS:
            if bool(
                config.get(
                    label,
                    self.default_config.get(
                        label,
                        False
                    )
                )
            ):
                selected.append(
                    (
                        label,
                        method_name
                    )
                )

        return selected

    # =========================================================
    # Foreground methods
    # =========================================================

    def method_ok_bring_to_front(
        self,
        target_hwnd,
        hwnd_window,
        capture
    ):
        return hwnd_window.bring_to_front()

    def method_genshin_activate(
        self,
        target_hwnd,
        hwnd_window,
        capture
    ):
        if capture is None:
            raise RuntimeError(
                "capture=None，無法建立 GenshinInteraction"
            )

        interaction = GenshinInteraction(
            capture,
            hwnd_window
        )

        # 重要：只測 activate，不送任何 Genshin input。
        return interaction.activate()

    def method_restore_setforeground(
        self,
        target_hwnd,
        hwnd_window,
        capture
    ):
        win32gui.ShowWindow(
            target_hwnd,
            win32con.SW_RESTORE
        )
        return win32gui.SetForegroundWindow(
            target_hwnd
        )

    def method_bringtop_setforeground(
        self,
        target_hwnd,
        hwnd_window,
        capture
    ):
        win32gui.BringWindowToTop(
            target_hwnd
        )
        return win32gui.SetForegroundWindow(
            target_hwnd
        )

    def method_setwindowpos_top(
        self,
        target_hwnd,
        hwnd_window,
        capture
    ):
        win32gui.SetWindowPos(
            target_hwnd,
            win32con.HWND_TOP,
            0,
            0,
            0,
            0,
            win32con.SWP_NOMOVE
            | win32con.SWP_NOSIZE
            | win32con.SWP_SHOWWINDOW
        )

        return win32gui.SetForegroundWindow(
            target_hwnd
        )

    def _attach_thread_input_core(
        self,
        target_hwnd,
        use_alt=False
    ):
        user32 = ctypes.windll.user32
        kernel32 = ctypes.windll.kernel32

        foreground_hwnd = self._foreground_hwnd()

        target_tid, _target_pid = (
            win32process.GetWindowThreadProcessId(
                target_hwnd
            )
        )

        foreground_tid = 0
        if foreground_hwnd:
            foreground_tid, _foreground_pid = (
                win32process.GetWindowThreadProcessId(
                    foreground_hwnd
                )
            )

        current_tid = int(
            kernel32.GetCurrentThreadId()
        )

        attached_pairs = []

        def attach(a, b):
            if (
                a
                and b
                and a != b
            ):
                ok = bool(
                    user32.AttachThreadInput(
                        int(a),
                        int(b),
                        True
                    )
                )
                if ok:
                    attached_pairs.append(
                        (
                            int(a),
                            int(b)
                        )
                    )
                return ok
            return True

        try:
            attach(
                current_tid,
                foreground_tid
            )
            attach(
                current_tid,
                target_tid
            )
            attach(
                foreground_tid,
                target_tid
            )

            win32gui.ShowWindow(
                target_hwnd,
                win32con.SW_RESTORE
            )

            if use_alt:
                self._tap_alt()

            win32gui.BringWindowToTop(
                target_hwnd
            )

            sfw = win32gui.SetForegroundWindow(
                target_hwnd
            )

            try:
                win32gui.SetFocus(
                    target_hwnd
                )
            except Exception:
                pass

            return {
                "SetForegroundWindow": sfw,
                "attached_pairs": attached_pairs,
            }

        finally:
            for a, b in reversed(
                attached_pairs
            ):
                try:
                    user32.AttachThreadInput(
                        a,
                        b,
                        False
                    )
                except Exception:
                    pass

    def method_attach_thread_input(
        self,
        target_hwnd,
        hwnd_window,
        capture
    ):
        return self._attach_thread_input_core(
            target_hwnd,
            use_alt=False
        )

    @staticmethod
    def _tap_alt():
        # 使用 keybd_event，避免引入額外 dependency。
        win32api.keybd_event(
            win32con.VK_MENU,
            0,
            0,
            0
        )
        time.sleep(
            0.03
        )
        win32api.keybd_event(
            win32con.VK_MENU,
            0,
            win32con.KEYEVENTF_KEYUP,
            0
        )
        time.sleep(
            0.03
        )

    def method_alt_setforeground(
        self,
        target_hwnd,
        hwnd_window,
        capture
    ):
        self._tap_alt()

        win32gui.ShowWindow(
            target_hwnd,
            win32con.SW_RESTORE
        )

        return win32gui.SetForegroundWindow(
            target_hwnd
        )

    def method_attach_alt(
        self,
        target_hwnd,
        hwnd_window,
        capture
    ):
        return self._attach_thread_input_core(
            target_hwnd,
            use_alt=True
        )

    def method_topmost_pulse(
        self,
        target_hwnd,
        hwnd_window,
        capture
    ):
        flags = (
            win32con.SWP_NOMOVE
            | win32con.SWP_NOSIZE
            | win32con.SWP_SHOWWINDOW
        )

        win32gui.SetWindowPos(
            target_hwnd,
            win32con.HWND_TOPMOST,
            0,
            0,
            0,
            0,
            flags
        )

        time.sleep(
            0.05
        )

        win32gui.SetWindowPos(
            target_hwnd,
            win32con.HWND_NOTOPMOST,
            0,
            0,
            0,
            0,
            flags
        )

        return win32gui.SetForegroundWindow(
            target_hwnd
        )

    def method_switch_to_this_window(
        self,
        target_hwnd,
        hwnd_window,
        capture
    ):
        user32 = ctypes.windll.user32

        func = getattr(
            user32,
            "SwitchToThisWindow",
            None
        )

        if func is None:
            raise RuntimeError(
                "user32.SwitchToThisWindow 不存在"
            )

        return func(
            target_hwnd,
            True
        )

    def method_wscript_appactivate(
        self,
        target_hwnd,
        hwnd_window,
        capture
    ):
        _tid, pid = (
            win32process.GetWindowThreadProcessId(
                target_hwnd
            )
        )

        command = (
            "$ws = New-Object -ComObject WScript.Shell; "
            f"$r = $ws.AppActivate({int(pid)}); "
            "Write-Output $r"
        )

        completed = subprocess.run(
            [
                "powershell.exe",
                "-NoProfile",
                "-NonInteractive",
                "-ExecutionPolicy",
                "Bypass",
                "-Command",
                command,
            ],
            capture_output=True,
            text=True,
            timeout=5,
            creationflags=getattr(
                subprocess,
                "CREATE_NO_WINDOW",
                0
            ),
        )

        return {
            "returncode": completed.returncode,
            "stdout": completed.stdout.strip(),
            "stderr": completed.stderr.strip(),
            "pid": int(pid),
        }

    def method_wm_activate(
        self,
        target_hwnd,
        hwnd_window,
        capture
    ):
        # 軟啟用：刻意不呼叫 SetForegroundWindow。
        a = win32gui.SendMessage(
            target_hwnd,
            win32con.WM_NCACTIVATE,
            1,
            0
        )

        b = win32gui.SendMessage(
            target_hwnd,
            win32con.WM_ACTIVATE,
            1,  # WA_ACTIVE
            0
        )

        return {
            "WM_NCACTIVATE": a,
            "WM_ACTIVATE": b,
        }

    def method_allow_setforeground(
        self,
        target_hwnd,
        hwnd_window,
        capture
    ):
        user32 = ctypes.windll.user32

        # ASFW_ANY = -1 / 0xFFFFFFFF
        allowed = bool(
            user32.AllowSetForegroundWindow(
                ctypes.c_uint32(
                    0xFFFFFFFF
                ).value
            )
        )

        try:
            sfw = win32gui.SetForegroundWindow(
                target_hwnd
            )
        except Exception as exc:
            sfw = f"exception: {exc!r}"

        return {
            "AllowSetForegroundWindow": allowed,
            "SetForegroundWindow": sfw,
        }

    def method_unlock_setforeground(
        self,
        target_hwnd,
        hwnd_window,
        capture
    ):
        user32 = ctypes.windll.user32

        # LSFW_UNLOCK = 2
        unlocked = bool(
            user32.LockSetForegroundWindow(
                2
            )
        )

        try:
            sfw = win32gui.SetForegroundWindow(
                target_hwnd
            )
        except Exception as exc:
            sfw = f"exception: {exc!r}"

        return {
            "LockSetForegroundWindow(UNLOCK)": unlocked,
            "SetForegroundWindow": sfw,
        }

    def method_showwindowasync(
        self,
        target_hwnd,
        hwnd_window,
        capture
    ):
        user32 = ctypes.windll.user32

        shown = bool(
            user32.ShowWindowAsync(
                target_hwnd,
                win32con.SW_RESTORE
            )
        )

        time.sleep(
            0.05
        )

        try:
            sfw = win32gui.SetForegroundWindow(
                target_hwnd
            )
        except Exception as exc:
            sfw = f"exception: {exc!r}"

        return {
            "ShowWindowAsync": shown,
            "SetForegroundWindow": sfw,
        }

    # =========================================================
    # Main
    # =========================================================

    def run(self):
        _interaction, capture, hwnd_window = (
            self._context()
        )

        target_hwnd = self._get_target_hwnd(
            hwnd_window
        )

        self.log_info(
            "========== TBH FOREGROUND TEST START =========="
        )

        delay = self.pre_test_delay

        if delay > 0:
            self.log_info(
                f"[FG TEST] 測試前倒數 {delay:.1f} 秒。"
                "現在請切到 Chrome / Explorer / 其他程式，"
                "並在該視窗點擊或打字；倒數結束前不要再碰 TBH。"
            )

            deadline = time.monotonic() + delay
            last_reported = None

            while True:
                remaining = deadline - time.monotonic()

                if remaining <= 0:
                    break

                # 只在整秒變化時輸出，避免洗 log。
                report = int(remaining) + 1
                if report != last_reported:
                    self.log_info(
                        f"[FG TEST] {report} 秒後開始..."
                    )
                    last_reported = report

                self.sleep(
                    min(0.25, max(0.01, remaining))
                )

        self.log_info(
            "[FG TEST] 倒數結束，現在開始 foreground 測試"
        )

        self._log_state(
            "INITIAL",
            target_hwnd
        )

        if bool(
            getattr(
                self,
                "config",
                {}
            ).get(
                "要求開始時 TBH 在背景",
                True
            )
        ):
            if self._is_target_foreground(
                target_hwnd
            ):
                self.log_info(
                    "[FG TEST] TBH 目前已經是 foreground。"
                    "請先切到 Chrome / 記事本 / 其他視窗，"
                    "再重新執行此 TEST，否則無法判斷哪個方法真正有效。"
                )
                return False

        selected = self._enabled_methods()

        if not selected:
            self.log_info(
                "[FG TEST] 沒有勾選任何測試方法"
            )
            return False

        success_after_stop = bool(
            getattr(
                self,
                "config",
                {}
            ).get(
                "成功後停止",
                True
            )
        )

        results = []

        for label, method_name in selected:
            if self._is_target_foreground(
                target_hwnd
            ):
                self.log_info(
                    f"[FG TEST] {label}: "
                    "TBH 已由前一方法取得 foreground，"
                    "略過此方法以避免假陽性。"
                )
                results.append(
                    (
                        label,
                        "SKIP"
                    )
                )
                continue

            method = getattr(
                self,
                f"method_{method_name}"
            )

            result = self._run_method(
                label,
                target_hwnd,
                lambda method=method: method(
                    target_hwnd,
                    hwnd_window,
                    capture
                )
            )

            if result is True:
                results.append(
                    (
                        label,
                        "SUCCESS"
                    )
                )

                if success_after_stop:
                    break
            elif result is False:
                results.append(
                    (
                        label,
                        "FAIL"
                    )
                )
            else:
                results.append(
                    (
                        label,
                        "SKIP"
                    )
                )

        self.log_info(
            "========== TBH FOREGROUND TEST SUMMARY =========="
        )

        for label, result in results:
            self.log_info(
                f"{result:7} | {label}"
            )

        winners = [
            label
            for label, result in results
            if result == "SUCCESS"
        ]

        if winners:
            self.log_info(
                "WINNER = "
                + " / ".join(
                    winners
                )
            )
            return True

        self.log_info(
            "沒有任何已測方法讓 TBH 成為真正 foreground。"
        )
        return False
