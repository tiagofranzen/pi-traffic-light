"""The web server: the page in web/, live status over SSE, and the action API."""
from __future__ import annotations
import os
import logging
import threading
from time import sleep
import json
from http.server import BaseHTTPRequestHandler, HTTPServer
from socketserver import ThreadingMixIn
from urllib.parse import urlparse, parse_qs
from typing import TYPE_CHECKING, Dict, Tuple
from .clock import CLOCK_QUIET_HOURS
if TYPE_CHECKING:
    from .controller import TrafficLightController
from .config import EMERGENCY_BLINK_INTERVAL, SSE_KEEPALIVE_SECONDS, WEB_SERVER_HOST, WEB_SERVER_PORT
from .weather import RAIN_RED_MINUTES, RAIN_YELLOW_MINUTES, TEMP_GREEN_RANGE, TEMP_YELLOW_RANGE

WEB_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), 'web')  # <repo>/web
WEB_TYPES = {'.html': 'text/html; charset=utf-8', '.css': 'text/css; charset=utf-8', '.js': 'text/javascript; charset=utf-8'}

def _web_config() -> str:
    """The Python settings the page needs, put into index.html as window.TL_CONFIG."""
    return json.dumps({
        'temp_green': TEMP_GREEN_RANGE, 'temp_yellow': TEMP_YELLOW_RANGE,
        'rain_yellow_hours': RAIN_YELLOW_MINUTES // 60, 'rain_red_minutes': RAIN_RED_MINUTES,
        'blink_seconds': EMERGENCY_BLINK_INTERVAL, 'quiet_hours': CLOCK_QUIET_HOURS,
    })

class _Handler(BaseHTTPRequestHandler):
    controller: TrafficLightController = None
    def log_message(self, fmt: str, *args) -> None:
        logging.debug(f"{self.address_string()} - {fmt % args}")
    def do_GET(self):
        p = urlparse(self.path)
        if p.path == '/status': self._status()
        elif p.path == '/events': self._events()
        elif p.path == '/': self._index(p)
        elif p.path.startswith('/static/'): self._static(p.path[len('/static/'):])
        else: self._err(404,'Not found')
    def _status(self):
        try:
            data = self.controller.get_status(); self._json(data)
        except Exception as e:
            logging.error(f"status error: {e}"); self._err(500,'error')
    def _events(self):
        self.send_response(200)
        self.send_header('Content-type', 'text/event-stream')
        self.send_header('Cache-Control', 'no-cache')
        self.send_header('Connection', 'keep-alive')
        self.end_headers()
        seen = -1
        _status_feed.subscribe(self.controller)
        try:
            while self.controller.state.running:
                # Sleeps until the status changes; a keepalive now and then, so an idle phone's radio can sleep.
                version, payload = _status_feed.wait_newer(seen, SSE_KEEPALIVE_SECONDS)
                if version == seen or not payload:
                    self.wfile.write(b": ping\n\n")
                else:
                    self.wfile.write(f"data: {payload}\n\n".encode()); seen = version
                self.wfile.flush()
        except OSError:  # browser gone: broken pipe, reset, timeout
            pass
        finally:
            _status_feed.unsubscribe()
    def _index(self,p):
        q = parse_qs(p.query); act = q.get('action',[None])[0]
        if act:
            try:
                if act=='set_color':
                    color = q.get('color',[''])[0];
                    if color: self.controller.set_manual_color(color)
                elif act=='set_mode':
                    mode = q.get('mode',[''])[0];
                    if mode: self.controller.set_mode(mode)
                elif act=='set_volume':
                    # value 0-100 (0 = mute), or mute=toggle; preview=1 rings one bell at the new level
                    raw = q.get('value',[None])[0]
                    volume = None if q.get('mute',[''])[0] == 'toggle' else float(raw) / 100.0
                    if volume is not None and not (0.0 <= volume <= 1.0): raise ValueError("value 0-100")
                    self.controller.set_chime_volume(volume, preview=q.get('preview',['0'])[0] == '1')
                elif act=='chime':
                    # Test a chime: quarter 1-3 = :15/:30/:45, 4 = full hour; hour 1-12.
                    quarter = int(q.get('quarter',['4'])[0]); hour = int(q.get('hour',['12'])[0])
                    if not (1 <= quarter <= 4 and 1 <= hour <= 12): raise ValueError("quarter 1-4, hour 1-12")
                    self.controller.test_chime(quarter, hour)
                elif act=='race_start':
                    self.controller.trigger_race_start()
                elif act=='party_record':
                    with self.controller.state.lock: self.controller.state.mode_state['party_record'] = True
                self.send_response(200); self.end_headers(); return
            except Exception as e:
                logging.warning(f"action error {act}: {e}"); self._err(400, str(e)); return
        with open(os.path.join(WEB_DIR, 'index.html'), encoding='utf-8') as f:
            self._send(f.read().replace('__TL_CONFIG__', _web_config()).encode(), WEB_TYPES['.html'])
    def _static(self, name: str):
        path = os.path.join(WEB_DIR, os.path.basename(name))  # basename: nothing outside web/
        ext = os.path.splitext(path)[1]
        if ext not in WEB_TYPES or not os.path.isfile(path):
            self._err(404, 'Not found'); return
        with open(path, 'rb') as f:
            self._send(f.read(), WEB_TYPES[ext])
    def _json(self, data: Dict):
        self.send_response(200); self.send_header('Content-type','application/json'); self.send_header('Cache-Control','no-cache'); self.end_headers(); self.wfile.write(json.dumps(data).encode())
    def _send(self, body: bytes, content_type: str):
        # no-cache: the browser re-checks each load, so a deploy shows up straight away.
        self.send_response(200); self.send_header('Content-type', content_type); self.send_header('Cache-Control', 'no-cache')
        self.send_header('Content-Length', str(len(body))); self.end_headers(); self.wfile.write(body)
    def _err(self, code: int, msg: str):
        self.send_response(code); self.send_header('Content-type','text/plain'); self.end_headers(); self.wfile.write(msg.encode())

