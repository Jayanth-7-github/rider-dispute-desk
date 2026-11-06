from __future__ import annotations
import logging
import os
from datetime import datetime
from typing import Any, Dict, List, Optional
from fastapi import FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse, JSONResponse
from pydantic import BaseModel, Field

from app.agent import DisputeAgent
from app.db import (
    get_all_ops_items,
    get_connection,
    get_ops_item,
    get_pending_ops_items,
    get_recent_messages,
    get_traces_for_rider,
    init_db,
    reset_runtime_state,
    update_ops_item_status,
)
from app.payswift_client import PaySwiftClient
from app.policy import IST, normalize_rider_id

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")
logger = logging.getLogger("server")

app = FastAPI(
    title="QuickDrop Rider Payout Dispute Desk",
    description="Autonomous Agent for rider payout disputes with ops escalation and PaySwift reconciliation",
    version="1.0.0"
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

payswift = PaySwiftClient()
agent = DisputeAgent(payswift_client=payswift)


@app.on_event("startup")
def on_startup():
    init_db()
    logger.info("Database initialized.")


# Pydantic Schemas
class MessageRequest(BaseModel):
    message_id: str
    rider_id: str
    text: str
    received_at: str


class MessageResponse(BaseModel):
    reply: str


class OpsActionRequest(BaseModel):
    action: str = Field(..., description="'approve' or 'reject'")


# Endpoints
@app.get("/health")
def health():
    return {
        "status": "ok",
        "timestamp": datetime.now(IST).isoformat(),
        "payswift_url": payswift.base_url
    }


@app.post("/messages", response_model=MessageResponse)
def handle_message(msg: MessageRequest):
    try:
        reply = agent.process_message(
            message_id=msg.message_id,
            rider_id=msg.rider_id,
            text=msg.text,
            received_at=msg.received_at
        )
        return {"reply": reply}
    except Exception as e:
        logger.exception(f"Error handling message {msg.message_id}: {e}")
        return {"reply": "Aapki request receive ho gayi hai. Hum ise check kar rahe hain, thodi der mein update dete hain."}


@app.get("/trace/{rider_id}")
def get_trace(rider_id: str):
    norm_rider = normalize_rider_id(rider_id)
    return get_traces_for_rider(norm_rider)


@app.get("/ops/pending")
def get_ops_pending():
    return get_pending_ops_items()


@app.post("/ops/pending/{item_id}/action")
def action_ops_item(item_id: str, req: OpsActionRequest):
    item = get_ops_item(item_id)
    if not item:
        raise HTTPException(status_code=404, detail="Item not found")

    action = req.action.lower()
    if action not in ["approve", "reject"]:
        raise HTTPException(status_code=400, detail="Action must be 'approve' or 'reject'")

    if item["status"] != "pending":
        return {"status": "already_processed", "item": item}

    payout_result = None
    if action == "approve" and item["type"] == "approval" and item["amount"]:
        # Execute payout via PaySwift
        payout_result = payswift.create_payout(
            rider_id=item["rider_id"],
            amount=item["amount"],
            reference=f"Ops approval for item {item_id}: {item['reason']}",
            idempotency_key=f"ops_approve_{item_id}"
        )

    new_status = "approved" if action == "approve" else "rejected"
    update_ops_item_status(item_id, new_status)
    updated_item = get_ops_item(item_id)

    return {
        "status": "success",
        "action": action,
        "item": updated_item,
        "payout": payout_result
    }


@app.post("/reset")
def reset_endpoint():
    """Reset runtime database state (messages, traces, pending items)."""
    reset_runtime_state()
    return {"status": "reset_successful"}


# Ops Dashboard HTML Page
@app.get("/ops", response_class=HTMLResponse)
@app.get("/", response_class=HTMLResponse)
def ops_dashboard():
    return """
<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
  <title>QuickDrop - Rider Payout Dispute Desk</title>
  <script src="https://cdn.tailwindcss.com"></script>
  <style>
    @keyframes pulse-subtle {
      0%, 100% { opacity: 1; }
      50% { opacity: 0.6; }
    }
    .live-dot { animation: pulse-subtle 2s infinite ease-in-out; }
  </style>
</head>
<body class="bg-slate-900 text-slate-100 min-h-screen font-sans">
  <!-- Top Navigation -->
  <header class="bg-slate-800 border-b border-slate-700 px-6 py-4 flex items-center justify-between sticky top-0 z-50">
    <div class="flex items-center gap-3">
      <div class="w-9 h-9 rounded-lg bg-emerald-500/20 text-emerald-400 flex items-center justify-center font-bold text-xl border border-emerald-500/40">
        ⚡
      </div>
      <div>
        <h1 class="text-lg font-bold text-white flex items-center gap-2">
          QuickDrop Ops Desk
          <span class="text-xs px-2 py-0.5 rounded-full bg-emerald-500/20 text-emerald-300 font-medium border border-emerald-500/30">
            Autonomous Agent Active
          </span>
        </h1>
        <p class="text-xs text-slate-400">Rider Payout Dispute Handling & Ops Action Console</p>
      </div>
    </div>
    <div class="flex items-center gap-4 text-xs">
      <div class="flex items-center gap-2 bg-slate-900 px-3 py-1.5 rounded-lg border border-slate-700">
        <span class="w-2 h-2 rounded-full bg-emerald-400 live-dot"></span>
        <span class="text-slate-300">PaySwift API: Connected</span>
      </div>
      <button onclick="refreshAll()" class="px-3 py-1.5 bg-slate-700 hover:bg-slate-600 rounded-lg text-slate-200 transition font-medium">
        ↻ Refresh
      </button>
    </div>
  </header>

  <!-- Main Grid -->
  <main class="p-6 max-w-7xl mx-auto space-y-6">
    <!-- Stat Cards -->
    <div class="grid grid-cols-1 md:grid-cols-4 gap-4">
      <div class="bg-slate-800 border border-slate-700 rounded-xl p-4 shadow-sm">
        <p class="text-xs font-semibold text-slate-400 uppercase tracking-wider">Pending Approvals</p>
        <p id="stat-approvals" class="text-2xl font-bold text-amber-400 mt-1">0</p>
        <p class="text-xs text-slate-500 mt-1">Waiting for ops clearance</p>
      </div>
      <div class="bg-slate-800 border border-slate-700 rounded-xl p-4 shadow-sm">
        <p class="text-xs font-semibold text-slate-400 uppercase tracking-wider">Escalations</p>
        <p id="stat-escalations" class="text-2xl font-bold text-rose-400 mt-1">0</p>
        <p class="text-xs text-slate-500 mt-1">Exceptions & security alerts</p>
      </div>
      <div class="bg-slate-800 border border-slate-700 rounded-xl p-4 shadow-sm">
        <p class="text-xs font-semibold text-slate-400 uppercase tracking-wider">Auto-Pay Rule</p>
        <p class="text-2xl font-bold text-emerald-400 mt-1">≤ ₹200</p>
        <p class="text-xs text-slate-500 mt-1">1 auto-pay / rider / day</p>
      </div>
      <div class="bg-slate-800 border border-slate-700 rounded-xl p-4 shadow-sm">
        <p class="text-xs font-semibold text-slate-400 uppercase tracking-wider">Policy Window</p>
        <p class="text-2xl font-bold text-sky-400 mt-1">7 Days</p>
        <p class="text-xs text-slate-500 mt-1">Calendar day IST boundary</p>
      </div>
    </div>

    <!-- Section 1: Pending Queue -->
    <section class="bg-slate-800 border border-slate-700 rounded-xl overflow-hidden shadow">
      <div class="px-5 py-4 border-b border-slate-700 flex justify-between items-center bg-slate-800/80">
        <div>
          <h2 class="font-bold text-white text-base">Ops Action Queue</h2>
          <p class="text-xs text-slate-400">Items requiring human clearance (amounts > ₹200, repeat daily disputes, or exceptions)</p>
        </div>
        <span id="pending-count-badge" class="px-2.5 py-1 bg-amber-500/20 text-amber-300 border border-amber-500/30 rounded-full text-xs font-semibold">
          0 Items
        </span>
      </div>
      <div class="overflow-x-auto">
        <table class="w-full text-left text-xs text-slate-300">
          <thead class="bg-slate-900/60 uppercase font-semibold text-slate-400 border-b border-slate-700">
            <tr>
              <th class="px-4 py-3">Item ID</th>
              <th class="px-4 py-3">Rider</th>
              <th class="px-4 py-3">Type</th>
              <th class="px-4 py-3">Amount</th>
              <th class="px-4 py-3">Reason</th>
              <th class="px-4 py-3">Created</th>
              <th class="px-4 py-3 text-right">Action</th>
            </tr>
          </thead>
          <tbody id="pending-table-body" class="divide-y divide-slate-700/60 font-mono">
            <tr>
              <td colspan="7" class="px-4 py-6 text-center text-slate-500 font-sans">No pending items. All clear!</td>
            </tr>
          </tbody>
        </table>
      </div>
    </section>

    <!-- Section 2: Rider Trace & Audit Inspector -->
    <section class="bg-slate-800 border border-slate-700 rounded-xl p-5 shadow space-y-4">
      <div class="flex flex-col md:flex-row md:items-center justify-between gap-3">
        <div>
          <h2 class="font-bold text-white text-base">Audit Trace Inspector</h2>
          <p class="text-xs text-slate-400">Inspect the agent's decision steps, tool calls, and calculations for any rider</p>
        </div>
        <div class="flex items-center gap-2">
          <input id="rider-input" type="text" placeholder="Enter Rider ID (e.g. R003, R016)" 
                 class="px-3 py-1.5 bg-slate-900 border border-slate-700 rounded-lg text-xs text-white placeholder-slate-500 focus:outline-none focus:border-emerald-500">
          <button onclick="inspectRider()" class="px-4 py-1.5 bg-emerald-600 hover:bg-emerald-500 text-white rounded-lg text-xs font-semibold transition">
            Inspect Trace
          </button>
        </div>
      </div>

      <div id="trace-container" class="bg-slate-950 border border-slate-800 rounded-xl p-4 min-h-[180px] max-h-[420px] overflow-y-auto font-mono text-xs text-slate-300 space-y-3">
        <p class="text-slate-500 font-sans text-center py-8">Select or enter a Rider ID above to view the full audit trace.</p>
      </div>
    </section>
  </main>

  <script>
    async function fetchPending() {
      try {
        const res = await fetch('/ops/pending');
        const items = await res.json();
        const tbody = document.getElementById('pending-table-body');
        const countBadge = document.getElementById('pending-count-badge');
        
        let approvals = 0;
        let escalations = 0;
        items.forEach(i => i.type === 'approval' ? approvals++ : escalations++);
        document.getElementById('stat-approvals').innerText = approvals;
        document.getElementById('stat-escalations').innerText = escalations;
        countBadge.innerText = `${items.length} Items`;

        if (!items || items.length === 0) {
          tbody.innerHTML = '<tr><td colspan="7" class="px-4 py-6 text-center text-slate-500 font-sans">No pending items. All clear!</td></tr>';
          return;
        }

        tbody.innerHTML = items.map(item => `
          <tr class="hover:bg-slate-750 transition">
            <td class="px-4 py-3 text-slate-400 font-semibold">${item.id}</td>
            <td class="px-4 py-3 text-white font-bold cursor-pointer hover:underline" onclick="selectRider('${item.rider_id}')">${item.rider_id}</td>
            <td class="px-4 py-3">
              <span class="px-2 py-0.5 rounded text-[11px] font-semibold ${item.type === 'approval' ? 'bg-amber-500/20 text-amber-300 border border-amber-500/30' : 'bg-rose-500/20 text-rose-300 border border-rose-500/30'}">
                ${item.type.toUpperCase()}
              </span>
            </td>
            <td class="px-4 py-3 font-semibold text-emerald-400">${item.amount ? '₹' + item.amount : '-'}</td>
            <td class="px-4 py-3 max-w-xs truncate text-slate-300 font-sans" title="${item.reason}">${item.reason}</td>
            <td class="px-4 py-3 text-slate-400 text-[11px] font-sans">${new Date(item.created_at).toLocaleTimeString()}</td>
            <td class="px-4 py-3 text-right space-x-2 font-sans">
              <button onclick="actionItem('${item.id}', 'approve')" class="px-2.5 py-1 bg-emerald-600 hover:bg-emerald-500 text-white rounded font-medium text-xs transition">
                Approve
              </button>
              <button onclick="actionItem('${item.id}', 'reject')" class="px-2.5 py-1 bg-rose-600/80 hover:bg-rose-500 text-white rounded font-medium text-xs transition">
                Reject
              </button>
            </td>
          </tr>
        `).join('');
      } catch (e) {
        console.error("Error fetching pending:", e);
      }
    }

    async function actionItem(itemId, action) {
      if (!confirm(`Are you sure you want to ${action.toUpperCase()} item ${itemId}?`)) return;
      try {
        const res = await fetch(`/ops/pending/${itemId}/action`, {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ action: action })
        });
        const data = await res.json();
        alert(`Item ${itemId} ${action}d successfully!`);
        fetchPending();
      } catch (e) {
        alert("Action failed: " + e.message);
      }
    }

    function selectRider(riderId) {
      document.getElementById('rider-input').value = riderId;
      inspectRider();
    }

    async function inspectRider() {
      const riderId = document.getElementById('rider-input').value.trim();
      if (!riderId) return;
      const container = document.getElementById('trace-container');
      container.innerHTML = '<p class="text-slate-500 text-center py-4 font-sans">Loading trace...</p>';
      try {
        const res = await fetch(`/trace/${riderId}`);
        const trace = await res.json();
        if (!trace || trace.length === 0) {
          container.innerHTML = `<p class="text-slate-500 text-center py-6 font-sans">No trace found for rider ${riderId}.</p>`;
          return;
        }

        container.innerHTML = trace.map(step => {
          let badgeColor = 'bg-slate-700 text-slate-300';
          if (step.type === 'message_in') badgeColor = 'bg-sky-500/20 text-sky-300 border border-sky-500/30';
          if (step.type === 'tool_call') badgeColor = 'bg-purple-500/20 text-purple-300 border border-purple-500/30';
          if (step.type === 'decision') badgeColor = 'bg-amber-500/20 text-amber-300 border border-amber-500/30';
          if (step.type === 'reply') badgeColor = 'bg-emerald-500/20 text-emerald-300 border border-emerald-500/30';

          return `
            <div class="border border-slate-800 bg-slate-900/80 rounded-lg p-3 space-y-1.5">
              <div class="flex items-center justify-between text-[11px]">
                <div class="flex items-center gap-2">
                  <span class="px-2 py-0.5 rounded text-[10px] uppercase font-bold ${badgeColor}">
                    ${step.type}
                  </span>
                  <span class="text-slate-200 font-semibold">${step.name}</span>
                </div>
                <span class="text-slate-500 font-sans">${step.at}</span>
              </div>
              ${step.input ? `
                <div class="text-[11px] text-slate-400 bg-slate-950 p-2 rounded border border-slate-850">
                  <span class="text-slate-500 font-semibold block text-[10px] uppercase">Input:</span>
                  <pre class="overflow-x-auto whitespace-pre-wrap">${JSON.stringify(step.input, null, 2)}</pre>
                </div>
              ` : ''}
              ${step.output ? `
                <div class="text-[11px] text-emerald-300 bg-slate-950 p-2 rounded border border-slate-850">
                  <span class="text-slate-500 font-semibold block text-[10px] uppercase">Output:</span>
                  <pre class="overflow-x-auto whitespace-pre-wrap">${typeof step.output === 'object' ? JSON.stringify(step.output, null, 2) : step.output}</pre>
                </div>
              ` : ''}
            </div>
          `;
        }).join('');
      } catch (e) {
        container.innerHTML = `<p class="text-rose-400 text-center py-4 font-sans">Failed to load trace: ${e.message}</p>`;
      }
    }

    function refreshAll() {
      fetchPending();
      inspectRider();
    }

    fetchPending();
    setInterval(fetchPending, 10000);
  </script>
</body>
</html>
    """

