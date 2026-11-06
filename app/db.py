from __future__ import annotations
import csv
import json
import os
import sqlite3
import uuid
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional
import pytz

from app.policy import IST, get_ist_date_str, normalize_distance, normalize_rider_id

DATA_DIR = Path(__file__).resolve().parent.parent / "data"
DB_PATH = os.getenv("SQLITE_DB_PATH", str(Path(__file__).resolve().parent.parent / "dispute_desk.db"))


def get_connection() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def init_db(force_reseed: bool = False):
    conn = get_connection()
    cursor = conn.cursor()

    cursor.execute("""
    CREATE TABLE IF NOT EXISTS riders (
        rider_id TEXT PRIMARY KEY,
        name TEXT,
        city TEXT,
        joined_on TEXT
    )
    """)

    cursor.execute("""
    CREATE TABLE IF NOT EXISTS trips (
        trip_id TEXT PRIMARY KEY,
        rider_id TEXT,
        started_at TEXT,
        status TEXT,
        distance_km REAL,
        surge_multiplier REAL,
        date_ist TEXT
    )
    """)
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_trips_rider_date ON trips(rider_id, date_ist)")

    cursor.execute("""
    CREATE TABLE IF NOT EXISTS payout_lines (
        line_id TEXT PRIMARY KEY,
        payout_date TEXT,
        rider_id TEXT,
        line_type TEXT,
        trip_id TEXT,
        amount INTEGER
    )
    """)
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_payout_rider_date ON payout_lines(rider_id, payout_date)")
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_payout_trip ON payout_lines(trip_id)")

    cursor.execute("""
    CREATE TABLE IF NOT EXISTS messages (
        message_id TEXT PRIMARY KEY,
        rider_id TEXT,
        text TEXT,
        received_at TEXT,
        reply TEXT,
        created_at TEXT
    )
    """)

    cursor.execute("""
    CREATE TABLE IF NOT EXISTS traces (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        rider_id TEXT,
        at TEXT,
        type TEXT,
        name TEXT,
        input_json TEXT,
        output_json TEXT
    )
    """)
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_traces_rider ON traces(rider_id)")

    cursor.execute("""
    CREATE TABLE IF NOT EXISTS ops_pending (
        id TEXT PRIMARY KEY,
        rider_id TEXT,
        type TEXT,
        amount INTEGER,
        reason TEXT,
        status TEXT DEFAULT 'pending',
        created_at TEXT,
        actioned_at TEXT
    )
    """)

    conn.commit()

    # Seed data if empty or forced
    cursor.execute("SELECT COUNT(*) FROM riders")
    count = cursor.fetchone()[0]
    if count == 0 or force_reseed:
        seed_data(conn)

    conn.close()


def seed_data(conn: sqlite3.Connection):
    cursor = conn.cursor()
    cursor.execute("DELETE FROM riders")
    cursor.execute("DELETE FROM trips")
    cursor.execute("DELETE FROM payout_lines")

    riders_file = DATA_DIR / "riders.csv"
    if riders_file.exists():
        with open(riders_file, mode="r", encoding="utf-8") as f:
            reader = csv.DictReader(f)
            riders_data = [
                (normalize_rider_id(r["rider_id"]), r["name"], r["city"], r["joined_on"])
                for r in reader
            ]
            cursor.executemany("INSERT OR REPLACE INTO riders VALUES (?, ?, ?, ?)", riders_data)

    trips_file = DATA_DIR / "trips.csv"
    if trips_file.exists():
        with open(trips_file, mode="r", encoding="utf-8") as f:
            reader = csv.DictReader(f)
            trips_data = []
            seen_trips = set()
            for r in reader:
                tid = r["trip_id"]
                if tid in seen_trips:
                    continue  # drop duplicate raw logs
                seen_trips.add(tid)
                date_ist = get_ist_date_str(r["started_at"])
                dist = normalize_distance(float(r["distance_km"]))
                surge = float(r["surge_multiplier"]) if r["surge_multiplier"] else 1.0
                trips_data.append((
                    tid,
                    normalize_rider_id(r["rider_id"]),
                    r["started_at"],
                    r["status"],
                    dist,
                    surge,
                    date_ist
                ))
            cursor.executemany("INSERT OR REPLACE INTO trips VALUES (?, ?, ?, ?, ?, ?, ?)", trips_data)

    payouts_file = DATA_DIR / "payout_lines.csv"
    if payouts_file.exists():
        with open(payouts_file, mode="r", encoding="utf-8") as f:
            reader = csv.DictReader(f)
            payouts_data = [
                (
                    r["line_id"],
                    r["payout_date"],
                    normalize_rider_id(r["rider_id"]),
                    r["line_type"],
                    r["trip_id"] if r["trip_id"] else None,
                    int(r["amount"])
                )
                for r in reader
            ]
            cursor.executemany("INSERT OR REPLACE INTO payout_lines VALUES (?, ?, ?, ?, ?, ?)", payouts_data)

    conn.commit()


