from __future__ import annotations
import json
import os
import sys
import time
from pathlib import Path
from typing import Any, Dict, List, Optional
import httpx

DATA_PATH = Path(__file__).resolve().parent.parent / "data" / "conversations.json"


def run_evals(service_url: str, payswift_url: Optional[str] = None):
    service_url = service_url.rstrip("/").replace("localhost", "127.0.0.1")
    payswift_url = (payswift_url or os.getenv("PAYSWIFT_BASE_URL", "http://127.0.0.1:8081")).rstrip("/").replace("localhost", "127.0.0.1")

    print("=" * 80)
    print(f"  QUICKDROP RIDER PAYOUT DISPUTE DESK - EVALUATION SUITE")
    print(f"  Target Service:  {service_url}")
    print(f"  PaySwift URL:    {payswift_url}")
    print("=" * 80)

    # 1. Health check & PaySwift discovery
    try:
        with httpx.Client(timeout=5.0) as client:
            res = client.get(f"{service_url}/health")
            if res.status_code != 200:
                print(f"[FAIL] Service health check returned HTTP {res.status_code}")
                sys.exit(1)
            health_data = res.json()
            # Only adopt payswift_url from health if not an internal docker alias when running externally
            candidate_ps = health_data.get("payswift_url")
            if candidate_ps and "payswift:" not in candidate_ps and not os.getenv("PAYSWIFT_BASE_URL"):
                payswift_url = candidate_ps.rstrip("/").replace("localhost", "127.0.0.1")
    except Exception as e:
        print(f"[FAIL] Could not connect to target service at {service_url}: {e}")
        print("  -> Ensure your app is running: uvicorn app.main:app --port 8000")
        sys.exit(1)

    # 2. PaySwift Health Check
    payswift_running = False
    try:
        with httpx.Client(timeout=3.0) as client:
            ps_res = client.get(f"{payswift_url}/health")
            if ps_res.status_code == 200:
                payswift_running = True
    except Exception:
        pass

    if not payswift_running:
        print(f"\n[WARNING] PaySwift sandbox is not reachable at {payswift_url}.")
        print("  Attempting to start local PaySwift sandbox automatically...")
        try:
            import threading
            from payswift.mock_server import Handler as PaySwiftHandler, ThreadingHTTPServer
            ps_server = ThreadingHTTPServer(("127.0.0.1", 8081), PaySwiftHandler)
            ps_thread = threading.Thread(target=ps_server.serve_forever, daemon=True)
            ps_thread.start()
            time.sleep(1.0)
            payswift_url = "http://127.0.0.1:8081"
            print("  PaySwift sandbox started on http://127.0.0.1:8081!")
        except Exception as ps_err:
            print(f"[FAIL] Could not start PaySwift: {ps_err}")
            print("  -> Please run PaySwift in another terminal: python payswift/mock_server.py")
            sys.exit(1)

    with open(DATA_PATH, mode="r", encoding="utf-8") as f:
        scenarios: List[Dict[str, Any]] = json.load(f)

    print(f"\nLoaded {len(scenarios)} evaluation scenarios from {DATA_PATH.name}.\n")

    results = []
    start_time = time.time()

    for idx, sc in enumerate(scenarios, 1):
        rider_id = sc["rider_id"]
        scenario_desc = sc.get("scenario", "dispute scenario")
        expected = sc["expected"]

        # 1. Reset state for isolation
        try:
            with httpx.Client(timeout=4.0) as client:
                client.post(f"{service_url}/reset")
                client.post(f"{payswift_url}/admin/reset")
        except Exception:
            pass

        # 2. Run conversation turns
        replies: List[str] = []
        turn_errors = []
        for turn in sc["turns"]:
            if turn["from"] == "rider":
                payload = {
                    "message_id": turn["message_id"],
                    "rider_id": rider_id,
                    "text": turn["text"],
                    "received_at": turn["received_at"]
                }
                try:
                    with httpx.Client(timeout=10.0) as client:
                        resp = client.post(f"{service_url}/messages", json=payload)
                        if resp.status_code == 200:
                            data = resp.json()
                            replies.append(data.get("reply", ""))
                        else:
                            turn_errors.append(f"HTTP {resp.status_code}: {resp.text}")
                except Exception as ex:
                    turn_errors.append(str(ex))

        # 3. Fetch PaySwift payouts
        actual_payout = 0
        try:
            with httpx.Client(timeout=4.0) as client:
                res = client.get(f"{payswift_url}/v1/payouts", params={"rider_id": rider_id})
                if res.status_code == 200:
                    payout_items = res.json()
                    actual_payout = sum(p.get("amount", 0) for p in payout_items)
        except Exception as e:
            turn_errors.append(f"PaySwift query failed: {e}")

        # 4. Fetch Pending Ops items
        actual_approval: Optional[int] = None
        escalations_count = 0
        try:
            with httpx.Client(timeout=4.0) as client:
                res = client.get(f"{service_url}/ops/pending")
                if res.status_code == 200:
                    ops_items = res.json()
                    rider_ops = [i for i in ops_items if i.get("rider_id") == rider_id]
                    for item in rider_ops:
                        if item.get("type") == "approval":
                            amt = item.get("amount") or 0
                            actual_approval = (actual_approval or 0) + amt
                        elif item.get("type") == "escalation":
                            escalations_count += 1
        except Exception as e:
            turn_errors.append(f"Ops query failed: {e}")

        # 5. Check Trace Output Shape
        trace_ok = True
        try:
            with httpx.Client(timeout=4.0) as client:
                res = client.get(f"{service_url}/trace/{rider_id}")
                if res.status_code == 200:
                    traces = res.json()
                    if not isinstance(traces, list):
                        trace_ok = False
                    for step in traces:
                        if not all(k in step for k in ["at", "type", "name", "input", "output"]):
                            trace_ok = False
                            break
                else:
                    trace_ok = False
        except Exception:
            trace_ok = False

        # 6. Verify Expectations
        passed = True
        failure_reasons = []

        if turn_errors:
            passed = False
            failure_reasons.extend(turn_errors)

        if not trace_ok:
            passed = False
            failure_reasons.append("Trace output missing required fields")

        # Check 'one_of' or exact expectations
        if "one_of" in expected:
            matched_one = False
            for cand in expected["one_of"]:
                p_match = (actual_payout == cand.get("payout", 0))
                a_match = (actual_approval == cand.get("approval"))
                if p_match and a_match:
                    matched_one = True
                    break
            if not matched_one:
                passed = False
                failure_reasons.append(f"Expected one of {expected['one_of']}, got payout={actual_payout}, approval={actual_approval}")
        else:
            exp_payout = expected.get("payout", 0)
            if actual_payout != exp_payout:
                passed = False
                failure_reasons.append(f"Payout mismatch: expected ₹{exp_payout}, got ₹{actual_payout}")

            exp_approval = expected.get("approval")
            if actual_approval != exp_approval:
                passed = False
                failure_reasons.append(f"Approval mismatch: expected ₹{exp_approval}, got ₹{actual_approval}")

        # Escalation check
        exp_esc = expected.get("escalation", "no")
        if exp_esc == "no" and escalations_count > 0:
            passed = False
            failure_reasons.append(f"Expected no escalation, but found {escalations_count}")
        elif exp_esc == "required" and escalations_count == 0:
            passed = False
            failure_reasons.append("Expected required escalation, but none was logged")

        # Mentions check
        concat_replies = " ".join(replies)
        for mention in expected.get("reply_mentions", []):
            if mention not in concat_replies:
                passed = False
                failure_reasons.append(f"Reply missing required mention '{mention}'")

        status_str = "\033[92mPASS\033[0m" if passed else "\033[91mFAIL\033[0m"
        print(f"[{status_str}] Scenario {idx:02d} ({rider_id}): {scenario_desc[:45]:<45} | Pay: ₹{actual_payout:<3} | Appr: {repr(actual_approval):<5}")
        if not passed:
            for r in failure_reasons:
                print(f"       -> \033[93m{r}\033[0m")

        results.append({
            "idx": idx,
            "rider_id": rider_id,
            "scenario": scenario_desc,
            "passed": passed,
            "reasons": failure_reasons
        })

    duration = time.time() - start_time
    total = len(results)
    passed_count = sum(1 for r in results if r["passed"])
    failed_count = total - passed_count
    accuracy = (passed_count / total) * 100

    print("\n" + "=" * 80)
    print(f"  EVALUATION SUMMARY")
    print(f"  Total Scenarios: {total}")
    print(f"  Passed:          {passed_count} ({accuracy:.1f}%)")
    print(f"  Failed:          {failed_count}")
    print(f"  Execution Time:  {duration:.2f}s")
    print("=" * 80)

    if failed_count == 0:
        print("\n\033[92m✓ ALL EVALUATION TESTS PASSED PERFECTLY!\033[0m\n")
        sys.exit(0)
    else:
        print(f"\n\033[91m✗ {failed_count} SCENARIO(S) FAILED.\033[0m\n")
        sys.exit(1)


if __name__ == "__main__":
    target = sys.argv[1] if len(sys.argv) > 1 else "http://localhost:8000"
    ps_target = sys.argv[2] if len(sys.argv) > 2 else None
    run_evals(target, ps_target)

