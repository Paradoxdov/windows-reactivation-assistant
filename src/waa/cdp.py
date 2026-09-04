"""Minimal Chrome DevTools Protocol client.

Chosen over Selenium/WebDriver on purpose: CDP talks straight to the installed
Microsoft Edge over a local WebSocket, so there is no msedgedriver.exe to keep
version-matched on every serviced PC.
"""

import json
import threading
import time

import websocket  # websocket-client


class CDPError(Exception):
    def __init__(self, message, method=None):
        self.method = method
        super().__init__(message)


class CDPClient(object):
    """One WebSocket connection to the browser, multiplexed by sessionId."""

    def __init__(self, ws_url, logger, timeout=45):
        self._logger = logger
        self._ws = websocket.create_connection(
            ws_url, timeout=timeout, enable_multithread=True,
            suppress_origin=True, skip_utf8_validation=True,
        )
        self._next_id = 0
        self._send_lock = threading.Lock()
        self._pending = {}
        self._pending_lock = threading.Lock()
        self._events = []
        self._events_lock = threading.Lock()
        self._closed = False
        self._reader = threading.Thread(target=self._read_loop, daemon=True)
        self._reader.start()

    # ------------------------------------------------------------------ io
    def _read_loop(self):
        while not self._closed:
            try:
                raw = self._ws.recv()
            except Exception as exc:
                if not self._closed:
                    self._fail_all(exc)
                return
            if not raw:
                continue
            try:
                message = json.loads(raw)
            except ValueError:
                continue
            if "id" in message:
                with self._pending_lock:
                    slot = self._pending.pop(message["id"], None)
                if slot is not None:
                    slot["result"] = message
                    slot["event"].set()
            else:
                with self._events_lock:
                    self._events.append(message)
                    if len(self._events) > 4000:
                        del self._events[:2000]

    def _fail_all(self, exc):
        with self._pending_lock:
            pending, self._pending = self._pending, {}
        for slot in pending.values():
            slot["result"] = {"error": {"message": "connection lost: %s" % exc}}
            slot["event"].set()

    def send(self, method, params=None, session_id=None, timeout=30):
        if self._closed:
            raise CDPError("CDP connection is closed", method)
        with self._send_lock:
            self._next_id += 1
            message_id = self._next_id
            payload = {"id": message_id, "method": method, "params": params or {}}
            if session_id:
                payload["sessionId"] = session_id
            slot = {"event": threading.Event(), "result": None}
            with self._pending_lock:
                self._pending[message_id] = slot
            try:
                self._ws.send(json.dumps(payload))
            except Exception as exc:
                with self._pending_lock:
                    self._pending.pop(message_id, None)
                raise CDPError("Failed to send %s: %s" % (method, exc), method)

        if not slot["event"].wait(timeout):
            with self._pending_lock:
                self._pending.pop(message_id, None)
            raise CDPError("Timed out waiting for %s" % method, method)

        response = slot["result"] or {}
        if "error" in response:
            raise CDPError("%s failed: %s" % (method, response["error"].get("message")), method)
        return response.get("result", {})

    # -------------------------------------------------------------- events
    def drain_events(self, method=None):
        with self._events_lock:
            events, self._events = self._events, []
        if method:
            return [e for e in events if e.get("method") == method]
        return events

    def wait_for_event(self, method, timeout=30, predicate=None):
        deadline = time.time() + timeout
        while time.time() < deadline:
            with self._events_lock:
                for index, event in enumerate(self._events):
                    if event.get("method") != method:
                        continue
                    if predicate and not predicate(event):
                        continue
                    return self._events.pop(index)
            time.sleep(0.05)
        return None

    def close(self):
        self._closed = True
        try:
            self._ws.close()
        except Exception:
            pass
