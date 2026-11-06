from __future__ import annotations
import logging
import os
from datetime import datetime
from typing import Any, Dict, List, Optional
from fastapi import FastAPI, HTTPException, Request, Response
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
    get_rider,
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


@app.get("/ops/riders")
def list_riders_api():
    conn = get_connection()
    c = conn.cursor()
    c.execute("""
        SELECT r.rider_id, r.name, r.city, r.joined_on,
               COUNT(DISTINCT m.message_id) as message_count
        FROM riders r
        LEFT JOIN messages m ON r.rider_id = m.rider_id
        GROUP BY r.rider_id
        ORDER BY message_count DESC, r.rider_id ASC
        LIMIT 25
    """)
    rows = c.fetchall()
    conn.close()
    return [dict(r) for r in rows]


@app.get("/ops/messages/{rider_id}")
def get_rider_messages_api(rider_id: str):
    norm = normalize_rider_id(rider_id)
    rider_info = get_rider(norm)
    messages = get_recent_messages(norm)
    return {"rider": rider_info, "messages": messages}


@app.get("/ops/payouts")
def get_recent_payouts_api():
    payouts = payswift.get_payouts()
    return payouts[-12:] if payouts else []


@app.post("/reset")
def reset_endpoint():
    reset_runtime_state()
    return {"status": "reset_successful"}


