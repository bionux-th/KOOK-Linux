import json
import logging
import ssl
import threading
import time
import zlib
from typing import Callable, Optional

import websocket

logger = logging.getLogger(__name__)


class ChatGateway:
    def __init__(self, token: str):
        self.token = token
        self._ws: Optional[websocket.WebSocket] = None
        self._thread: Optional[threading.Thread] = None
        self._running = False
        self._sn = 0
        self._heartbeat_interval = 30
        self.on_message: Optional[Callable] = None
        self._last_recv = 0

    def connect(self, gateway_url: str):
        self._running = True
        def _run():
            while self._running:
                try:
                    self._connect_once(gateway_url)
                except Exception as e:
                    logger.warning("Gateway reconnect after: %s", e)
                    time.sleep(5)
        self._thread = threading.Thread(target=_run, daemon=True)
        self._thread.start()

    def _recv(self, ws) -> dict:
        raw = ws.recv()
        if isinstance(raw, bytes):
            return json.loads(zlib.decompress(raw))
        return json.loads(raw)

    def _connect_once(self, gateway_url: str):
        ws = websocket.create_connection(
            gateway_url,
            timeout=10,
            sslopt={"cert_reqs": ssl.CERT_NONE},
        )
        self._ws = ws

        hello = self._recv(ws)
        self._sn = hello.get("s", 0)
        logger.debug("Gateway HELLO, session=%s", hello.get("d", {}).get("sessionId", "?"))

        ws.settimeout(self._heartbeat_interval + 5)

        while self._running:
            try:
                data = self._recv(ws)
                self._sn = data.get("s", self._sn)
                self._last_recv = time.time()
                ev = data.get("d")
                if isinstance(ev, dict):
                    self._handle_event(ev)
            except websocket.WebSocketTimeoutException:
                self._send_heartbeat(ws)
                continue
            except Exception:
                break

        ws.close()
        self._ws = None

    def _send_heartbeat(self, ws):
        try:
            ws.send(json.dumps({"s": self._sn, "compress": 0}))
        except Exception:
            raise

    def _handle_event(self, ev: dict):
        channel_type = ev.get("channel_type")
        event_type = ev.get("type")
        body = ev.get("body", {})
        if channel_type == "TEXT" and event_type == 1:
            if self.on_message:
                self.on_message(body)
        elif event_type == 2:
            pass  # HEARTBEAT_ACK
        elif event_type == 3:
            pass  # RECONNECT

    def stop(self):
        self._running = False
        if self._ws:
            try:
                self._ws.close()
            except Exception:
                pass
            self._ws = None