def reset_runtime_state():
    """Reset messages, traces, and pending ops for testing/eval isolation."""
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute("DELETE FROM messages")
    cursor.execute("DELETE FROM traces")
    cursor.execute("DELETE FROM ops_pending")
    conn.commit()
    conn.close()


def get_rider(rider_id: str) -> Optional[Dict[str, Any]]:
    conn = get_connection()
    c = conn.cursor()
    c.execute("SELECT * FROM riders WHERE rider_id = ?", (normalize_rider_id(rider_id),))
    row = c.fetchone()
    conn.close()
    return dict(row) if row else None


def get_trip(trip_id: str) -> Optional[Dict[str, Any]]:
    conn = get_connection()
    c = conn.cursor()
    c.execute("SELECT * FROM trips WHERE trip_id = ?", (trip_id,))
    row = c.fetchone()
    conn.close()
    return dict(row) if row else None


def get_trips_for_rider_date(rider_id: str, date_ist: str) -> List[Dict[str, Any]]:
    conn = get_connection()
    c = conn.cursor()
    c.execute(
        "SELECT * FROM trips WHERE rider_id = ? AND date_ist = ? ORDER BY started_at ASC",
        (normalize_rider_id(rider_id), date_ist)
    )
    rows = c.fetchall()
    conn.close()
    return [dict(r) for r in rows]


def get_trips_for_rider(rider_id: str) -> List[Dict[str, Any]]:
    conn = get_connection()
    c = conn.cursor()
    c.execute(
        "SELECT * FROM trips WHERE rider_id = ? ORDER BY started_at DESC",
        (normalize_rider_id(rider_id),)
    )
    rows = c.fetchall()
    conn.close()
    return [dict(r) for r in rows]


def get_payouts_for_rider_date(rider_id: str, date_str: str) -> List[Dict[str, Any]]:
    conn = get_connection()
    c = conn.cursor()
    c.execute(
        "SELECT * FROM payout_lines WHERE rider_id = ? AND payout_date = ?",
        (normalize_rider_id(rider_id), date_str)
    )
    rows = c.fetchall()
    conn.close()
    return [dict(r) for r in rows]


def get_payouts_for_rider(rider_id: str) -> List[Dict[str, Any]]:
    conn = get_connection()
    c = conn.cursor()
    c.execute(
        "SELECT * FROM payout_lines WHERE rider_id = ? ORDER BY payout_date DESC",
        (normalize_rider_id(rider_id),)
    )
    rows = c.fetchall()
    conn.close()
    return [dict(r) for r in rows]


def get_payouts_for_trip(trip_id: str) -> List[Dict[str, Any]]:
    conn = get_connection()
    c = conn.cursor()
    c.execute("SELECT * FROM payout_lines WHERE trip_id = ?", (trip_id,))
    rows = c.fetchall()
    conn.close()
    return [dict(r) for r in rows]


def record_message(message_id: str, rider_id: str, text: str, received_at: str, reply: str):
    conn = get_connection()
    c = conn.cursor()
    now_iso = datetime.now(IST).isoformat()
    c.execute(
        "INSERT OR REPLACE INTO messages VALUES (?, ?, ?, ?, ?, ?)",
        (message_id, normalize_rider_id(rider_id), text, received_at, reply, now_iso)
    )
    conn.commit()
    conn.close()