# Ops Dashboard HTML Page
@app.get("/ops", response_class=HTMLResponse)
@app.get("/", response_class=HTMLResponse)
def ops_dashboard(response: Response):
    response.headers["Cache-Control"] = "no-cache, no-store, must-revalidate"
    response.headers["Pragma"] = "no-cache"
    response.headers["Expires"] = "0"

    html = """<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
  <title>QuickDrop — Rider Payout Dispute Desk</title>
  <link rel="preconnect" href="https://fonts.googleapis.com">
  <link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
  <link href="https://fonts.googleapis.com/css2?family=Plus+Jakarta+Sans:wght@300;400;500;600;700;800&family=JetBrains+Mono:wght@400;500;600;700&display=swap" rel="stylesheet">
  <script src="https://cdn.tailwindcss.com"></script>

  <style>
    /* Direct Hardcoded Luxury Obsidian & Radiant Gold Palette */
    :root {
      --bg-black: #050505;
      --card-black: #0c0c0c;
      --card-border: rgba(229, 184, 66, 0.22);
      --gold-primary: #F5C542;
      --gold-light: #FFF0B3;
      --gold-dim: #9E7A22;
      --gold-glow: rgba(245, 197, 66, 0.12);
    }

    body {
      background-color: #040404 !important;
      color: #e5e5e5 !important;
      font-family: 'Plus Jakarta Sans', sans-serif !important;
    }

    .gold-header {
      background: linear-gradient(135deg, #FFF8DC 0%, #F5C542 45%, #E5B842 75%, #B8860B 100%) !important;
      -webkit-background-clip: text !important;
      -webkit-text-fill-color: transparent !important;
      font-weight: 800 !important;
      letter-spacing: 0.04em !important;
    }

    .gold-card {
      background: #0a0a0a !important;
      border: 1px solid rgba(229, 184, 66, 0.22) !important;
      box-shadow: 0 4px 24px rgba(0, 0, 0, 0.8), 0 0 15px rgba(229, 184, 66, 0.05) !important;
      border-radius: 16px !important;
    }

    .gold-card:hover {
      border-color: rgba(245, 197, 66, 0.45) !important;
      box-shadow: 0 6px 30px rgba(0, 0, 0, 0.9), 0 0 25px rgba(229, 184, 66, 0.12) !important;
    }

    .gold-btn {
      background: linear-gradient(135deg, #FCE07A 0%, #F5C542 50%, #C69226 100%) !important;
      color: #000000 !important;
      font-weight: 800 !important;
      border: none !important;
      box-shadow: 0 2px 10px rgba(245, 197, 66, 0.3) !important;
      transition: all 0.2s ease !important;
    }
    .gold-btn:hover {
      background: linear-gradient(135deg, #FFF3B8 0%, #FCE07A 50%, #D4A017 100%) !important;
      box-shadow: 0 4px 18px rgba(245, 197, 66, 0.45) !important;
      transform: translateY(-1px) !important;
    }

    .gold-btn-outline {
      background: rgba(245, 197, 66, 0.05) !important;
      border: 1px solid rgba(245, 197, 66, 0.35) !important;
      color: #FCE07A !important;
      transition: all 0.2s ease !important;
    }
    .gold-btn-outline:hover {
      background: rgba(245, 197, 66, 0.15) !important;
      border-color: #F5C542 !important;
      color: #FFFFFF !important;
    }

    .gold-badge {
      background: rgba(245, 197, 66, 0.12) !important;
      border: 1px solid rgba(245, 197, 66, 0.35) !important;
      color: #FCE07A !important;
    }

    .gold-input {
      background: #0e0e0e !important;
      border: 1px solid rgba(229, 184, 66, 0.25) !important;
      color: #FFFFFF !important;
    }
    .gold-input:focus {
      border-color: #F5C542 !important;
      outline: none !important;
      box-shadow: 0 0 10px rgba(245, 197, 66, 0.25) !important;
    }

    /* Custom Scrollbar */
    ::-webkit-scrollbar { width: 6px; height: 6px; }
    ::-webkit-scrollbar-track { background: #050505; }
    ::-webkit-scrollbar-thumb { background: #2f2511; border-radius: 4px; border: 1px solid rgba(229, 184, 66, 0.3); }
    ::-webkit-scrollbar-thumb:hover { background: #5c4314; }

    @keyframes pulse-ring {
      0%, 100% { transform: scale(1); opacity: 1; box-shadow: 0 0 0 0 rgba(245, 197, 66, 0.7); }
      50% { transform: scale(1.1); opacity: 0.8; box-shadow: 0 0 10px 4px rgba(245, 197, 66, 0.8); }
    }
    .gold-pulse-indicator { animation: pulse-ring 2.2s infinite ease-in-out; }
  </style>
</head>
<body class="min-h-screen font-sans selection:bg-amber-400 selection:text-black antialiased">

  <!-- TOP EXECUTIVE NAVIGATION BAR -->
  <header style="background: rgba(8, 8, 8, 0.95); border-bottom: 1px solid rgba(229, 184, 66, 0.25);" class="backdrop-blur-xl sticky top-0 z-50">
    <div class="max-w-7xl mx-auto px-6 py-4 flex flex-col md:flex-row items-center justify-between gap-4">
      <div class="flex items-center gap-3.5">
        <div style="background: linear-gradient(135deg, #FCE07A, #C69226); padding: 1px; border-radius: 12px;">
          <div style="background: #070707; border-radius: 11px; width: 38px; height: 38px;" class="flex items-center justify-center font-bold text-amber-300 text-lg">
            ⚡
          </div>
        </div>
        <div>
          <div class="flex items-center gap-2">
            <h1 class="text-lg font-bold tracking-wider gold-header uppercase">
              QuickDrop Dispute Desk
            </h1>
            <span class="gold-badge text-[10px] uppercase font-mono px-2 py-0.5 rounded-full font-bold">
              Autonomous Agent Active
            </span>
          </div>
          <p class="text-xs text-zinc-400">Rider Payout Dispute Investigation & Autonomous Bank Clearance</p>
        </div>
      </div>

      <div class="flex items-center gap-3 text-xs">
        <div style="background: #0a0a0a; border: 1px solid rgba(229, 184, 66, 0.25);" class="flex items-center gap-2 px-3 py-1.5 rounded-lg">
          <span style="background: #F5C542;" class="w-2 h-2 rounded-full gold-pulse-indicator"></span>
          <span class="font-mono text-amber-300 font-semibold">PaySwift: Connected</span>
        </div>
        <div style="background: #0a0a0a; border: 1px solid rgba(229, 184, 66, 0.25);" class="flex items-center gap-2 px-3 py-1.5 rounded-lg font-mono text-zinc-400">
          <span>IST: UTC+05:30</span>
        </div>
        <button onclick="refreshDashboard()" class="gold-btn px-3.5 py-1.5 rounded-lg text-xs font-bold cursor-pointer">
          ↻ Refresh Data
        </button>
      </div>
    </div>
  </header>

  <!-- MAIN WRAPPER -->
  <main class="max-w-7xl mx-auto p-6 space-y-6">

    <!-- EXECUTIVE METRICS STRIP -->
    <div class="grid grid-cols-1 sm:grid-cols-2 lg:grid-cols-4 gap-4">
      <!-- Metric 1 -->
      <div class="gold-card p-5">
        <div class="flex justify-between items-start">
          <p class="text-[11px] font-mono font-bold uppercase tracking-wider text-amber-300/80">Pending Approvals</p>
          <span class="text-amber-400 text-sm">✦</span>
        </div>
        <p id="stat-approvals" class="text-3xl font-extrabold gold-header mt-2">0</p>
        <p class="text-[11px] text-zinc-400 mt-1">Requires human clearance (> ₹200 / repeat)</p>
      </div>

      <!-- Metric 2 -->
      <div class="gold-card p-5">
        <div class="flex justify-between items-start">
          <p class="text-[11px] font-mono font-bold uppercase tracking-wider text-amber-300/80">Ops Escalations</p>
          <span class="text-amber-400 text-sm">⚠</span>
        </div>
        <p id="stat-escalations" class="text-3xl font-extrabold text-amber-200 mt-2">0</p>
        <p class="text-[11px] text-zinc-400 mt-1">Policy exceptions, fraud & security alerts</p>
      </div>

      <!-- Metric 3 -->
      <div class="gold-card p-5">
        <div class="flex justify-between items-start">
          <p class="text-[11px] font-mono font-bold uppercase tracking-wider text-amber-300/80">Auto-Pay Rule</p>
          <span class="text-amber-400 text-sm">₹</span>
        </div>
        <p class="text-3xl font-extrabold gold-header mt-2">≤ ₹200</p>
        <p class="text-[11px] text-zinc-400 mt-1">Max 1 auto-pay / rider / IST calendar day</p>
      </div>

      <!-- Metric 4 -->
      <div class="gold-card p-5">
        <div class="flex justify-between items-start">
          <p class="text-[11px] font-mono font-bold uppercase tracking-wider text-amber-300/80">Policy Window</p>
          <span class="text-amber-400 text-sm">⏳</span>
        </div>
        <p class="text-3xl font-extrabold gold-header mt-2">7 Days</p>
        <p class="text-[11px] text-zinc-400 mt-1">Strict IST calendar boundary</p>
      </div>
    </div>

    <!-- MAIN TWO-COLUMN SPLIT PANE -->
    <div class="grid grid-cols-1 lg:grid-cols-12 gap-6">

      <!-- LEFT COLUMN: OPS PENDING QUEUE (7 Cols) -->
      <div class="lg:col-span-7 space-y-6">
        <section class="gold-card overflow-hidden">
          <div style="background: #080808; border-bottom: 1px solid rgba(229, 184, 66, 0.2);" class="p-5 flex items-center justify-between">
            <div>
              <h2 class="text-base font-bold text-white tracking-wide gold-header flex items-center gap-2">
                Operations Action Queue
              </h2>
              <p class="text-xs text-zinc-400">Items awaiting human authorization before fund disbursement</p>
            </div>
            <span id="pending-count-badge" class="gold-badge px-3 py-1 rounded-full text-[11px] font-mono font-bold">
              0 Items
            </span>
          </div>

          <!-- Table Container -->
          <div class="overflow-x-auto">
            <table class="w-full text-left text-xs">
              <thead style="background: #060606; border-bottom: 1px solid rgba(229, 184, 66, 0.2);" class="font-mono text-[10px] uppercase tracking-wider text-amber-300/90">
                <tr>
                  <th class="px-4 py-3.5">Item</th>
                  <th class="px-4 py-3.5">Rider</th>
                  <th class="px-4 py-3.5">Type</th>
                  <th class="px-4 py-3.5">Amount</th>
                  <th class="px-4 py-3.5">Reason</th>
                  <th class="px-4 py-3.5 text-right">Clearance</th>
                </tr>
              </thead>
              <tbody id="pending-table-body" class="divide-y divide-zinc-900 text-zinc-300 font-mono">
                <tr>
                  <td colspan="6" class="px-4 py-10 text-center text-zinc-500 font-sans">
                    No pending items. The queue is fully cleared.
                  </td>
                </tr>
              </tbody>
            </table>
          </div>
        </section>

        <!-- ACTIVE RIDERS FAST-SWITCHER -->
        <section class="gold-card p-5">
          <div class="flex items-center justify-between mb-3.5">
            <h3 class="text-sm font-bold gold-header uppercase tracking-wider">
              Recent Rider Directory
            </h3>
            <span class="text-[11px] text-zinc-400 font-mono">Click to inspect</span>
          </div>
          <div id="riders-list" class="flex flex-wrap gap-2">
            <span class="text-xs text-zinc-500">Loading riders...</span>
          </div>
        </section>
      </div>

      <!-- RIGHT COLUMN: RIDER AUDIT & CHAT EXPLORER (5 Cols) -->
      <div class="lg:col-span-5 space-y-6">
        <section class="gold-card p-5 flex flex-col h-full min-h-[580px]">
          
          <!-- Dossier Top Bar -->
          <div style="border-bottom: 1px solid rgba(229, 184, 66, 0.2);" class="flex items-center justify-between pb-4">
            <div class="flex items-center gap-3">
              <div style="background: #141208; border: 1px solid rgba(229, 184, 66, 0.4);" class="w-10 h-10 rounded-full flex items-center justify-center font-bold text-amber-300 font-mono text-sm">
                <span id="dossier-avatar">R03</span>
              </div>
              <div>
                <h3 id="dossier-rider-id" class="font-bold text-white text-sm tracking-wide">R003 — Imran Khan</h3>
                <p id="dossier-meta" class="text-xs text-zinc-400 font-mono">Bengaluru · Joined 2024-03-15</p>
              </div>
            </div>
            <div style="background: #050505; border: 1px solid rgba(229, 184, 66, 0.25);" class="flex gap-1.5 p-1 rounded-lg font-mono text-[11px]">
              <button onclick="switchTab('trace')" id="tab-btn-trace" class="px-3 py-1 rounded-md font-bold gold-btn">
                Trace
              </button>
              <button onclick="switchTab('chat')" id="tab-btn-chat" class="px-3 py-1 rounded-md font-semibold text-zinc-400 hover:text-white">
                Chat
              </button>
            </div>
          </div>

          <!-- Trace View Tab -->
          <div id="tab-content-trace" class="flex-1 mt-4 overflow-y-auto space-y-3 max-h-[520px] pr-1">
            <div class="text-center py-12 text-zinc-500 font-mono text-xs">
              Select any rider or pending item on the left to review their step-by-step autonomous trace.
            </div>
          </div>

          <!-- Chat View Tab (Hidden by default) -->
          <div id="tab-content-chat" class="hidden flex-1 mt-4 overflow-y-auto space-y-3 max-h-[520px] pr-1 font-sans">
            <div class="text-center py-12 text-zinc-500 font-mono text-xs">
              No messages selected.
            </div>
          </div>

        </section>
      </div>

    </div>

    <!-- BOTTOM SECTION: PAYSWIFT LIVE LEDGER MONITOR -->
    <section class="gold-card p-5">
      <div class="flex items-center justify-between mb-4">
        <div class="flex items-center gap-2.5">
          <span style="background: #F5C542;" class="w-2.5 h-2.5 rounded-full gold-pulse-indicator"></span>
          <h3 class="text-sm font-bold gold-header uppercase tracking-wider">
            PaySwift Live Banking Ledger
          </h3>
        </div>
        <p class="text-[11px] text-zinc-400 font-mono">Reconciled against bank sandbox in real time</p>
      </div>

      <div class="overflow-x-auto">
        <table class="w-full text-left text-xs font-mono">
          <thead style="background: #060606; border-bottom: 1px solid rgba(229, 184, 66, 0.2);" class="uppercase text-[10px] tracking-wider text-amber-300/80">
            <tr>
              <th class="px-4 py-2.5">Payout ID</th>
              <th class="px-4 py-2.5">Rider</th>
              <th class="px-4 py-2.5">Amount</th>
              <th class="px-4 py-2.5">Reference</th>
              <th class="px-4 py-2.5">Status</th>
              <th class="px-4 py-2.5 text-right">Timestamp</th>
            </tr>
          </thead>
          <tbody id="payswift-ledger-body" class="divide-y divide-zinc-900 text-zinc-300">
            <tr>
              <td colspan="6" class="px-4 py-6 text-center text-zinc-500 font-sans">No recent transactions recorded yet.</td>
            </tr>
          </tbody>
        </table>
      </div>
    </section>

  </main>

  <script>
    let activeRider = 'R003';
    let currentTab = 'trace';

    function switchTab(tab) {
      currentTab = tab;
      const traceBtn = document.getElementById('tab-btn-trace');
      const chatBtn = document.getElementById('tab-btn-chat');
      const traceContent = document.getElementById('tab-content-trace');
      const chatContent = document.getElementById('tab-content-chat');

      if (tab === 'trace') {
        traceBtn.className = 'px-3 py-1 rounded-md font-bold gold-btn';
        chatBtn.className = 'px-3 py-1 rounded-md font-semibold text-zinc-400 hover:text-white';
        traceContent.classList.remove('hidden');
        chatContent.classList.add('hidden');
      } else {
        chatBtn.className = 'px-3 py-1 rounded-md font-bold gold-btn';
        traceBtn.className = 'px-3 py-1 rounded-md font-semibold text-zinc-400 hover:text-white';
        chatContent.classList.remove('hidden');
        traceContent.classList.add('hidden');
      }
    }

    async function fetchPending() {
      try {
        const res = await fetch('/ops/pending');
        const items = await res.json();
        const tbody = document.getElementById('pending-table-body');
        const badge = document.getElementById('pending-count-badge');

        let approvals = 0;
        let escalations = 0;
        items.forEach(i => i.type === 'approval' ? approvals++ : escalations++);
        document.getElementById('stat-approvals').innerText = approvals;
        document.getElementById('stat-escalations').innerText = escalations;
        badge.innerText = `${items.length} Pending`;

        if (!items || items.length === 0) {
          tbody.innerHTML = '<tr><td colspan="6" class="px-4 py-8 text-center text-zinc-500 font-sans">All pending items are cleared. Operations desk is running smoothly.</td></tr>';
          return;
        }

        tbody.innerHTML = items.map(item => `
          <tr class="hover:bg-zinc-900/60 transition">
            <td class="px-4 py-3 text-amber-400 font-bold">${item.id}</td>
            <td class="px-4 py-3 text-white font-bold cursor-pointer hover:underline hover:text-amber-300" onclick="selectRider('${item.rider_id}')">
              ${item.rider_id}
            </td>
            <td class="px-4 py-3">
              <span class="px-2 py-0.5 rounded text-[10px] tracking-wider font-bold ${
                item.type === 'approval' 
                  ? 'gold-badge' 
                  : 'bg-amber-900/30 text-amber-300 border border-amber-600/40'
              }">
                ${item.type.toUpperCase()}
              </span>
            </td>
            <td class="px-4 py-3 font-bold text-amber-300">${item.amount ? '₹' + item.amount : '—'}</td>
            <td class="px-4 py-3 text-zinc-300 max-w-xs truncate font-sans" title="${item.reason}">${item.reason}</td>
            <td class="px-4 py-3 text-right space-x-2 font-sans">
              <button onclick="actionItem('${item.id}', 'approve')" class="gold-btn px-2.5 py-1 rounded text-[11px] cursor-pointer">
                Approve
              </button>
              <button onclick="actionItem('${item.id}', 'reject')" class="gold-btn-outline px-2.5 py-1 rounded text-[11px] cursor-pointer hover:text-rose-300">
                Reject
              </button>
            </td>
          </tr>
        `).join('');
      } catch (e) {
        console.error("Error loading pending queue:", e);
      }
    }

    async function actionItem(id, action) {
      if (!confirm(`Are you sure you want to ${action.toUpperCase()} item ${id}?`)) return;
      try {
        const res = await fetch(`/ops/pending/${id}/action`, {
          method: 'POST',
          headers: {'Content-Type': 'application/json'},
          body: JSON.stringify({action: action})
        });
        const data = await res.json();
        fetchPending();
        fetchPayouts();
        selectRider(activeRider);
      } catch (e) {
        alert("Action failed: " + e.message);
      }
    }

    async function fetchRiders() {
      try {
        const res = await fetch('/ops/riders');
        const riders = await res.json();
        const container = document.getElementById('riders-list');
        if (!riders || riders.length === 0) return;

        container.innerHTML = riders.map(r => `
          <button onclick="selectRider('${r.rider_id}')" class="px-2.5 py-1 rounded-lg text-xs font-mono font-semibold transition cursor-pointer ${
            activeRider === r.rider_id 
              ? 'gold-btn' 
              : 'gold-btn-outline'
          }">
            ${r.rider_id} ${r.message_count > 0 ? `<span class="opacity-75 text-[10px]">(${r.message_count})</span>` : ''}
          </button>
        `).join('');
      } catch (e) {
        console.error("Error loading riders:", e);
      }
    }

    async function selectRider(riderId) {
      activeRider = riderId;
      fetchRiders();

      document.getElementById('dossier-avatar').innerText = riderId.replace('R0', 'R').substring(0, 3);
      document.getElementById('dossier-rider-id').innerText = `${riderId} — Loading...`;

      loadTrace(riderId);
      loadChat(riderId);
    }

    async function loadTrace(riderId) {
      const container = document.getElementById('tab-content-trace');
      container.innerHTML = '<p class="text-zinc-500 text-center py-8 font-mono text-xs">Loading audit trace...</p>';
      try {
        const res = await fetch(`/trace/${riderId}`);
        const trace = await res.json();
        if (!trace || trace.length === 0) {
          container.innerHTML = `<p class="text-zinc-500 text-center py-10 font-mono text-xs">No audit trace recorded yet for ${riderId}.</p>`;
          return;
        }

        container.innerHTML = trace.map(step => {
          let badgeClass = 'gold-badge';
          if (step.type === 'message_in') badgeClass = 'bg-sky-950/60 text-sky-300 border border-sky-600/40';
          if (step.type === 'tool_call') badgeClass = 'gold-badge';
          if (step.type === 'decision') badgeClass = 'bg-amber-950/60 text-amber-200 border border-amber-600/40';
          if (step.type === 'reply') badgeClass = 'bg-emerald-950/60 text-emerald-300 border border-emerald-600/40';

          return `
            <div style="background: #070707; border: 1px solid rgba(229, 184, 66, 0.2);" class="p-3.5 rounded-xl space-y-1.5 text-xs font-mono">
              <div class="flex items-center justify-between">
                <div class="flex items-center gap-2">
                  <span class="px-2 py-0.5 rounded text-[10px] uppercase font-bold ${badgeClass}">
                    ${step.type}
                  </span>
                  <span class="font-bold text-amber-200">${step.name}</span>
                </div>
                <span class="text-[10px] text-zinc-500">${new Date(step.at).toLocaleTimeString()}</span>
              </div>
              ${step.input ? `
                <div style="background: #020202; border: 1px solid rgba(229, 184, 66, 0.1);" class="mt-1.5 p-2 rounded-lg text-[11px] text-zinc-400">
                  <span class="text-amber-400/80 font-bold block text-[10px] uppercase">Input</span>
                  <pre class="overflow-x-auto whitespace-pre-wrap">${JSON.stringify(step.input, null, 2)}</pre>
                </div>
              ` : ''}
              ${step.output ? `
                <div style="background: #020202; border: 1px solid rgba(229, 184, 66, 0.1);" class="mt-1.5 p-2 rounded-lg text-[11px] text-amber-200/90">
                  <span class="text-amber-400/80 font-bold block text-[10px] uppercase">Output</span>
                  <pre class="overflow-x-auto whitespace-pre-wrap">${typeof step.output === 'object' ? JSON.stringify(step.output, null, 2) : step.output}</pre>
                </div>
              ` : ''}
            </div>
          `;
        }).join('');
      } catch (e) {
        container.innerHTML = `<p class="text-rose-400 text-center py-6 font-mono text-xs">Error: ${e.message}</p>`;
      }
    }

    async function loadChat(riderId) {
      const container = document.getElementById('tab-content-chat');
      try {
        const res = await fetch(`/ops/messages/${riderId}`);
        const data = await res.json();
        
        if (data.rider) {
          document.getElementById('dossier-rider-id').innerText = `${data.rider.rider_id} — ${data.rider.name}`;
          document.getElementById('dossier-meta').innerText = `${data.rider.city} · Joined ${data.rider.joined_on}`;
        }

        const messages = data.messages || [];
        if (messages.length === 0) {
          container.innerHTML = `<p class="text-zinc-500 text-center py-10 font-mono text-xs">No WhatsApp messages from ${riderId}.</p>`;
          return;
        }

        container.innerHTML = messages.map(m => `
          <div class="space-y-2">
            <!-- Rider message -->
            <div class="flex justify-start">
              <div style="background: #0a0a0a; border: 1px solid rgba(229, 184, 66, 0.2);" class="max-w-[85%] rounded-2xl rounded-tl-sm px-4 py-2.5 text-xs text-zinc-200">
                <p class="font-bold text-[10px] text-amber-400/90 mb-0.5">Rider (${m.rider_id})</p>
                <p>${m.text}</p>
                <span class="text-[9px] text-zinc-500 block text-right mt-1 font-mono">${new Date(m.received_at).toLocaleTimeString()}</span>
              </div>
            </div>
            <!-- Agent reply -->
            ${m.reply ? `
              <div class="flex justify-end">
                <div style="background: rgba(245, 197, 66, 0.1); border: 1px solid rgba(245, 197, 66, 0.35);" class="max-w-[85%] rounded-2xl rounded-tr-sm px-4 py-2.5 text-xs text-amber-100">
                  <p class="font-bold text-[10px] text-amber-300 mb-0.5">QuickDrop Agent ⚡</p>
                  <p>${m.reply}</p>
                  <span class="text-[9px] text-amber-400/70 block text-right mt-1 font-mono">${new Date(m.created_at).toLocaleTimeString()}</span>
                </div>
              </div>
            ` : ''}
          </div>
        `).join('');
      } catch (e) {
        console.error("Error loading chat:", e);
      }
    }

    async function fetchPayouts() {
      try {
        const res = await fetch('/ops/payouts');
        const payouts = await res.json();
        const tbody = document.getElementById('payswift-ledger-body');
        if (!payouts || payouts.length === 0) return;

        tbody.innerHTML = payouts.slice().reverse().map(p => `
          <tr class="hover:bg-zinc-900/60 transition">
            <td class="px-4 py-2.5 text-amber-400 font-bold">${p.payout_id || '—'}</td>
            <td class="px-4 py-2.5 text-white font-bold cursor-pointer hover:underline hover:text-amber-300" onclick="selectRider('${p.rider_id}')">${p.rider_id}</td>
            <td class="px-4 py-2.5 font-bold text-amber-300">₹${p.amount}</td>
            <td class="px-4 py-2.5 text-zinc-300 max-w-xs truncate font-sans">${p.reference || 'Dispute settlement'}</td>
            <td class="px-4 py-2.5">
              <span class="gold-badge px-2 py-0.5 rounded text-[10px] uppercase font-bold">
                ${p.status || 'processed'}
              </span>
            </td>
            <td class="px-4 py-2.5 text-right text-zinc-400 text-[11px]">${p.created_at || '—'}</td>
          </tr>
        `).join('');
      } catch (e) {
        console.error("Error loading payouts:", e);
      }
    }

    function refreshDashboard() {
      fetchPending();
      fetchRiders();
      fetchPayouts();
      selectRider(activeRider);
    }

    // Auto-refresh every 6 seconds
    refreshDashboard();
    setInterval(refreshDashboard, 6000);
  </script>
</body>
</html>
    """
    return HTMLResponse(content=html)
