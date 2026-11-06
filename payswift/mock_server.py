from __future__ import annotations
import json
import os
import random
import time
import uuid
from collections import deque
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from threading import Lock
from urllib.parse import parse_qs, urlparse

PORT = int(os.getenv("PORT", "8081"))
CHAOS = os.getenv("PROVIDER_CHAOS", "0").lower() in ("1", "true", "yes")
ERROR_RATE = float(os.getenv("PROVIDER_ERROR_RATE", "0.15"))
SLOW_RATE = float(os.getenv("PROVIDER_SLOW_RATE", "0.10"))
SLOW_SECONDS = float(os.getenv("PROVIDER_SLOW_SECONDS", "1.5"))
LOST_RESPONSE_RATE = float(os.getenv("PROVIDER_LOST_RESPONSE_RATE", "0.05"))

RATE_LIMIT_WINDOW = 60.0
RATE_LIMIT_REQUESTS = 30
BLOCK_SECONDS = 10.0

_lock = Lock()
_payouts = []
_idempotency = {}
_inflight = set()
_recent = deque()
_blocked_until = 0.0
_outage_until = 0.0


def _now():
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


class Handler(BaseHTTPRequestHandler):
    server_version = "PaySwift/1.4"

    def log_message(self, format, *args):
        # Clean logging
        print(f"[payswift] {self.address_string()} - {format % args}", flush=True)

    def _send(self, status: int, body: dict):
        data = json.dumps(body).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _read_json(self):
        length = int(self.headers.get("Content-Length", 0))
        if length <= 0:
            return None
        raw = self.rfile.read(length)
        return json.loads(raw.decode("utf-8"))

    def do_GET(self):
        parsed = urlparse(self.path)
        path = parsed.path
        query = parse_qs(parsed.query)

        if path == "/health":
            self._send(200, {"status": "ok"})
            return

        if path == "/v1/payouts":
            rider = query.get("rider_id", [None])[0]
            with _lock:
                if rider:
                    items = [p for p in _payouts if p["rider_id"] == rider]
                else:
                    items = list(_payouts)
            self._send(200, items)
            return

        self._send(404, {"error": "not_found"})

    def do_POST(self):
        global _blocked_until, _outage_until, _payouts, _idempotency, _recent

        parsed = urlparse(self.path)
        path = parsed.path

        if path == "/admin/reset":
            with _lock:
                _payouts.clear()
                _idempotency.clear()
                _inflight.clear()
                _recent.clear()
            self._send(200, {"status": "reset"})
            return

        if path == "/admin/outage":
            body = self._read_json() or {}
            seconds = float(body.get("seconds", 5))
            with _lock:
                _outage_until = time.time() + seconds
            self._send(200, {"outage_for_seconds": seconds})
            return

        if path == "/v1/payouts":
            now = time.time()
            with _lock:
                if now < _outage_until:
                    self._send(503, {"error": "service_unavailable", "message": "Service outage in progress"})
                    return

                if now < _blocked_until:
                    self._send(429, {"error": "account_blocked", "message": "Too many requests. Account temporarily blocked."})
                    return

                _recent.append(now)
                while _recent and _recent[0] < now - RATE_LIMIT_WINDOW:
                    _recent.popleft()
                if len(_recent) > RATE_LIMIT_REQUESTS:
                    _blocked_until = now + BLOCK_SECONDS
                    self._send(429, {"error": "account_blocked", "message": "Too many requests. Account temporarily blocked."})
                    return

            try:
                body = self._read_json()
            except Exception:
                self._send(400, {"error": "invalid_json"})
                return

            if not body or not isinstance(body, dict):
                self._send(400, {"error": "invalid_json"})
                return

            rider_id = body.get("rider_id")
            amount = body.get("amount")
            reference = body.get("reference", "")

            if not rider_id or not isinstance(rider_id, str):
                self._send(400, {"error": "invalid_rider_id"})
                return

            if not isinstance(amount, int) or isinstance(amount, bool) or amount < 1 or amount > 10000:
                self._send(400, {"error": "invalid_amount", "message": "amount must be an integer between 1 and 10000"})
                return

            idem = self.headers.get("Idempotency-Key")

            if CHAOS:
                if random.random() < ERROR_RATE:
                    self._send(503, {"error": "service_unavailable", "message": "Temporary upstream failure"})
                    return
                if random.random() < SLOW_RATE:
                    time.sleep(SLOW_SECONDS)

            with _lock:
                if idem:
                    fingerprint = json.dumps({"rider_id": rider_id, "amount": amount, "reference": reference}, sort_keys=True)
                    if idem in _inflight:
                        self._send(409, {"error": "request_in_progress", "message": "A request with this Idempotency-Key is still being processed. Retry shortly."})
                        return

                    if idem in _idempotency:
                        stored = _idempotency[idem]
                        if stored["fingerprint"] != fingerprint:
                            self._send(409, {"error": "idempotency_key_reused", "message": "Idempotency-Key was used with a different request body"})
                            return
                        self._send(201, stored["response"])
                        return

                    _inflight.add(idem)

            payout_id = f"pout_{uuid.uuid4().hex[:12]}"
            created_at = _now()
            payout = {
                "payout_id": payout_id,
                "rider_id": rider_id,
                "amount": amount,
                "reference": reference,
                "status": "processed",
                "created_at": created_at
            }

            with _lock:
                _payouts.append(payout)
                if idem:
                    _inflight.discard(idem)
                    _idempotency[idem] = {
                        "fingerprint": fingerprint,
                        "response": payout
                    }

            if CHAOS and random.random() < LOST_RESPONSE_RATE:
                # drop response
                return

            self._send(201, payout)
            return

        self._send(404, {"error": "not_found"})


def run():
    server = ThreadingHTTPServer(("0.0.0.0", PORT), Handler)
    print(f"PaySwift sandbox listening on :{PORT} (chaos={CHAOS})", flush=True)
    server.serve_forever()


if __name__ == "__main__":
    run()

