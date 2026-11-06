from __future__ import annotations
import logging
import os
import random
import time
from datetime import datetime
from typing import Any, Dict, List, Optional
import httpx

from app.policy import IST, get_ist_date_str, normalize_rider_id

logger = logging.getLogger("payswift")

DEFAULT_PAYSWIFT_URL = os.getenv("PAYSWIFT_BASE_URL", "http://localhost:8081").rstrip("/")

# Global in-memory ledger used when PAYSWIFT_MOCK=1 or in local test mode
_mock_payouts: List[Dict[str, Any]] = []
_mock_idempotency: Dict[str, Any] = {}


def reset_mock_payswift():
    global _mock_payouts, _mock_idempotency
    _mock_payouts.clear()
    _mock_idempotency.clear()


class PaySwiftClient:
    def __init__(self, base_url: Optional[str] = None, use_mock: Optional[bool] = None):
        self.base_url = (base_url or os.getenv("PAYSWIFT_BASE_URL", DEFAULT_PAYSWIFT_URL)).rstrip("/")
        self.use_mock = use_mock if use_mock is not None else (os.getenv("PAYSWIFT_MOCK", "0") in ("1", "true", "yes"))

    def health(self) -> bool:
        if self.use_mock:
            return True
        try:
            with httpx.Client(timeout=3.0) as client:
                res = client.get(f"{self.base_url}/health")
                return res.status_code == 200
        except Exception:
            return False

    def get_payouts(self, rider_id: Optional[str] = None) -> List[Dict[str, Any]]:
        norm_rider = normalize_rider_id(rider_id) if rider_id else None
        if self.use_mock:
            if norm_rider:
                return [p for p in _mock_payouts if p["rider_id"] == norm_rider]
            return list(_mock_payouts)

        url = f"{self.base_url}/v1/payouts"
        params = {}
        if norm_rider:
            params["rider_id"] = norm_rider

        for attempt in range(4):
            try:
                with httpx.Client(timeout=5.0) as client:
                    res = client.get(url, params=params)
                    if res.status_code == 200:
                        return res.json()
                    elif res.status_code == 429:
                        time.sleep(0.5 * (attempt + 1))
                        continue
                    elif res.status_code >= 500:
                        time.sleep(0.3 * (attempt + 1))
                        continue
            except (httpx.RequestError, httpx.TimeoutException) as exc:
                logger.warning(f"PaySwift GET /v1/payouts attempt {attempt+1} failed: {exc}")
                time.sleep(0.3 * (attempt + 1))
        return []

    def has_payout_today(self, rider_id: str, today_ist_date: str) -> bool:
        """
        Reconcile against PaySwift: checks if the rider already had a payout created today (in IST).
        Finance rule: 'once per rider per day'.
        """
        payouts = self.get_payouts(rider_id)
        for p in payouts:
            created_at = p.get("created_at")
            if created_at:
                try:
                    payout_date = get_ist_date_str(created_at)
                    if payout_date == today_ist_date:
                        return True
                except Exception:
                    continue
        return False

    def create_payout(
        self,
        rider_id: str,
        amount: int,
        reference: str,
        idempotency_key: Optional[str] = None
    ) -> Dict[str, Any]:
        """
        Execute payout with idempotency, exponential backoff, and retry handling for chaos modes.
        """
        norm_rider = normalize_rider_id(rider_id)

        if self.use_mock:
            import uuid
            if idempotency_key and idempotency_key in _mock_idempotency:
                return _mock_idempotency[idempotency_key]["response"]
            now_str = datetime.now(IST).strftime("%Y-%m-%dT%H:%M:%SZ")
            payout = {
                "payout_id": f"pout_{uuid.uuid4().hex[:12]}",
                "rider_id": norm_rider,
                "amount": int(amount),
                "reference": reference or f"Dispute resolution for {norm_rider}",
                "status": "processed",
                "created_at": now_str
            }
            _mock_payouts.append(payout)
            if idempotency_key:
                _mock_idempotency[idempotency_key] = {"response": payout}
            return payout

        url = f"{self.base_url}/v1/payouts"
        headers = {"Content-Type": "application/json"}
        if idempotency_key:
            headers["Idempotency-Key"] = idempotency_key

        payload = {
            "rider_id": norm_rider,
            "amount": int(amount),
            "reference": reference or f"Dispute resolution for {norm_rider}"
        }

        max_attempts = 5
        for attempt in range(max_attempts):
            try:
                with httpx.Client(timeout=6.0) as client:
                    res = client.post(url, json=payload, headers=headers)

                    if res.status_code == 201:
                        return res.json()

                    if res.status_code == 409:
                        # Idempotency conflict or request_in_progress
                        err_body = {}
                        try:
                            err_body = res.json()
                        except Exception:
                            pass
                        err_msg = err_body.get("message", "")
                        if "still being processed" in err_msg or "in_progress" in err_body.get("error", ""):
                            time.sleep(0.5 + 0.2 * attempt)
                            continue
                        # If key already used, check if it was processed
                        existing = self.get_payouts(norm_rider)
                        for ex in existing:
                            if ex.get("reference") == payload["reference"] and ex.get("amount") == payload["amount"]:
                                return ex
                        return {"status": "already_processed", "error": "idempotency_conflict", "details": err_body}

                    if res.status_code == 429:
                        # Rate limit / account temporarily blocked
                        sleep_time = 0.6 * (2 ** attempt) + random.uniform(0.1, 0.3)
                        time.sleep(sleep_time)
                        continue

                    if res.status_code >= 500:
                        # Chaos 5xx: service unavailable / gateway timeout
                        sleep_time = 0.3 * (2 ** attempt) + random.uniform(0.05, 0.2)
                        time.sleep(sleep_time)
                        continue

                    # 400 Bad request
                    try:
                        err_json = res.json()
                    except Exception:
                        err_json = {"text": res.text}
                    return {"status": "error", "code": res.status_code, "error": err_json}

            except (httpx.RequestError, httpx.TimeoutException) as exc:
                logger.warning(f"PaySwift POST attempt {attempt+1} error: {exc}")
                time.sleep(0.4 * (attempt + 1) + random.uniform(0.05, 0.15))

        # Check if payout actually reached PaySwift despite network failure (lost response chaos)
        existing = self.get_payouts(norm_rider)
        for ex in existing:
            if ex.get("reference") == payload["reference"] and ex.get("amount") == payload["amount"]:
                return ex

        return {"status": "failed", "error": "Max retries exceeded communicating with PaySwift"}
