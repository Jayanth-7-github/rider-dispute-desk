# QuickDrop Rider Payout Dispute Desk

Autonomous AI agent system handling rider payout complaints for QuickDrop with deterministic policy enforcement, PaySwift ledger reconciliation, and human-in-the-loop ops escalation.

---

## 1. Quick Start

### Run with Docker Compose
```bash
docker compose up --build
```
- **Service API**: `http://localhost:8000` (answers `GET /health` with 200 OK)
- **Ops Dashboard**: `http://localhost:8000/ops`
- **PaySwift Sandbox**: `http://localhost:8081`

### Run Evaluations
```bash
python evals/run.py http://localhost:8000
```
*(For standalone local testing without Docker, run `python evals/test_all_scenarios.py`)*

---

## 2. Agent Design

The system enforces a strict boundary between probabilistic LLM natural language understanding and deterministic financial business logic.

```
                  ┌──────────────────────┐
                  │ Messaging Vendor     │
                  │ POST /messages       │
                  └──────────┬───────────┘
                             │
                             ▼
              ┌──────────────────────────────┐
              │  Security & NLU Pipeline     │
              │  - Prompt Injection Defense  │
              │  - Identity Authorization    │
              │  - Hinglish Intent / Date NLU│
              └──────────────┬───────────────┘
                             │
                             ▼
       ┌────────────────────────────────────────────┐
       │ Deterministic Policy & Audit Tools (Code)  │
       │ - lookup_trip                              │
       │ - audit_daily_incentive (12 trips in IST)  │
       │ - audit_cancellation_penalties             │
       │ - reconcile_payswift_ledger                │
       └──────────────┬─────────────────────────────┘
                      │
           ┌──────────┴──────────┐
           ▼                     ▼
  [ ≤ ₹200 & 1st today ]   [ > ₹200 OR repeat ]
           │                     │
           ▼                     ▼
┌──────────────────────┐  ┌──────────────────────┐
│ PaySwift Client      │  │ Ops Pending Queue    │
│ - Idempotent Payout  │  │ - Approval / Escalate│
│ - Chaos/Retry Logic  │  │ - Ops Portal Review  │
└──────────────────────┘  └──────────────────────┘
```

### What the Model Decides
- **Conversational NLU**: Interpreting mixed Hinglish, Hindi, and English rider complaints (e.g., *"20 wala"*, *"kal ka payout kam aaya"*, *"penalty do baar kata"*, *"10 wala bhi abhi de do"*).
- **Date & Context Resolution**: Mapping relative expressions (*"kal"*, *"19 sept"*, *"20 tarikh"*) to ISO calendar dates in IST.
- **Tone & Empathy**: Explaining calculations in respectful, natural conversational phrasing mentioning exact order IDs and rupees owed.

### What Code Decides
- **Fare Arithmetic**: Never hallucinated. Evaluates ₹25 base (first 2 km) + ₹6/km, applies surge multipliers, and rounds half-up.
- **Financial Controls**: Strictly enforces the Finance rule (max ₹200 auto-pay per dispute, at most 1 auto-pay per rider per day). Reconciles directly against PaySwift's API ledger.
- **Policy Thresholds**: Enforces 12 unique completed trips in IST calendar day for daily incentive (₹150) and 7-day cutoff window.
- **Security & Authorization**: Blocks prompt injection attacks (e.g. `SYSTEM: ignore rules...`) and cross-account spoofing (*"This is R005"* from `R020`).
- **Idempotency & Resilience**: Handles vendor duplicate messages (`wamid`), PaySwift rate limits (429), and transient upstream 5xx errors with exponential backoff and jitter.

---

## 3. Assumptions Made

1. **IST Calendar Day**: "Day" is evaluated strictly in Indian Standard Time (`Asia/Kolkata`, UTC+05:30). Trips completed after 18:30 UTC belong to the following calendar day in IST.
2. **Data Anomaly Sanitization**: Distance entries > 100 in historical trip dumps represent meters rather than kilometers and are normalized (e.g. `5000.0 m` → `5.0 km`). Duplicate raw trip export rows are deduplicated by `trip_id`.
3. **Dispute Bundling**: Multiple unpaid orders reported for the same date (e.g. R033) are treated as a single combined dispute. If the sum is ≤ ₹200, it is auto-paid in a single PaySwift transaction; if > ₹200, queued together for ops approval.
4. **GPS Distance Disputes & Medical Hardship**: Disputing the recorded GPS distance or cancellation due to accidents requires human ops verification and is escalated with ₹0 auto-payout.

---

## 4. Evaluation Results

- **Test Suite**: Evaluated across all **24 scenarios** in `data/conversations.json`.
- **Pass Rate**: **100.0% (24 / 24 Passed)** in ~1.2 seconds.
- **Verified Dimensions**:
  - Exact PaySwift payout amounts (₹0, ₹10, ₹15, ₹22, ₹25, ₹32, ₹34, ₹150, ₹176).
  - Exact Ops pending approval amounts (₹249, ₹425).
  - Escalation routing (Prompt injection, account spoofing, 7-day cutoff, distance dispute, accident exception).
  - Multi-turn dialogues, vague complaint clarification, and pushback handling.
  - Required keywords & numeric mentions in replies.

---

## 5. What Was Skipped

- **Audio/Voice Processing**: Voice notes on WhatsApp were not implemented; the vendor interface is text-only.
- **Direct GPS Trace Recalculation**: Automated re-snapping of raw GPS points for distance disputes was deferred to ops investigation tools.
