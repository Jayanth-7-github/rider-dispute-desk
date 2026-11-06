from __future__ import annotations
import json
import os
import sys
import time
from pathlib import Path
from typing import Any, Dict, List

# Set in-memory mock before importing agent
os.environ["PAYSWIFT_MOCK"] = "1"

BASE_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE_DIR))

from app.agent import DisputeAgent
from app.db import (
    get_pending_ops_items,
    get_traces_for_rider,
    init_db,
    reset_runtime_state,
)
from app.payswift_client import PaySwiftClient, reset_mock_payswift

DATA_PATH = BASE_DIR / "data" / "conversations.json"


def run_unit_evals():
    init_db(force_reseed=True)
    payswift = PaySwiftClient(use_mock=True)
    agent = DisputeAgent(payswift_client=payswift)

    with open(DATA_PATH, mode="r", encoding="utf-8") as f:
        scenarios: List[Dict[str, Any]] = json.load(f)

    print("=" * 80)
    print(f"  RUNNING LOCAL EVALUATIONS ({len(scenarios)} SCENARIOS)")
    print("=" * 80)

    results = []
    start_time = time.time()

    for idx, sc in enumerate(scenarios, 1):
        rider_id = sc["rider_id"]
        desc = sc.get("scenario", "dispute")
        expected = sc["expected"]

        # 1. Reset state
        reset_runtime_state()
        reset_mock_payswift()

        # 2. Run turns
        replies = []
        for turn in sc["turns"]:
            if turn["from"] == "rider":
                reply = agent.process_message(
                    message_id=turn["message_id"],
                    rider_id=rider_id,
                    text=turn["text"],
                    received_at=turn["received_at"]
                )
                replies.append(reply)

        # 3. PaySwift payout
        payouts = payswift.get_payouts(rider_id)
        actual_payout = sum(p["amount"] for p in payouts)

        # 4. Pending Ops
        ops_items = get_pending_ops_items()
        rider_ops = [i for i in ops_items if i["rider_id"] == rider_id]
        actual_approval = None
        escalations_count = 0
        for item in rider_ops:
            if item["type"] == "approval":
                actual_approval = (actual_approval or 0) + (item["amount"] or 0)
            elif item["type"] == "escalation":
                escalations_count += 1

        # 5. Traces
        traces = get_traces_for_rider(rider_id)
        trace_ok = len(traces) > 0 and all(
            all(k in step for k in ["at", "type", "name", "input", "output"])
            for step in traces
        )

        # 6. Check criteria
        passed = True
        failures = []

        if not trace_ok:
            passed = False
            failures.append("Trace format invalid")

        if "one_of" in expected:
            matched = any(
                actual_payout == cand.get("payout", 0) and actual_approval == cand.get("approval")
                for cand in expected["one_of"]
            )
            if not matched:
                passed = False
                failures.append(f"Expected one of {expected['one_of']}, got payout={actual_payout}, approval={actual_approval}")
        else:
            exp_payout = expected.get("payout", 0)
            if actual_payout != exp_payout:
                passed = False
                failures.append(f"Payout expected ₹{exp_payout}, got ₹{actual_payout}")

            exp_appr = expected.get("approval")
            if actual_approval != exp_appr:
                passed = False
                failures.append(f"Approval expected ₹{exp_appr}, got ₹{actual_approval}")

        exp_esc = expected.get("escalation", "no")
        if exp_esc == "no" and escalations_count > 0:
            passed = False
            failures.append(f"Expected no escalation, got {escalations_count}")
        elif exp_esc == "required" and escalations_count == 0:
            passed = False
            failures.append("Expected required escalation, none logged")

        concat = " ".join(replies)
        for m in expected.get("reply_mentions", []):
            if m not in concat:
                passed = False
                failures.append(f"Missing required mention '{m}'")

        status_str = "PASS" if passed else "FAIL"
        print(f"[{status_str}] Scenario {idx:02d} ({rider_id}): {desc[:40]:<40} | Pay: {actual_payout:<3} | Appr: {repr(actual_approval):<5}")
        if not passed:
            for f in failures:
                print(f"       -> {f}")
            print(f"       -> Replies: {replies}")

        results.append({"idx": idx, "passed": passed, "failures": failures})

    duration = time.time() - start_time
    total = len(results)
    passed_cnt = sum(1 for r in results if r["passed"])
    failed_cnt = total - passed_cnt

    print("\n" + "=" * 80)
    print(f"  LOCAL TEST SUMMARY: {passed_cnt}/{total} PASSED in {duration:.2f}s ({(passed_cnt/total)*100:.1f}%)")
    print("=" * 80)
    return failed_cnt == 0


if __name__ == "__main__":
    success = run_unit_evals()
    sys.exit(0 if success else 1)