class _ThreadingHTTPServer(ThreadingMixIn, HTTPServer):
    daemon_threads = True
    allow_reuse_address = True

class _StatusFeed:
    """Builds the status once every 50 ms for all browsers (instead of once per browser)
    while at least one browser is connected, and wakes the event streams only when it changed."""
    def __init__(self):
        self.cond = threading.Condition()
        self.has_clients = threading.Event()
        self.clients = 0
        self.version = 0
        self.payload = ''
    def subscribe(self, controller: TrafficLightController) -> None:
        payload = json.dumps(controller.get_status())
        with self.cond:
            if payload != self.payload:
                self.payload = payload; self.version += 1; self.cond.notify_all()
            self.clients += 1
            self.has_clients.set()
    def unsubscribe(self) -> None:
        with self.cond:
            self.clients = max(0, self.clients - 1)
            if self.clients == 0:
                self.has_clients.clear()
    def run(self, controller: TrafficLightController) -> None:
        while controller.state.running:
            if not self.has_clients.wait(timeout=1.0):
                continue
            payload = json.dumps(controller.get_status())
            if payload != self.payload:
                with self.cond:
                    self.payload = payload; self.version += 1; self.cond.notify_all()
            sleep(0.05)
    def wait_newer(self, seen: int, timeout: float) -> Tuple[int, str]:
        with self.cond:
            self.cond.wait_for(lambda: self.version != seen, timeout)
            return self.version, self.payload

_status_feed = _StatusFeed()

def _start_web(controller: TrafficLightController):
    _Handler.controller = controller
    threading.Thread(target=_status_feed.run, args=(controller,), name='status', daemon=True).start()
    srv = _ThreadingHTTPServer((WEB_SERVER_HOST, WEB_SERVER_PORT), _Handler)
    logging.info(f"Web server http://{WEB_SERVER_HOST}:{WEB_SERVER_PORT}")
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        srv.shutdown(); logging.info("Web server stopped")
