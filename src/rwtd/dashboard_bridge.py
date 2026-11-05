"""Authenticated loopback UI. HTTP workers enqueue only; no device access here."""
import hmac
import json
import secrets
import threading
import time
import re
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlsplit, parse_qs


class DashboardBridge:
    def __init__(self, controller, assets, snapshot, image, max_command_bytes=32768,padding_recognizer=None,
                 embedded_parent_origin=None):
        if embedded_parent_origin is not None and not re.fullmatch(r'http://127\.0\.0\.1:[0-9]{1,5}', embedded_parent_origin):
            raise ValueError('Invalid embedded parent origin')
        self.controller, self.assets, self.snapshot_provider, self.image_provider = controller, assets, snapshot, image
        self.token = secrets.token_urlsafe(32)
        self.last_seen = time.monotonic()
        self.state_error = None
        self.workspace_roi_url = None
        self.workspace_calibration_url = None
        self.workspace_statistics_url = None
        self.workspace_host = None
        self.command_access = None
        self.embedded_frame_origin = None
        self.embedded_frame_origins = []
        self.padding_preview_lock=threading.Lock()
        bridge = self

        class Handler(BaseHTTPRequestHandler):
            def setup(self):
                super().setup()
                self.connection.settimeout(5)

            def log_message(self, *args):
                pass  # Never log authentication or request payloads.

            def send(self, status, data, kind="application/json"):
                self.send_response(status)
                self.send_header("Content-Type", kind)
                self.send_header("Content-Length", str(len(data)))
                self.send_header("Cache-Control", "no-store")
                self.send_header("X-Content-Type-Options", "nosniff")
                children = [bridge.embedded_frame_origin, *bridge.embedded_frame_origins]
                valid_children = dict.fromkeys(child for child in children if isinstance(child,str)
                    and re.fullmatch(r'http://127\.0\.0\.1:[0-9]{1,5}',child))
                frame_source = ' '.join(["'self'", *valid_children])
                ancestor = embedded_parent_origin or "'none'"
                self.send_header("Content-Security-Policy", "default-src 'self'; img-src 'self' blob:; script-src 'self'; style-src 'self'; frame-src "+frame_source+"; frame-ancestors "+ancestor+"; object-src 'none'")
                self.end_headers()
                self.wfile.write(data)

            def checked(self, authenticated=False, post=False):
                if self.headers.get("Host") != bridge.authority:
                    self.send(403, b'{"error":"Invalid Host"}')
                    return False
                origin = self.headers.get("Origin")
                if (post and origin != bridge.origin) or (origin and origin != bridge.origin):
                    self.send(403, b'{"error":"Invalid Origin"}')
                    return False
                if authenticated and not hmac.compare_digest(self.headers.get("Authorization", ""), "Bearer " + bridge.token):
                    self.send(401, b'{"error":"Unauthorized"}')
                    return False
                if authenticated:
                    bridge.last_seen = time.monotonic()
                return True

            def do_GET(self):
                path = urlsplit(self.path).path
                if not self.checked(authenticated=path.startswith("/api/")):
                    return
                if path in {"/", "/workspace_tabs.js", "/farm_workspace.js", "/roi_overview.js", "/dashboard.js", "/color_preview.js", "/diagnostics.js", "/canvas_model.js", "/canvas_view.js", "/workflow_ui.js", "/step_settings.js", "/target_editor.js", "/decision_editor.js", "/test_context.js", "/roi_editor.js", "/gesture_editor.js", "/dashboard.css"}:
                    name = "index.html" if path == "/" else path[1:]
                    kind = "text/html; charset=utf-8" if path == "/" else "text/css; charset=utf-8" if path == "/dashboard.css" else "text/javascript; charset=utf-8"
                    if not (bridge.assets / name).is_file():
                        self.send(404, b'{"error":"Not found"}')
                        return
                    self.send(200, (bridge.assets / name).read_bytes(), kind)
                elif path == '/api/workspace':
                    self.send(200,json.dumps({'roi_url':bridge.workspace_roi_url,
                        'calibration_url':bridge.workspace_calibration_url,
                        'statistics_url':bridge.workspace_statistics_url}).encode('utf-8'))
                elif path == "/api/state":
                    status, data = bridge.state_response()
                    self.send(status, data)
                elif path == '/api/workspace/state':
                    if bridge.workspace_host is None:
                        self.send(503,b'{"error":"Workspace unavailable"}')
                    else:
                        self.send(200,json.dumps(bridge.workspace_host.snapshot(),ensure_ascii=False).encode('utf-8'))
                elif path == "/api/image":
                    query = parse_qs(urlsplit(self.path).query)
                    which = query.get("which", ["frame"])[0]
                    report_id = query.get("report_id", [None])[0]
                    frame_id = parse_qs(urlsplit(self.path).query).get("frame_id", [None])[0]
                    if which in {"before", "after"} and report_id:
                        data = bridge.image_provider("evidence:" + report_id + ":" + which) if re.fullmatch(r"[A-Za-z0-9_]{1,100}", report_id) else None
                    elif which == "frame" and frame_id:
                        data = bridge.image_provider("frame:" + frame_id)
                    elif which == "reference":
                        feature = query.get('feature', [''])[0]
                        data = bridge.image_provider('reference:' + feature) if 1 <= len(feature) <= 100 else None
                    else:
                        data = bridge.image_provider(which) if which in {"frame", "before", "after", "source", "processed", "template"} else None
                    self.send(200 if data else 404, data or b'{"error":"Image unavailable"}', "image/png" if data else "application/json")
                else:
                    self.send(404, b'{"error":"Not found"}')

            def do_POST(self):
                if not self.checked(authenticated=True, post=True):
                    return
                path=urlsplit(self.path).path
                if path not in {"/api/command","/api/padding-preview","/api/workspace/command"}:
                    self.send(404, b'{"error":"Not found"}')
                    return
                try:
                    length = int(self.headers.get("Content-Length", "0"))
                    limit=8*1024*1024+32768 if path=='/api/padding-preview' else max_command_bytes
                    if not 0 < length <= limit or self.headers.get("Content-Type", "").split(";")[0] != "application/json":
                        raise ValueError("Invalid content type/length")
                    data = json.loads(self.rfile.read(length), parse_constant=lambda value: (_ for _ in ()).throw(ValueError("Nonfinite JSON value")))
                    if path=="/api/padding-preview":
                        from src.rwtd.android_core.padding_coco import preview,preview_image
                        if not bridge.padding_preview_lock.acquire(blocking=False):
                            raise ValueError('Pairing preview busy; wait for current request')
                        try:
                            if isinstance(data,dict) and 'image_png' in data:
                                if padding_recognizer is None:raise ValueError('Image preview recognizer unavailable')
                                result=preview_image(data,padding_recognizer)
                            else:result=preview(data)
                        finally:bridge.padding_preview_lock.release()
                        self.send(200,json.dumps(result,ensure_ascii=False).encode('utf-8'))
                        return
                    if not isinstance(data, dict) or set(data) != {"request_id", "command", "payload"}:
                        raise ValueError("Invalid command schema")
                    if path == '/api/workspace/command':
                        if bridge.workspace_host is None:
                            raise ValueError('Workspace unavailable')
                        result=bridge.workspace_host.enqueue(data['request_id'],data['command'],data['payload'])
                        self.send(202,json.dumps(result).encode('utf-8'))
                        return
                    if bridge.command_access:
                        bridge.command_access(data['command'])
                    result = bridge.controller.enqueue(data["request_id"], data["command"], data["payload"])
                    self.send(202, json.dumps(result).encode("utf-8"))
                except (ValueError, TypeError, KeyError) as exc:
                    self.send(400, json.dumps({"error": str(exc)}).encode("utf-8"))

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.server.daemon_threads = True
        self.authority = f"127.0.0.1:{self.server.server_port}"
        self.origin = "http://" + self.authority
        self.url = self.origin + "/#token=" + self.token
        self.thread = threading.Thread(target=lambda: self.server.serve_forever(poll_interval=.2), daemon=True, name="RwtD-loopback-dashboard")

    def state_response(self):
        """Keep state failures structured without exposing exception payloads."""
        try:
            data = json.dumps(self.snapshot_provider(), ensure_ascii=False, allow_nan=False).encode('utf-8')
            self.state_error = None
            return 200, data
        except Exception as exc:
            kind = type(exc).__name__
            if self.state_error != kind:
                self.state_error = kind
                self.controller._log('STATE_ERROR', 'Dashboard state unavailable (' + kind + ')')
            return 500, json.dumps({'error':'Dashboard state unavailable (' + kind + ')',
                                    'error_id':'DASHBOARD_STATE_FAILED'}, ensure_ascii=False).encode('utf-8')

    def start(self):
        self.thread.start()

    def close(self):
        if self.thread.is_alive():
            self.server.shutdown()
            self.thread.join(timeout=2)
        self.server.server_close()

