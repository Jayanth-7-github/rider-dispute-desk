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
@app.get("/ops-desk", response_class=HTMLResponse)
@app.get("/dashboard", response_class=HTMLResponse)
@app.get("/", response_class=HTMLResponse)
def ops_dashboard():
    headers = {
        "Cache-Control": "no-cache, no-store, must-revalidate, max-age=0",
        "Pragma": "no-cache",
        "Expires": "0",
    }

    html = """<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8">
  <meta http-equiv="Cache-Control" content="no-cache, no-store, must-revalidate">
  <meta http-equiv="Pragma" content="no-cache">
  <meta http-equiv="Expires" content="0">
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
  <title>QuickDrop — Rider Dispute Desk</title>
  <link rel="preconnect" href="https://fonts.googleapis.com">
  <link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
  <link href="https://fonts.googleapis.com/css2?family=Plus+Jakarta+Sans:wght@300;400;500;600;700;800;900&family=JetBrains+Mono:wght@400;500;600;700&display=swap" rel="stylesheet">
  <script src="https://cdn.tailwindcss.com"></script>

  <style>
    /* DBS BANK BRAND REBRANDING PALETTE: STRONG RED (#C50022), PURE BLACK (#000000), WHITE (#FFFFFF) */
    :root {
      --strong-red: #C50022;
      --red-hover: #E00028;
      --red-glow: rgba(197, 0, 34, 0.4);
      --red-subtle: rgba(197, 0, 34, 0.12);
      --pure-black: #000000;
      --card-black: #080808;
      --card-border: #1a1a1a;
      --white: #FFFFFF;
      --cream-white: #B5AC8A;
      --muted-white: #888888;
    }

    * {
      box-sizing: border-box;
    }

    body {
      background-color: #000000 !important;
      color: #FFFFFF !important;
      font-family: 'Plus Jakarta Sans', sans-serif !important;
    }

    .brand-red {
      color: #C50022 !important;
    }

    .bg-brand-red {
      background-color: #C50022 !important;
    }

    .dbs-card {
      background: #080808 !important;
      border: 1px solid #1a1a1a !important;
      box-shadow: 0 4px 20px rgba(0, 0, 0, 0.9) !important;
      border-radius: 12px !important;
      transition: border-color 0.2s ease, box-shadow 0.2s ease !important;
    }

    .dbs-card:hover {
      border-color: #C50022 !important;
      box-shadow: 0 6px 28px rgba(197, 0, 34, 0.18) !important;
    }

    .red-btn {
      background: #C50022 !important;
      color: #FFFFFF !important;
      font-weight: 800 !important;
      border: 1px solid #C50022 !important;
      box-shadow: 0 2px 12px rgba(197, 0, 34, 0.4) !important;
      transition: all 0.2s ease !important;
    }
    .red-btn:hover {
      background: #E00028 !important;
      border-color: #E00028 !important;
      box-shadow: 0 4px 20px rgba(197, 0, 34, 0.6) !important;
      transform: translateY(-1px) !important;
    }

    .red-btn-outline {
      background: #000000 !important;
      border: 1px solid #2a2a2a !important;
      color: #FFFFFF !important;
      transition: all 0.2s ease !important;
    }
    .red-btn-outline:hover {
      background: rgba(197, 0, 34, 0.15) !important;
      border-color: #C50022 !important;
      color: #FFFFFF !important;
    }

    .red-badge {
      background: #C50022 !important;
      color: #FFFFFF !important;
      font-weight: 800 !important;
    }

    .red-badge-outline {
      background: rgba(197, 0, 34, 0.12) !important;
      border: 1px solid #C50022 !important;
      color: #FFFFFF !important;
    }

    .white-badge {
      background: #141414 !important;
      border: 1px solid #2e2e2e !important;
      color: #FFFFFF !important;
    }

    /* Custom Scrollbar */
    ::-webkit-scrollbar { width: 5px; height: 5px; }
    ::-webkit-scrollbar-track { background: #000000; }
    ::-webkit-scrollbar-thumb { background: #222222; border-radius: 3px; border: 1px solid #C50022; }
    ::-webkit-scrollbar-thumb:hover { background: #C50022; }

    @keyframes pulse-red-ring {
      0%, 100% { transform: scale(1); opacity: 1; box-shadow: 0 0 0 0 rgba(197, 0, 34, 0.8); }
      50% { transform: scale(1.15); opacity: 0.9; box-shadow: 0 0 12px 4px rgba(197, 0, 34, 0.7); }
    }
    .red-pulse { animation: pulse-red-ring 2s infinite ease-in-out; }
  </style>
</head>
<body class="min-h-screen font-sans selection:bg-red-700 selection:text-white antialiased">

  <!-- TOP BRAND REBRANDING HEADER BAR -->
  <header style="background: #000000; border-bottom: 2px solid #C50022;" class="sticky top-0 z-50">
    <div class="max-w-7xl mx-auto px-6 py-4 flex flex-col md:flex-row items-center justify-between gap-4">
      <div class="flex items-center gap-4">
        <!-- Logo Icon Box -->
        <div style="background: #C50022; width: 42px; height: 42px; border-radius: 8px;" class="flex items-center justify-center font-black text-white text-xl tracking-tighter shadow-lg shadow-red-950">
          QD
        </div>
        <div>
          <div class="flex items-center gap-3">
            <h1 class="text-xl font-black tracking-wider text-white uppercase font-mono">
              QUICKDROP <span class="brand-red">DISPUTE DESK</span>
            </h1>
            <span class="red-badge text-[10px] uppercase font-mono px-2.5 py-0.5 rounded tracking-wider">
              AUTONOMOUS ACTIVE
            </span>
          </div>
          <p class="text-xs text-stone-400 font-mono">End-to-End Payout Dispute Resolution · Bank Clearance Engine</p>
        </div>
      </div>

      <div class="flex items-center gap-3 text-xs">
        <div style="background: #0a0a0a; border: 1px solid #1f1f1f;" class="flex items-center gap-2 px-3.5 py-1.5 rounded-md">
          <span style="background: #C50022;" class="w-2.5 h-2.5 rounded-full red-pulse"></span>
          <span class="font-mono text-white font-bold">PaySwift: Connected</span>
        </div>
        <div style="background: #0a0a0a; border: 1px solid #1f1f1f;" class="flex items-center gap-2 px-3 py-1.5 rounded-md font-mono text-stone-400">
          <span>IST: UTC+05:30</span>
        </div>
        <button onclick="refreshDashboard()" class="red-btn px-4 py-1.5 rounded-md text-xs font-black cursor-pointer uppercase tracking-wider">
          ↻ REFRESH
        </button>
      </div>
    </div>
  </header>

  <!-- MAIN WRAPPER -->
  <main class="max-w-7xl mx-auto p-6 space-y-6">

    <!-- METRICS STRIP -->
    <div class="grid grid-cols-1 sm:grid-cols-2 lg:grid-cols-4 gap-4">
      <!-- Metric 1 -->
      <div class="dbs-card p-5 border-l-4 border-l-[#C50022]">
        <div class="flex justify-between items-start">
          <p class="text-[11px] font-mono font-bold uppercase tracking-wider text-stone-400">Pending Approvals</p>
          <span class="brand-red text-sm font-black">■</span>
        </div>
        <p id="stat-approvals" class="text-3xl font-black text-white mt-2">0</p>
        <p class="text-[11px] text-stone-400 mt-1">Requires human ops clearance (> ₹200 / repeat)</p>
      </div>

      <!-- Metric 2 -->
      <div class="dbs-card p-5 border-l-4 border-l-[#C50022]">
        <div class="flex justify-between items-start">
          <p class="text-[11px] font-mono font-bold uppercase tracking-wider text-stone-400">Ops Escalations</p>
          <span class="brand-red text-sm font-black">■</span>
        </div>
        <p id="stat-escalations" class="text-3xl font-black text-white mt-2">0</p>
        <p class="text-[11px] text-stone-400 mt-1">Policy exceptions, fraud & security flags</p>
      </div>

      <!-- Metric 3 -->
      <div class="dbs-card p-5 border-l-4 border-l-[#C50022]">
        <div class="flex justify-between items-start">
          <p class="text-[11px] font-mono font-bold uppercase tracking-wider text-stone-400">Auto-Pay Rule</p>
          <span class="brand-red text-sm font-black">₹</span>
        </div>
        <p class="text-3xl font-black brand-red mt-2">≤ ₹200</p>
        <p class="text-[11px] text-stone-400 mt-1">Max 1 auto-pay / rider / IST calendar day</p>
      </div>

      <!-- Metric 4 -->
      <div class="dbs-card p-5 border-l-4 border-l-[#C50022]">
        <div class="flex justify-between items-start">
          <p class="text-[11px] font-mono font-bold uppercase tracking-wider text-stone-400">Policy Window</p>
          <span class="brand-red text-sm font-black">■</span>
        </div>
        <p class="text-3xl font-black text-white mt-2">7 Days</p>
        <p class="text-[11px] text-stone-400 mt-1">Strict IST calendar boundary</p>
      </div>
    </div>

    <!-- MAIN TWO-COLUMN SPLIT PANE -->
    <div class="grid grid-cols-1 lg:grid-cols-12 gap-6">

      <!-- LEFT COLUMN: OPS PENDING QUEUE (7 Cols) -->
      <div class="lg:col-span-7 space-y-6">
        <section class="dbs-card overflow-hidden">
          <div style="background: #0c0c0c; border-bottom: 2px solid #C50022;" class="p-5 flex items-center justify-between">
            <div>
              <h2 class="text-sm font-black text-white tracking-widest uppercase font-mono flex items-center gap-2">
                <span class="brand-red font-black">///</span> Operations Action Queue
              </h2>
              <p class="text-xs text-stone-400">Items requiring human sign-off before banking payout</p>
            </div>
            <span id="pending-count-badge" class="red-badge px-3 py-1 rounded text-[11px] font-mono font-bold uppercase tracking-wider">
              0 Items
            </span>
          </div>

          <!-- Table Container -->
          <div class="overflow-x-auto">
            <table class="w-full text-left text-xs">
              <thead style="background: #050505; border-bottom: 1px solid #1c1c1c;" class="font-mono text-[10px] uppercase tracking-wider text-stone-400">
                <tr>
                  <th class="px-4 py-3.5">Item</th>
                  <th class="px-4 py-3.5">Rider</th>
                  <th class="px-4 py-3.5">Type</th>
                  <th class="px-4 py-3.5">Amount</th>
                  <th class="px-4 py-3.5">Reason</th>
                  <th class="px-4 py-3.5 text-right">Clearance</th>
                </tr>
              </thead>
              <tbody id="pending-table-body" class="divide-y divide-[#141414] text-white font-mono">
                <tr>
                  <td colspan="6" class="px-4 py-10 text-center text-stone-500 font-sans">
                    No pending items. The queue is completely clear.
                  </td>
                </tr>
              </tbody>
            </table>
          </div>
        </section>

        <!-- ACTIVE RIDERS DIRECTORY -->
        <section class="dbs-card p-5">
          <div class="flex items-center justify-between mb-3.5">
            <h3 class="text-xs font-black uppercase tracking-widest text-white font-mono flex items-center gap-2">
              <span class="brand-red">///</span> Recent Rider Directory
            </h3>
            <span class="text-[11px] text-stone-400 font-mono">Select to inspect dossier</span>
          </div>
          <div id="riders-list" class="flex flex-wrap gap-2">
            <span class="text-xs text-stone-500">Loading riders...</span>
          </div>
        </section>
      </div>

      <!-- RIGHT COLUMN: RIDER AUDIT & CHAT EXPLORER (5 Cols) -->
      <div class="lg:col-span-5 space-y-6">
        <section class="dbs-card p-5 flex flex-col h-full min-h-[580px]">
          
          <!-- Dossier Top Bar -->
          <div style="border-bottom: 1px solid #1c1c1c;" class="flex items-center justify-between pb-4">
            <div class="flex items-center gap-3">
              <div style="background: #C50022;" class="w-10 h-10 rounded-md flex items-center justify-center font-black text-white font-mono text-sm shadow-md">
                <span id="dossier-avatar">R03</span>
              </div>
              <div>
                <h3 id="dossier-rider-id" class="font-black text-white text-sm tracking-wide font-mono">R003 — Imran Khan</h3>
                <p id="dossier-meta" class="text-xs text-stone-400 font-mono">Bengaluru · Joined 2024-03-15</p>
              </div>
            </div>
            <div style="background: #000000; border: 1px solid #222222;" class="flex gap-1 p-1 rounded-md font-mono text-[11px]">
              <button onclick="switchTab('trace')" id="tab-btn-trace" class="px-3.5 py-1 rounded font-black red-btn cursor-pointer">
                TRACE
              </button>
              <button onclick="switchTab('chat')" id="tab-btn-chat" class="px-3.5 py-1 rounded font-bold text-stone-400 hover:text-white cursor-pointer">
                CHAT
              </button>
            </div>
          </div>

          <!-- Trace View Tab -->
          <div id="tab-content-trace" class="flex-1 mt-4 overflow-y-auto space-y-3 max-h-[520px] pr-1">
            <div class="text-center py-12 text-stone-500 font-mono text-xs">
              Select any rider or pending item on the left to review their step-by-step autonomous trace.
            </div>
          </div>

          <!-- Chat View Tab (Hidden by default) -->
          <div id="tab-content-chat" class="hidden flex-1 mt-4 overflow-y-auto space-y-3 max-h-[520px] pr-1 font-sans">
            <div class="text-center py-12 text-stone-500 font-mono text-xs">
              No messages selected.
            </div>
          </div>

        </section>
      </div>

    </div>

    <!-- BOTTOM SECTION: PAYSWIFT LIVE LEDGER MONITOR -->
    <section class="dbs-card p-5">
      <div class="flex items-center justify-between mb-4">
        <div class="flex items-center gap-2.5">
          <span style="background: #C50022;" class="w-2.5 h-2.5 rounded-full red-pulse"></span>
          <h3 class="text-xs font-black uppercase tracking-widest text-white font-mono flex items-center gap-2">
            <span class="brand-red">///</span> PaySwift Live Banking Ledger
          </h3>
        </div>
        <p class="text-[11px] text-stone-400 font-mono">Reconciled against bank API in real time</p>
      </div>

      <div class="overflow-x-auto">
        <table class="w-full text-left text-xs font-mono">
          <thead style="background: #050505; border-bottom: 1px solid #1c1c1c;" class="uppercase text-[10px] tracking-wider text-stone-400">
            <tr>
              <th class="px-4 py-2.5">Payout ID</th>
              <th class="px-4 py-2.5">Rider</th>
              <th class="px-4 py-2.5">Amount</th>
              <th class="px-4 py-2.5">Reference</th>
              <th class="px-4 py-2.5">Status</th>
              <th class="px-4 py-2.5 text-right">Timestamp</th>
            </tr>
          </thead>
          <tbody id="payswift-ledger-body" class="divide-y divide-[#141414] text-white">
            <tr>
              <td colspan="6" class="px-4 py-6 text-center text-stone-500 font-sans">No recent transactions recorded yet.</td>
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
        traceBtn.className = 'px-3.5 py-1 rounded font-black red-btn cursor-pointer';
        chatBtn.className = 'px-3.5 py-1 rounded font-bold text-stone-400 hover:text-white cursor-pointer';
        traceContent.classList.remove('hidden');
        chatContent.classList.add('hidden');
      } else {
        chatBtn.className = 'px-3.5 py-1 rounded font-black red-btn cursor-pointer';
        traceBtn.className = 'px-3.5 py-1 rounded font-bold text-stone-400 hover:text-white cursor-pointer';
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
        badge.innerText = `${items.length} PENDING`;

        if (!items || items.length === 0) {
          tbody.innerHTML = '<tr><td colspan="6" class="px-4 py-8 text-center text-stone-500 font-sans">All pending items are cleared. Operations desk is running smoothly.</td></tr>';
          return;
        }

        tbody.innerHTML = items.map(item => `
          <tr class="hover:bg-[#111111] transition">
            <td class="px-4 py-3 font-bold brand-red font-mono">${item.id}</td>
            <td class="px-4 py-3 text-white font-bold cursor-pointer hover:underline hover:text-[#C50022]" onclick="selectRider('${item.rider_id}')">
              ${item.rider_id}
            </td>
            <td class="px-4 py-3">
              <span class="px-2 py-0.5 rounded text-[10px] tracking-wider font-mono font-bold ${
                item.type === 'approval' 
                  ? 'red-badge' 
                  : 'white-badge'
              }">
                ${item.type.toUpperCase()}
              </span>
            </td>
            <td class="px-4 py-3 font-bold brand-red font-mono">${item.amount ? '₹' + item.amount : '—'}</td>
            <td class="px-4 py-3 text-stone-300 max-w-xs truncate font-sans" title="${item.reason}">${item.reason}</td>
            <td class="px-4 py-3 text-right space-x-2 font-sans">
              <button onclick="actionItem('${item.id}', 'approve')" class="red-btn px-3 py-1 rounded text-[11px] cursor-pointer">
                Approve
              </button>
              <button onclick="actionItem('${item.id}', 'reject')" class="red-btn-outline px-3 py-1 rounded text-[11px] cursor-pointer">
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
          <button onclick="selectRider('${r.rider_id}')" class="px-3 py-1 rounded text-xs font-mono font-bold transition cursor-pointer ${
            activeRider === r.rider_id 
              ? 'red-btn' 
              : 'red-btn-outline'
          }">
            ${r.rider_id} ${r.message_count > 0 ? `<span class="opacity-80 text-[10px]">(${r.message_count})</span>` : ''}
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
      container.innerHTML = '<p class="text-stone-500 text-center py-8 font-mono text-xs">Loading audit trace...</p>';
      try {
        const res = await fetch(`/trace/${riderId}`);
        const trace = await res.json();
        if (!trace || trace.length === 0) {
          container.innerHTML = `<p class="text-stone-500 text-center py-10 font-mono text-xs">No audit trace recorded yet for ${riderId}.</p>`;
          return;
        }

        container.innerHTML = trace.map(step => {
          let badgeClass = 'white-badge';
          if (step.type === 'message_in') badgeClass = 'white-badge';
          if (step.type === 'tool_call') badgeClass = 'red-badge';
          if (step.type === 'decision') badgeClass = 'red-badge-outline';
          if (step.type === 'reply') badgeClass = 'red-badge';

          return `
            <div style="background: #090909; border: 1px solid #1a1a1a;" class="p-3.5 rounded-lg space-y-1.5 text-xs font-mono">
              <div class="flex items-center justify-between">
                <div class="flex items-center gap-2">
                  <span class="px-2 py-0.5 rounded text-[10px] uppercase font-bold ${badgeClass}">
                    ${step.type}
                  </span>
                  <span class="font-bold text-white">${step.name}</span>
                </div>
                <span class="text-[10px] text-stone-500">${new Date(step.at).toLocaleTimeString()}</span>
              </div>
              ${step.input ? `
                <div style="background: #000000; border: 1px solid #1f1f1f;" class="mt-1.5 p-2 rounded text-[11px] text-stone-300">
                  <span class="brand-red font-bold block text-[10px] uppercase font-mono">Input</span>
                  <pre class="overflow-x-auto whitespace-pre-wrap">${JSON.stringify(step.input, null, 2)}</pre>
                </div>
              ` : ''}
              ${step.output ? `
                <div style="background: #000000; border: 1px solid #1f1f1f;" class="mt-1.5 p-2 rounded text-[11px] text-white">
                  <span class="brand-red font-bold block text-[10px] uppercase font-mono">Output</span>
                  <pre class="overflow-x-auto whitespace-pre-wrap">${typeof step.output === 'object' ? JSON.stringify(step.output, null, 2) : step.output}</pre>
                </div>
              ` : ''}
            </div>
          `;
        }).join('');
      } catch (e) {
        container.innerHTML = `<p class="text-red-400 text-center py-6 font-mono text-xs">Error: ${e.message}</p>`;
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
          container.innerHTML = `<p class="text-stone-500 text-center py-10 font-mono text-xs">No WhatsApp messages from ${riderId}.</p>`;
          return;
        }

        container.innerHTML = messages.map(m => `
          <div class="space-y-2">
            <!-- Rider message -->
            <div class="flex justify-start">
              <div style="background: #0f0f0f; border: 1px solid #222222;" class="max-w-[85%] rounded-lg px-4 py-2.5 text-xs text-white">
                <p class="font-bold text-[10px] text-stone-400 mb-0.5 font-mono">Rider (${m.rider_id})</p>
                <p>${m.text}</p>
                <span class="text-[9px] text-stone-500 block text-right mt-1 font-mono">${new Date(m.received_at).toLocaleTimeString()}</span>
              </div>
            </div>
            <!-- Agent reply -->
            ${m.reply ? `
              <div class="flex justify-end">
                <div style="background: rgba(197, 0, 34, 0.12); border: 1px solid #C50022;" class="max-w-[85%] rounded-lg px-4 py-2.5 text-xs text-white">
                  <p class="font-bold text-[10px] brand-red mb-0.5 font-mono">QuickDrop Agent ///</p>
                  <p>${m.reply}</p>
                  <span class="text-[9px] text-stone-400 block text-right mt-1 font-mono">${new Date(m.created_at).toLocaleTimeString()}</span>
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
          <tr class="hover:bg-[#111111] transition">
            <td class="px-4 py-2.5 brand-red font-bold font-mono">${p.payout_id || '—'}</td>
            <td class="px-4 py-2.5 text-white font-bold cursor-pointer hover:underline hover:text-[#C50022]" onclick="selectRider('${p.rider_id}')">${p.rider_id}</td>
            <td class="px-4 py-2.5 font-black text-white font-mono">₹${p.amount}</td>
            <td class="px-4 py-2.5 text-stone-300 max-w-xs truncate font-sans">${p.reference || 'Dispute settlement'}</td>
            <td class="px-4 py-2.5">
              <span class="red-badge px-2 py-0.5 rounded text-[10px] uppercase font-bold font-mono">
                ${p.status || 'processed'}
              </span>
            </td>
            <td class="px-4 py-2.5 text-right text-stone-400 text-[11px] font-mono">${p.created_at || '—'}</td>
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
    return HTMLResponse(content=html, headers=headers)
