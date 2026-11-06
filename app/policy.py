from __future__ import annotations
import math
from datetime import datetime, date
from typing import Optional, Union
import pytz

IST = pytz.timezone("Asia/Kolkata")

AUTO_PAY_LIMIT = 200
DAILY_INCENTIVE_THRESHOLD = 12
DAILY_INCENTIVE_AMOUNT = 150
CANCELLATION_PENALTY_AMOUNT = 10
DISPUTE_WINDOW_DAYS = 7


def normalize_rider_id(rider_id: str) -> str:
    """Normalize rider_id representations, e.g. 'r19' -> 'R019', 'R7' -> 'R007'."""
    if not rider_id:
        return ""
    r = str(rider_id).strip().upper()
    if r.startswith("R") and r[1:].isdigit():
        return f"R{int(r[1:]):03d}"
    return r


def normalize_distance(distance_km: float) -> float:
    """
    Handle data anomalies where distance was recorded in meters (e.g. 5000.0 instead of 5.0).
    """
    if distance_km is None:
        return 0.0
    d = float(distance_km)
    if d > 100.0:
        return d / 1000.0
    return d


def round_half_up(val: float) -> int:
    """Round to nearest rupee, 0.5 rounds up."""
    return int(math.floor(val + 0.5))


def calculate_trip_fare(distance_km: float, surge_multiplier: Optional[float] = 1.0) -> int:
    """
    Policy:
    - ₹25 base (first 2 km)
    - ₹6 for each km after first 2 km
    - surge applies to whole trip fare (base + distance)
    - round each trip to nearest rupee, 0.5 rounds up
    """
    norm_dist = normalize_distance(distance_km)
    if norm_dist <= 2.0:
        base = 25.0
    else:
        base = 25.0 + (norm_dist - 2.0) * 6.0

    surge = float(surge_multiplier) if surge_multiplier and surge_multiplier > 0 else 1.0
    total = base * surge
    return round_half_up(total)


def parse_to_ist(dt_input: Union[str, datetime]) -> datetime:
    """Convert any ISO timestamp or datetime to IST timezone aware datetime."""
    if isinstance(dt_input, str):
        # Handle trailing Z or offsets
        dt_str = dt_input.replace("Z", "+00:00")
        dt = datetime.fromisoformat(dt_str)
    else:
        dt = dt_input

    if dt.tzinfo is None:
        # If naive, assume UTC or IST? Trips timestamps are like 2026-09-19T12:30:00Z
        dt = pytz.utc.localize(dt)
    return dt.astimezone(IST)


def get_ist_date_str(dt_input: Union[str, datetime, date]) -> str:
    """Get YYYY-MM-DD in IST."""
    if isinstance(dt_input, date) and not isinstance(dt_input, datetime):
        return dt_input.isoformat()
    dt_ist = parse_to_ist(dt_input)
    return dt_ist.strftime("%Y-%m-%d")


def is_within_dispute_window(received_at: Union[str, datetime], dispute_date: Union[str, datetime, date]) -> bool:
    """
    Policy: Only disputes within the last 7 days are considered.
    'Day' means calendar day in IST.
    """
    rec_date_str = get_ist_date_str(received_at)
    disp_date_str = get_ist_date_str(dispute_date) if isinstance(dispute_date, (str, datetime)) and "T" in str(dispute_date) else str(dispute_date)[:10]

    rec_d = date.fromisoformat(rec_date_str)
    disp_d = date.fromisoformat(disp_date_str)

    delta_days = (rec_d - disp_d).days
    return 0 <= delta_days <= DISPUTE_WINDOW_DAYS
