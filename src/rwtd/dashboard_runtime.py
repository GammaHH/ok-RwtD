"""Bounded, owner-thread preview scheduling; never owns an Android client."""
import time
import uuid

PREVIEW_MODES = {"Android Preview", "Manual Device Test"}


class DashboardRuntime:
    def __init__(self, controller, restore_simulation, reopen, clock=time.monotonic):
        self.controller = controller
        self.restore_simulation, self.reopen, self.clock = restore_simulation, reopen, clock
        self.automatic = False
        self.full_recognition = False
        self.interval = 1.5
        self.initial = False
        self.due = 0
        self.closing = False
        self.idle = False

    def command(self, command, payload):
        if command == "mode":
            self.automatic = False
            self.initial = self.controller.mode in PREVIEW_MODES
            self.due = self.clock()
            if not self.initial:
                self.restore_simulation()
        elif command == "preview_settings":
            if (set(payload) - {'full_recognition'} != {"enabled", "interval_seconds"}
                    or type(payload["enabled"]) is not bool
                    or type(payload.get('full_recognition', False)) is not bool):
                raise ValueError("Use enabled boolean and interval_seconds")
            interval = payload["interval_seconds"]
            if type(interval) not in (int, float) or not .5 <= interval <= 30:
                raise ValueError("Preview interval must be 0.5..30 seconds")
            if payload["enabled"] and self.controller.mode not in PREVIEW_MODES:
                raise ValueError("Preview requires explicit Android Preview or Manual Device Test mode")
            self.automatic, self.interval = payload["enabled"], interval
            self.full_recognition = payload.get('full_recognition', False)
            self.due = self.clock()
        elif command == "dashboard_open":
            self.reopen()
        elif command == "dashboard_close":
            self.closing = True
            self.automatic = self.initial = False
        elif command == "stop":
            self.automatic = self.initial = False
        elif command == "pause_module":
            self.controller.module_pause.set()
        else:
            raise ValueError("Unknown Dashboard runtime command")
        return {"status": "READY"}

    def tick(self, last_seen):
        self.controller._owned()
        now = self.clock()
        self.idle = now - last_seen > 120
        if self.idle:
            self.automatic = self.initial = False
            self.controller.module_pause.set()
        if self.closing or self.idle or self.controller.capture_suppressed.is_set():
            return
        if self.controller.mode not in PREVIEW_MODES or not (self.initial or self.automatic) or now < self.due:
            return
        with self.controller.lock:
            if self.controller.device_pending or self.controller.capture_pending or self.controller.active or self.controller.queue:
                return
        try:
            self.controller.enqueue("preview-" + uuid.uuid4().hex, "refresh_frame",
                                    {'full_recognition': self.full_recognition})
            self.initial = False
            self.due = now + self.interval
        except ValueError:
            self.due = now + self.interval

    def snapshot(self):
        return {"auto_refresh": self.automatic, "refresh_interval_seconds": self.interval,
                "full_recognition": self.full_recognition,
                "browser_idle": self.idle, "closing": self.closing}
