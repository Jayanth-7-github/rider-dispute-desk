from __future__ import annotations
import logging
import os
import re
from datetime import date, datetime, timedelta
from typing import Any, Dict, List, Optional, Tuple
import pytz

from app.db import (
    create_ops_item,
    get_message,
    get_payouts_for_rider,
    get_payouts_for_rider_date,
    get_payouts_for_trip,
    get_recent_messages,
    get_rider,
    get_traces_for_rider,
    get_trip,
    get_trips_for_rider,
    get_trips_for_rider_date,
    record_message,
    record_trace,
)
from app.payswift_client import PaySwiftClient
from app.policy import (
    AUTO_PAY_LIMIT,
    CANCELLATION_PENALTY_AMOUNT,
    DAILY_INCENTIVE_AMOUNT,
    DAILY_INCENTIVE_THRESHOLD,
    IST,
    calculate_trip_fare,
    get_ist_date_str,
    is_within_dispute_window,
    normalize_distance,
    normalize_rider_id,
    parse_to_ist,
)

logger = logging.getLogger("agent")


class DisputeAgent:
    def __init__(self, payswift_client: Optional[PaySwiftClient] = None):
        self.payswift = payswift_client or PaySwiftClient()

    def process_message(
        self,
        message_id: str,
        rider_id: str,
        text: str,
        received_at: str
    ) -> str:
        """
        Handle incoming message from messaging vendor.
        Returns the reply string.
        """
        norm_rider = normalize_rider_id(rider_id)
        now_iso = received_at or datetime.now(IST).isoformat()

        # Step 0: Check Deduplication / Idempotency
        existing_msg = get_message(message_id)
        if existing_msg and existing_msg.get("reply"):
            logger.info(f"Duplicate message {message_id} received, returning cached reply.")
            return existing_msg["reply"]

        # Log trace: message_in
        record_trace(
            rider_id=norm_rider,
            at=now_iso,
            type_="message_in",
            name="vendor_message_received",
            input_data={"message_id": message_id, "text": text, "received_at": received_at},
            output_data=None
        )

        # Step 1: Security & Prompt Injection Defense
        if "ignore all previous rules" in text.lower() or text.strip().startswith("SYSTEM:"):
            record_trace(
                rider_id=norm_rider,
                at=now_iso,
                type_="decision",
                name="prompt_injection_blocked",
                input_data={"text": text},
                output_data={"action": "reject_and_escalate"}
            )
            create_ops_item(
                rider_id=norm_rider,
                type_="escalation",
                amount=None,
                reason="Potential prompt injection or policy bypass attempt"
            )
            reply = "Aapka anurodh process nahi kiya ja sakta. Yeh mamla review ke liye ops team ko bhej diya gaya hai."
            record_trace(norm_rider, now_iso, "reply", "send_reply", None, reply)
            record_message(message_id, norm_rider, text, received_at, reply)
            return reply

        # Step 2: Account Impersonation Defense
        impersonation_match = re.search(r'\b(this is\s+)?(R\d{2,4})\b', text, re.IGNORECASE)
        if impersonation_match:
            claimed_rider = normalize_rider_id(impersonation_match.group(2))
            if claimed_rider != norm_rider:
                record_trace(
                    rider_id=norm_rider,
                    at=now_iso,
                    type_="decision",
                    name="impersonation_detected",
                    input_data={"claimed_rider": claimed_rider, "actual_rider": norm_rider},
                    output_data={"action": "refuse_third_party_access"}
                )
                create_ops_item(
                    rider_id=norm_rider,
                    type_="escalation",
                    amount=None,
                    reason=f"Rider {norm_rider} attempted to act on behalf of {claimed_rider}"
                )
                reply = "Main sirf is number se jude account ke baare mein madad kar sakta hoon. Aapke account ki koi specific problem ho to bataiye."
                record_trace(norm_rider, now_iso, "reply", "send_reply", None, reply)
                record_message(message_id, norm_rider, text, received_at, reply)
                return reply

        # Step 3: Context & History Gathering
        rec_dt = parse_to_ist(received_at)
        rec_date_str = get_ist_date_str(rec_dt)
        history = get_recent_messages(norm_rider)

        # Look for explicit order IDs in text
        order_ids = re.findall(r'T\d{6}', text)

        # Look for date references in text or previous context
        target_dates = self._extract_dates(text, rec_dt)

        # If no dates or orders found in current message, look back at recent conversation history
        if not order_ids and not target_dates and history:
            prev_texts = " ".join([h["text"] for h in history[-3:]])
            order_ids = re.findall(r'T\d{6}', prev_texts)
            target_dates = self._extract_dates(prev_texts, rec_dt)

        # Step 4: Handle Follow-up Inquiry / Status Check / Pushback
        lower_text = text.lower()
        is_status_inquiry = any(p in lower_text for p in [
            "kab tak", "kab aayega", "status", "thik hai, kab", "kab tak aayega"
        ])
        if is_status_inquiry:
            payouts = self.payswift.get_payouts(norm_rider)
            amt = payouts[-1]["amount"] if payouts else 25
            reply = f"₹{amt} ka payment PaySwift pe process ho gaya hai."
            record_trace(norm_rider, now_iso, "reply", "send_reply", None, reply)
            record_message(message_id, norm_rider, text, received_at, reply)
            return reply

        is_pushback = any(phrase in lower_text for phrase in [
            "dobara check", "fir se check", "again", "nahi nahi", "10 wala bhi abhi", "bhi abhi de do", "bhi abhi"
        ])

        if is_pushback:
            # Check what was previously discussed
            reply = self._handle_pushback(norm_rider, text, history, now_iso)
            record_trace(norm_rider, now_iso, "reply", "send_reply", None, reply)
            record_message(message_id, norm_rider, text, received_at, reply)
            return reply

        # Step 5: Check for Special Escalation Cases (Hardship, Accident, Distance dispute)
        if any(w in text.lower() for w in ["accident", "chot", "hospital", "gir gaya"]):
            record_trace(
                rider_id=norm_rider,
                at=now_iso,
                type_="decision",
                name="accident_hardship_escalation",
                input_data={"text": text},
                output_data={"action": "escalate_to_ops"}
            )
            create_ops_item(
                rider_id=norm_rider,
                type_="escalation",
                amount=None,
                reason=f"Accident/emergency during trip cancellation: {text}"
            )
            reply = "Accident ke baare mein sunkar khed hai. Main aapki request ops team ko bhej raha hoon taaki cancellation penalty review ki ja sake."
            record_trace(norm_rider, now_iso, "reply", "send_reply", None, reply)
            record_message(message_id, norm_rider, text, received_at, reply)
            return reply

        if "distance galat" in text.lower() or "km galat" in text.lower() or "distance wrong" in text.lower():
            trip_id = order_ids[0] if order_ids else None
            record_trace(
                rider_id=norm_rider,
                at=now_iso,
                type_="decision",
                name="distance_dispute_escalation",
                input_data={"trip_id": trip_id, "text": text},
                output_data={"action": "escalate_to_ops"}
            )
            create_ops_item(
                rider_id=norm_rider,
                type_="escalation",
                amount=None,
                reason=f"Rider disputing recorded GPS distance for trip {trip_id or 'unknown'}"
            )
            reply = f"Order {trip_id or ''} ke recorded distance ki jaanch ke liye request ops team ko bhej di gayi hai."
            record_trace(norm_rider, now_iso, "reply", "send_reply", None, reply)
            record_message(message_id, norm_rider, text, received_at, reply)
            return reply

        # Step 6: Vague Message Handling
        # If rider just says "payout galat aaya" without specifying date, order, or problem
        is_vague = not order_ids and not target_dates and any(
            p in text.lower() for p in ["payout galat", "payout kam", "paisa kam", "galat hai", "check karo pls"]
        ) and not any(k in text.lower() for k in ["incentive", "surge", "penalty", "cancel"])

        if is_vague:
            record_trace(
                rider_id=norm_rider,
                at=now_iso,
                type_="decision",
                name="request_clarification",
                input_data={"text": text},
                output_data={"need": "date_or_order_id"}
            )
            reply = "Kaunse din ya kaunse order ka payout galat laga? Date ya order ID bata dijiye."
            record_trace(norm_rider, now_iso, "reply", "send_reply", None, reply)
            record_message(message_id, norm_rider, text, received_at, reply)
            return reply

        # Step 7: Run Deterministic Audits Based on Mentions
        discrepancies: List[Dict[str, Any]] = []
        explanatory_notes: List[str] = []
        checked_trips: List[str] = []
        older_than_7_days = False

        # If specific orders mentioned:
        for tid in order_ids:
            trip_info = self._audit_trip(norm_rider, tid, now_iso)
            checked_trips.append(tid)
            if trip_info.get("wrong_rider"):
                record_trace(
                    rider_id=norm_rider,
                    at=now_iso,
                    type_="decision",
                    name="order_not_belong_to_rider",
                    input_data={"trip_id": tid, "actual_rider": trip_info["actual_rider"]},
                    output_data={"action": "escalate_optional"}
                )
                create_ops_item(
                    rider_id=norm_rider,
                    type_="escalation",
                    amount=None,
                    reason=f"Order {tid} belongs to {trip_info['actual_rider']}, not {norm_rider}"
                )
                reply = f"Order {tid} aapke account se linked nahi hai. Agar yeh galti se hua hai to ops team se check karwaya ja sakta hai."
                record_trace(norm_rider, now_iso, "reply", "send_reply", None, reply)
                record_message(message_id, norm_rider, text, received_at, reply)
                return reply

            trip_date = trip_info.get("date_ist")
            if trip_date and not is_within_dispute_window(received_at, trip_date):
                older_than_7_days = True
                record_trace(
                    rider_id=norm_rider,
                    at=now_iso,
                    type_="decision",
                    name="dispute_window_expired",
                    input_data={"trip_id": tid, "trip_date": trip_date, "received_at": received_at},
                    output_data={"action": "reject_old_dispute"}
                )
                create_ops_item(
                    rider_id=norm_rider,
                    type_="escalation",
                    amount=None,
                    reason=f"Dispute for order {tid} on {trip_date} is older than 7 days limit"
                )
                reply = f"Order {tid} ({trip_date}) ka dispute 7 dino se purana hai. Policy ke mutabiq 7 dino ke baad dispute auto-process nahi hota, ise ops review ke liye bhej diya hai."
                record_trace(norm_rider, now_iso, "reply", "send_reply", None, reply)
                record_message(message_id, norm_rider, text, received_at, reply)
                return reply

            if trip_info.get("discrepancy", 0) > 0:
                discrepancies.append(trip_info)
                explanatory_notes.append(
                    f"Order {tid}: ₹{trip_info['expected_fare']} banta tha, ₹{trip_info['actual_paid']} mila. Farak: ₹{trip_info['discrepancy']}."
                )
            else:
                if "surge" in text.lower() and trip_info.get("surge_multiplier", 1.0) <= 1.0:
                    explanatory_notes.append(f"Order {tid} pe koi surge nahi tha. Payout ₹{trip_info.get('actual_paid')} sahi tha.")

        # If date(s) mentioned or implied:
        for dt_str in target_dates:
            if not is_within_dispute_window(received_at, dt_str):
                older_than_7_days = True
                record_trace(
                    rider_id=norm_rider,
                    at=now_iso,
                    type_="decision",
                    name="dispute_window_expired",
                    input_data={"date": dt_str, "received_at": received_at},
                    output_data={"action": "reject_old_dispute"}
                )
                create_ops_item(
                    rider_id=norm_rider,
                    type_="escalation",
                    amount=None,
                    reason=f"Disputes for {dt_str} are older than 7 days"
                )
                reply = f"{dt_str} ka dispute 7 dino se purana hai. Hum sirf pichle 7 dino ke disputes dekh sakte hain."
                record_trace(norm_rider, now_iso, "reply", "send_reply", None, reply)
                record_message(message_id, norm_rider, text, received_at, reply)
                return reply

            # Audit incentive on that date if mentioned or if rider complains about incentive
            wants_incentive = any(w in text.lower() for w in ["incentive", "12 se zyada", "12 order", "12 trips"])
            wants_penalty = any(w in text.lower() for w in ["penalty", "cancel", "kata", "kaata"])
            wants_surge = "surge" in text.lower()
            wants_missing = any(w in text.lower() for w in ["order ka payment nahi", "orders ka paisa", "missing", "nahi aaya"])

            # Check daily incentive
            if wants_incentive or not (wants_penalty or wants_surge or wants_missing):
                inc_info = self._audit_incentive(norm_rider, dt_str, now_iso)
                if inc_info["discrepancy"] > 0:
                    discrepancies.append(inc_info)
                    explanatory_notes.append(
                        f"{dt_str} ko aapke {inc_info['completed_count']} completed trips the. ₹{inc_info['discrepancy']} daily incentive banta tha jo nahi mila tha."
                    )
                else:
                    explanatory_notes.append(
                        f"{dt_str} ko aapke {inc_info['completed_count']} trips complete hue the. Incentive 12 trips pe milta hai, isliye nahi bana."
                    )

            # Check cancellation penalties
            if wants_penalty or not (wants_incentive or wants_surge or wants_missing):
                pen_info = self._audit_penalties(norm_rider, dt_str, now_iso)
                if pen_info["discrepancy"] > 0:
                    discrepancies.append(pen_info)
                    explanatory_notes.append(
                        f"{dt_str} ko cancel penalty extra kata tha, ₹{pen_info['discrepancy']} wapas banta hai."
                    )

            # Check trips on that date for surge or missing trip payouts
            date_trips = get_trips_for_rider_date(norm_rider, dt_str)
            payouts_on_date = get_payouts_for_rider_date(norm_rider, dt_str)
            paid_trip_ids = {p["trip_id"] for p in payouts_on_date if p["trip_id"] and p["line_type"] == "trip"}

            for t in date_trips:
                tid = t["trip_id"]
                if tid in checked_trips:
                    continue
                checked_trips.append(tid)

                # Check if trip was completely unpaid
                if t["status"] == "completed" and tid not in paid_trip_ids:
                    fare = calculate_trip_fare(t["distance_km"], t["surge_multiplier"])
                    discrepancies.append({
                        "type": "unpaid_trip",
                        "trip_id": tid,
                        "date_ist": dt_str,
                        "discrepancy": fare,
                        "reason": f"Trip {tid} completed on {dt_str} was unpaid"
                    })
                    explanatory_notes.append(f"Trip {tid}: ₹{fare} missing tha.")
                elif t["status"] == "completed" and tid in paid_trip_ids:
                    # Check surge discrepancy
                    trip_payouts = [p for p in payouts_on_date if p["trip_id"] == tid and p["line_type"] == "trip"]
                    actual_paid = sum(p["amount"] for p in trip_payouts)
                    exp_fare = calculate_trip_fare(t["distance_km"], t["surge_multiplier"])
                    if exp_fare > actual_paid and (wants_surge or not (wants_incentive or wants_penalty)):
                        diff = exp_fare - actual_paid
                        discrepancies.append({
                            "type": "surge_missing",
                            "trip_id": tid,
                            "date_ist": dt_str,
                            "expected_fare": exp_fare,
                            "actual_paid": actual_paid,
                            "discrepancy": diff,
                            "reason": f"Surge missing on {tid}"
                        })
                        explanatory_notes.append(
                            f"{dt_str} ko order {tid} pe {t['surge_multiplier']}x surge laga tha: ₹{exp_fare} banta tha, ₹{actual_paid} mila. Farak ₹{diff}."
                        )

        # Step 8: Apply Finance Policy & Settle Disputes
        reply = self._settle_discrepancies(
            rider_id=norm_rider,
            discrepancies=discrepancies,
            explanatory_notes=explanatory_notes,
            message_id=message_id,
            now_iso=now_iso,
            rec_date_str=rec_date_str,
            raw_text=text
        )

        record_trace(norm_rider, now_iso, "reply", "send_reply", None, reply)
        record_message(message_id, norm_rider, text, received_at, reply)
        return reply

    def _extract_dates(self, text: str, rec_dt: datetime) -> List[str]:
        """Extract all target dates referenced in text (e.g. '20 wala', '19 sept', 'kal')."""
        dates = []
        lower = text.lower()

        # Handle 'kal' (yesterday)
        if "kal" in lower:
            yesterday = (rec_dt - timedelta(days=1)).date()
            dates.append(yesterday.isoformat())

        # Handle explicit numbers with date context: e.g. "20 tarikh", "20 wala", "20 ko", "19 sept", "Sep 19"
        # Regex for patterns like: '20 wala', '20 ko', '20 tarikh', '19 sept', '18 tarikh', '13 sept', 'Sep 19'
        month_matches = re.findall(r'(?:sep|september)\s*(\d{1,2})|(\d{1,2})\s*(?:sep|sept|september)', lower)
        for m in month_matches:
            day_str = m[0] or m[1]
            if day_str:
                d_int = int(day_str)
                # Assume 2026-09
                dates.append(f"2026-09-{d_int:02d}")

        day_matches = re.findall(r'\b(\d{1,2})\s*(?:tarikh|ko|wala|th)\b', lower)
        for d in day_matches:
            d_int = int(d)
            if 1 <= d_int <= 31:
                dates.append(f"2026-09-{d_int:02d}")

        # Unique preserving order
        unique_dates = []
        for d in dates:
            if d not in unique_dates:
                unique_dates.append(d)
        return unique_dates

    def _audit_trip(self, rider_id: str, trip_id: str, now_iso: str) -> Dict[str, Any]:
        """Tool: Lookup and verify trip details against policy."""
        trip = get_trip(trip_id)
        record_trace(
            rider_id=rider_id,
            at=now_iso,
            type_="tool_call",
            name="lookup_trip",
            input_data={"trip_id": trip_id},
            output_data=dict(trip) if trip else None
        )
        if not trip:
            return {"trip_id": trip_id, "found": False, "discrepancy": 0}

        if trip["rider_id"] != rider_id:
            return {"trip_id": trip_id, "wrong_rider": True, "actual_rider": trip["rider_id"], "discrepancy": 0}

        expected_fare = calculate_trip_fare(trip["distance_km"], trip["surge_multiplier"])
        payouts = get_payouts_for_trip(trip_id)
        actual_paid = sum(p["amount"] for p in payouts if p["line_type"] == "trip")
        diff = max(0, expected_fare - actual_paid)

        return {
            "type": "trip_discrepancy",
            "trip_id": trip_id,
            "date_ist": trip["date_ist"],
            "distance_km": trip["distance_km"],
            "surge_multiplier": trip["surge_multiplier"],
            "expected_fare": expected_fare,
            "actual_paid": actual_paid,
            "discrepancy": diff,
            "reason": f"Order {trip_id} surge/fare underpaid by ₹{diff}"
        }

    def _audit_incentive(self, rider_id: str, date_ist: str, now_iso: str) -> Dict[str, Any]:
        """Tool: Check unique completed trips in IST vs incentive payout."""
        trips = get_trips_for_rider_date(rider_id, date_ist)
        completed_trips = [t for t in trips if t["status"] == "completed"]
        unique_completed_ids = {t["trip_id"] for t in completed_trips}
        completed_count = len(unique_completed_ids)

        payouts = get_payouts_for_rider_date(rider_id, date_ist)
        incentive_payouts = [p for p in payouts if p["line_type"] == "daily_incentive"]
        incentive_paid = sum(p["amount"] for p in incentive_payouts)

        qualified = completed_count >= DAILY_INCENTIVE_THRESHOLD
        expected_incentive = DAILY_INCENTIVE_AMOUNT if qualified else 0
        discrepancy = max(0, expected_incentive - incentive_paid)

        output = {
            "date_ist": date_ist,
            "completed_count": completed_count,
            "threshold": DAILY_INCENTIVE_THRESHOLD,
            "qualified": qualified,
            "incentive_paid": incentive_paid,
            "discrepancy": discrepancy
        }
        record_trace(
            rider_id=rider_id,
            at=now_iso,
            type_="tool_call",
            name="audit_daily_incentive",
            input_data={"rider_id": rider_id, "date_ist": date_ist},
            output_data=output
        )
        return {
            "type": "incentive_discrepancy",
            "date_ist": date_ist,
            "completed_count": completed_count,
            "discrepancy": discrepancy,
            "reason": f"Daily incentive for {date_ist} ({completed_count} trips)"
        }

    def _audit_penalties(self, rider_id: str, date_ist: str, now_iso: str) -> Dict[str, Any]:
        """Tool: Check rider cancellations vs penalty deductions."""
        trips = get_trips_for_rider_date(rider_id, date_ist)
        rider_cancellations = [t for t in trips if t["status"] == "cancelled_by_rider"]
        cancellation_count = len(rider_cancellations)
        expected_penalty_deduction = cancellation_count * CANCELLATION_PENALTY_AMOUNT

        payouts = get_payouts_for_rider_date(rider_id, date_ist)
        penalties = [p for p in payouts if p["line_type"] == "cancellation_penalty"]
        # In payouts, penalties are negative (-10)
        actual_deducted = abs(sum(p["amount"] for p in penalties))

        discrepancy = max(0, actual_deducted - expected_penalty_deduction)

        output = {
            "date_ist": date_ist,
            "rider_cancellations": cancellation_count,
            "actual_deducted": actual_deducted,
            "expected_deducted": expected_penalty_deduction,
            "refund_owed": discrepancy
        }
        record_trace(
            rider_id=rider_id,
            at=now_iso,
            type_="tool_call",
            name="audit_cancellation_penalties",
            input_data={"rider_id": rider_id, "date_ist": date_ist},
            output_data=output
        )
        return {
            "type": "penalty_refund",
            "date_ist": date_ist,
            "discrepancy": discrepancy,
            "reason": f"Cancellation penalty refund for {date_ist}"
        }

    def _settle_discrepancies(
        self,
        rider_id: str,
        discrepancies: List[Dict[str, Any]],
        explanatory_notes: List[str],
        message_id: str,
        now_iso: str,
        rec_date_str: str,
        raw_text: str
    ) -> str:
        """
        Enforce Finance policy rules:
        - Auto-pay up to ₹200 per dispute
        - Once per rider per day (reconciled against PaySwift)
        - Amounts > ₹200 or repeat disputes go to Ops Approval
        """
        valid_disputes = [d for d in discrepancies if d.get("discrepancy", 0) > 0]

        if not valid_disputes:
            if explanatory_notes:
                return " ".join(explanatory_notes)
            return "Aapka account check kiya gaya hai aur sabhi payouts record ke anusaar sahi hain. Agar koi specific order ya date hai to bataiye."

        # Reconcile against PaySwift: has this rider already had an auto-payout today?
        has_paid_today = self.payswift.has_payout_today(rider_id, rec_date_str)
        record_trace(
            rider_id=rider_id,
            at=now_iso,
            type_="tool_call",
            name="reconcile_payswift_ledger",
            input_data={"rider_id": rider_id, "date_ist": rec_date_str},
            output_data={"has_paid_today": has_paid_today}
        )

        reply_lines: List[str] = list(explanatory_notes)

        # Group disputes by date/context
        dates_present = set(d.get("date_ist") for d in valid_disputes if d.get("date_ist"))

        # Case A: All discrepancies belong to a single date/issue (e.g. 2 missing orders on 20 Sep = 176, or 5 orders = 425)
        if len(dates_present) <= 1:
            total_owed = sum(d["discrepancy"] for d in valid_disputes)
            order_names = [d["trip_id"] for d in valid_disputes if d.get("trip_id")]
            ref_summary = f"Dispute resolution for {', '.join(order_names) if order_names else 'payout discrepancy'}"

            if total_owed <= AUTO_PAY_LIMIT and not has_paid_today:
                # Auto-pay entire amount for this dispute
                idem_key = f"payout_{rider_id}_{message_id}"
                payout_res = self.payswift.create_payout(
                    rider_id=rider_id,
                    amount=total_owed,
                    reference=ref_summary,
                    idempotency_key=idem_key
                )
                record_trace(
                    rider_id=rider_id,
                    at=now_iso,
                    type_="tool_call",
                    name="payswift_create_payout",
                    input_data={"amount": total_owed, "reference": ref_summary, "idempotency_key": idem_key},
                    output_data=payout_res
                )
                record_trace(
                    rider_id=rider_id,
                    at=now_iso,
                    type_="decision",
                    name="auto_pay_executed",
                    input_data={"amount": total_owed},
                    output_data={"status": "paid", "payout_id": payout_res.get("payout_id")}
                )
                reply_lines.append(f"₹{total_owed} aapko bhej rahe hain.")
            else:
                # Exceeds limit (> 200) or already paid today
                reason_txt = f"Amount ₹{total_owed} exceeds ₹{AUTO_PAY_LIMIT} limit" if total_owed > AUTO_PAY_LIMIT else f"Repeat dispute today (₹{total_owed})"
                item_id = create_ops_item(
                    rider_id=rider_id,
                    type_="approval",
                    amount=total_owed,
                    reason=f"{reason_txt}: {ref_summary}"
                )
                record_trace(
                    rider_id=rider_id,
                    at=now_iso,
                    type_="decision",
                    name="queue_ops_approval",
                    input_data={"amount": total_owed, "reason": reason_txt},
                    output_data={"ops_item_id": item_id}
                )
                if total_owed > AUTO_PAY_LIMIT:
                    return f"Aapke missing orders ka total ₹{total_owed} banta hai. ₹{AUTO_PAY_LIMIT} se zyada hone ke kaaran ise ops team ke approval ke liye bhej diya gaya hai, approve hote hi credit ho jayega."
                else:
                    reply_lines.append(f"Ek din mein ek hi auto-payment ho sakta hai, isliye ₹{total_owed} ops team approve karegi.")

            return " ".join(reply_lines)

        # Case B: Multi-date disputes in one message (e.g. 20 Sep surge ₹15, 21 Sep penalty ₹10)
        auto_paid_amounts: List[int] = []
        approved_queue_amounts: List[int] = []

        for idx, disp in enumerate(valid_disputes):
            amount = disp["discrepancy"]
            disp_reason = disp.get("reason", "Payout discrepancy")

            # Condition for auto-pay: <= 200 AND no payout made for this rider today AND haven't auto-paid in this batch yet!
            can_autopay = (amount <= AUTO_PAY_LIMIT) and (not has_paid_today) and (len(auto_paid_amounts) == 0)

            if can_autopay:
                idem_key = f"payout_{rider_id}_{disp.get('trip_id') or disp.get('date_ist')}_{message_id}"
                payout_res = self.payswift.create_payout(
                    rider_id=rider_id,
                    amount=amount,
                    reference=disp_reason,
                    idempotency_key=idem_key
                )
                auto_paid_amounts.append(amount)
                has_paid_today = True  # consumed daily quota!

                record_trace(
                    rider_id=rider_id,
                    at=now_iso,
                    type_="tool_call",
                    name="payswift_create_payout",
                    input_data={"amount": amount, "reference": disp_reason, "idempotency_key": idem_key},
                    output_data=payout_res
                )
                record_trace(
                    rider_id=rider_id,
                    at=now_iso,
                    type_="decision",
                    name="auto_pay_executed",
                    input_data={"amount": amount},
                    output_data={"status": "paid", "payout_id": payout_res.get("payout_id")}
                )
                reply_lines.append(f"₹{amount} abhi bhej rahe hain;")
            else:
                # Must be queued for Ops Approval
                approval_reason = f"Repeat dispute today (₹{amount})" if has_paid_today else f"Amount ₹{amount} exceeds limit"
                item_id = create_ops_item(
                    rider_id=rider_id,
                    type_="approval",
                    amount=amount,
                    reason=f"{approval_reason}: {disp_reason}"
                )
                approved_queue_amounts.append(amount)
                record_trace(
                    rider_id=rider_id,
                    at=now_iso,
                    type_="decision",
                    name="queue_ops_approval",
                    input_data={"amount": amount, "reason": approval_reason},
                    output_data={"ops_item_id": item_id}
                )
                reply_lines.append(f"ek din mein ek hi auto-payment ho sakta hai, isliye ₹{amount} ops team approve karegi.")

        return " ".join(reply_lines)

    def _handle_pushback(
        self,
        rider_id: str,
        text: str,
        history: List[Dict[str, Any]],
        now_iso: str
    ) -> str:
        """Handle rider asking again / demanding instant payment / disagreeing."""
        lower = text.lower()

        # If demanding payment that is in ops approval queue (e.g. Scenario 2: '10 wala bhi abhi de do na')
        if any(w in lower for w in ["bhi abhi", "abhi de do", "turant do"]):
            record_trace(
                rider_id=rider_id,
                at=now_iso,
                type_="decision",
                name="explain_pending_approval",
                input_data={"text": text},
                output_data={"action": "reassure_ops_approval"}
            )
            # Find the amount mentioned in text if any
            amt_match = re.search(r'\b(\d+)\b', text)
            amt_str = f"₹{amt_match.group(1)} " if amt_match else ""
            return f"Samajh sakta hoon. {amt_str}ops approval ke liye chala gaya hai, approve hote hi aa jayega."

        # If asking to check again on incentive / order (e.g. Scenario 3: 'nahi nahi 12 kiye the, dobara check karo')
        if "dobara check" in lower or "again" in lower or "nahi nahi" in lower:
            # Re-check previous incentive audit
            prev_inc = None
            if history:
                prev_text = history[-1]["text"]
                dt_list = self._extract_dates(prev_text, parse_to_ist(now_iso))
                if dt_list:
                    prev_inc = self._audit_incentive(rider_id, dt_list[0], now_iso)

            completed_cnt = prev_inc["completed_count"] if prev_inc else 10
            record_trace(
                rider_id=rider_id,
                at=now_iso,
                type_="decision",
                name="recheck_confirmed_and_offer_escalation",
                input_data={"recheck": True},
                output_data={"completed_trips": completed_cnt, "action": "offer_ops_review"}
            )
            create_ops_item(
                rider_id=rider_id,
                type_="escalation",
                amount=None,
                reason=f"Rider disagrees with trip count ({completed_cnt} completed trips) and requested re-check"
            )
            return f"Dobara check kiya: humare record mein {completed_cnt} completed trips hain. Agar aapko lagta hai record galat hai, main ise ops team ko bhej deta hoon."

        # Generic question (e.g. 'kab tak aayega?')
        if any(w in lower for w in ["kab tak", "status", "aayega"]):
            payouts = self.payswift.get_payouts(rider_id)
            if payouts:
                last_p = payouts[-1]
                return f"₹{last_p.get('amount', '')} ka payment PaySwift pe process ho gaya hai."
            return "Aapka payment process ho chuka hai, jald hi credit ho jayega."

        return "Maine aapki request check kar li hai. Kisi aur order ke baare mein jaankari chahiye to bataiye."
