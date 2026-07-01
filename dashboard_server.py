"""
dashboard_server.py
====================
Serves dashboard.html over HTTP with a username/password gate (HTTP Basic
Auth), so the live trading dashboard can be safely exposed to the internet
through a tunnel (e.g. Cloudflare Tunnel) without anyone who finds the URL
being able to see your account balance / open positions / trade history.

generate_dashboard.py (run with --watch) keeps overwriting dashboard.html in
this same folder; this server just serves whatever is currently on disk, so
viewers always see the latest snapshot on refresh.

Credentials come from strategy_config.json -> "dashboard_auth": {"username":
..., "password": ...}. If that section is missing/blank, the server refuses
to start (so a dashboard showing real money never accidentally goes out
unprotected) — set it from the "Dashboard Web Access" fields in
strategy_config_ui.py, or edit strategy_config.json directly.

Run:
    python dashboard_server.py            # binds 0.0.0.0:8787
    python dashboard_server.py --port 9000
"""

import argparse
import base64
import hmac
import json
import os
import sys
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
try:
    import account_collector as _ac
except ImportError:
    _ac = None

THIS_DIR = os.path.dirname(os.path.abspath(__file__))
CONFIG_PATH = os.path.join(THIS_DIR, "strategy_config.json")
DASHBOARD_PATH = os.path.join(THIS_DIR, "dashboard.html")


def load_auth():
    if not os.path.exists(CONFIG_PATH):
        return None, None
    try:
        with open(CONFIG_PATH, "r", encoding="utf-8") as f:
            cfg = json.load(f)
    except Exception:
        return None, None
    auth = cfg.get("dashboard_auth", {})
    return auth.get("username") or None, auth.get("password") or None


class AuthHandler(BaseHTTPRequestHandler):
    username = None
    password = None

    def _unauthorized(self):
        self.send_response(401)
        self.send_header("WWW-Authenticate", 'Basic realm="XAUUSD EA Dashboard"')
        self.send_header("Content-Type", "text/plain; charset=utf-8")
        self.end_headers()
        self.wfile.write(b"401 Unauthorized")

    def _check_auth(self):
        header = self.headers.get("Authorization")
        if not header or not header.startswith("Basic "):
            return False
        try:
            decoded = base64.b64decode(header[6:]).decode("utf-8")
            user, _, pwd = decoded.partition(":")
        except Exception:
            return False
        return hmac.compare_digest(user, self.username) and hmac.compare_digest(pwd, self.password)

    def do_POST(self):
        if not self._check_auth():
            self._unauthorized()
            return
        parsed = urllib.parse.urlparse(self.path)
        if parsed.path == "/api/clear_account_history":
            params = urllib.parse.parse_qs(parsed.query)
            label  = (params.get("label") or [None])[0]
            if not label:
                body = json.dumps({"ok": False, "error": "missing label"}).encode()
            elif _ac is None:
                body = json.dumps({"ok": False, "error": "account_collector not available"}).encode()
            else:
                ok   = _ac.clear_account_history(label)
                body = json.dumps({"ok": ok}).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)
        else:
            self.send_response(404)
            self.end_headers()

    def do_GET(self):
        if not self._check_auth():
            self._unauthorized()
            return

        if self.path not in ("/", "/dashboard.html"):
            self.send_response(404)
            self.end_headers()
            return

        if not os.path.exists(DASHBOARD_PATH):
            self.send_response(503)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Retry-After", "30")
            self.end_headers()
            self.wfile.write(
                "<html><body><h2>ยังไม่มี dashboard.html — "
                "รอ generate_dashboard.py --watch สร้างไฟล์รอบแรก</h2></body></html>".encode("utf-8")
            )
            return

        with open(DASHBOARD_PATH, "rb") as f:
            content = f.read()
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(content)

    def log_message(self, fmt, *args):
        # Quieter than the default (which logs every request to stderr).
        print(f"[dashboard_server] {self.client_address[0]} - {fmt % args}")


def main():
    parser = argparse.ArgumentParser(description="Serve dashboard.html with HTTP Basic Auth.")
    parser.add_argument("--port", type=int, default=8787)
    parser.add_argument("--bind", default="0.0.0.0")
    args = parser.parse_args()

    username, password = load_auth()
    if not username or not password:
        print(
            "ERROR: ยังไม่ได้ตั้งรหัสผ่าน dashboard — เพิ่ม \"dashboard_auth\": "
            '{"username": "...", "password": "..."} ใน strategy_config.json '
            '(หรือกรอกในแท็บ "Dashboard Web Access" ของ strategy_config_ui.py) แล้วรันใหม่.'
        )
        raise SystemExit(1)

    AuthHandler.username = username
    AuthHandler.password = password

    server = ThreadingHTTPServer((args.bind, args.port), AuthHandler)
    print(f"Dashboard server running: http://{args.bind}:{args.port}/  (Basic Auth required)")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