def get_message(message_id: str) -> Optional[Dict[str, Any]]:
    conn = get_connection()
    c = conn.cursor()
    c.execute("SELECT * FROM messages WHERE message_id = ?", (message_id,))
    row = c.fetchone()
    conn.close()
    return dict(row) if row else None


def get_recent_messages(rider_id: str, limit: int = 10) -> List[Dict[str, Any]]:
    conn = get_connection()
    c = conn.cursor()
    c.execute(
        "SELECT * FROM messages WHERE rider_id = ? ORDER BY received_at ASC",
        (normalize_rider_id(rider_id),)
    )
    rows = c.fetchall()
    conn.close()
    return [dict(r) for r in rows]


def record_trace(rider_id: str, at: str, type_: str, name: str, input_data: Any, output_data: Any):
    conn = get_connection()
    c = conn.cursor()
    c.execute(
        "INSERT INTO traces (rider_id, at, type, name, input_json, output_json) VALUES (?, ?, ?, ?, ?, ?)",
        (
            normalize_rider_id(rider_id),
            at,
            type_,
            name,
            json.dumps(input_data) if input_data is not None else None,
            json.dumps(output_data) if output_data is not None else None
        )
    )
    conn.commit()
    conn.close()


def get_traces_for_rider(rider_id: str) -> List[Dict[str, Any]]:
    conn = get_connection()
    c = conn.cursor()
    c.execute(
        "SELECT at, type, name, input_json, output_json FROM traces WHERE rider_id = ? ORDER BY id ASC",
        (normalize_rider_id(rider_id),)
    )
    rows = c.fetchall()
    conn.close()
    result = []
    for r in rows:
        result.append({
            "at": r["at"],
            "type": r["type"],
            "name": r["name"],
            "input": json.loads(r["input_json"]) if r["input_json"] is not None else None,
            "output": json.loads(r["output_json"]) if r["output_json"] is not None else None
        })
    return result


def create_ops_item(rider_id: str, type_: str, amount: Optional[int], reason: str) -> str:
    conn = get_connection()
    c = conn.cursor()
    item_id = f"ops_{uuid.uuid4().hex[:10]}"
    now_iso = datetime.now(IST).isoformat()
    c.execute(
        "INSERT INTO ops_pending (id, rider_id, type, amount, reason, status, created_at) VALUES (?, ?, ?, ?, ?, 'pending', ?)",
        (item_id, normalize_rider_id(rider_id), type_, amount, reason, now_iso)
    )
    conn.commit()
    conn.close()
    return item_id


def get_pending_ops_items() -> List[Dict[str, Any]]:
    conn = get_connection()
    c = conn.cursor()
    c.execute("SELECT id, rider_id, type, amount, reason, created_at FROM ops_pending WHERE status = 'pending' ORDER BY created_at ASC")
    rows = c.fetchall()
    conn.close()
    return [dict(r) for r in rows]


def get_all_ops_items() -> List[Dict[str, Any]]:
    conn = get_connection()
    c = conn.cursor()
    c.execute("SELECT * FROM ops_pending ORDER BY created_at DESC")
    rows = c.fetchall()
    conn.close()
    return [dict(r) for r in rows]


def get_ops_item(item_id: str) -> Optional[Dict[str, Any]]:
    conn = get_connection()
    c = conn.cursor()
    c.execute("SELECT * FROM ops_pending WHERE id = ?", (item_id,))
    row = c.fetchone()
    conn.close()
    return dict(row) if row else None


def update_ops_item_status(item_id: str, status: str) -> bool:
    conn = get_connection()
    c = conn.cursor()
    now_iso = datetime.now(IST).isoformat()
    c.execute("UPDATE ops_pending SET status = ?, actioned_at = ? WHERE id = ?", (status, now_iso, item_id))
    conn.commit()
    updated = c.rowcount > 0
    conn.close()
    return updated
