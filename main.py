import os
import time
import random
import threading
from datetime import time as dt_time
from dotenv import load_dotenv
from flask import Flask, redirect
import db
import dhan_bridge
from structure_engine import analyze_market_structure, ist_now
from telegram_alert import send_telegram
from ai_analyst import generate_premarket_brief

load_dotenv()
app = Flask(__name__)

runtime_status = {
    "spot": 24450.0,
    "last_msg": "Initializing...",
    "pcr": 1.0,
    "is_expiry": False
}
sim_candles = []
_engine_started = False
_engine_lock = threading.Lock()

def generate_sim_candle():
    open_p = runtime_status["spot"]
    move = random.uniform(-35, 38)
    close_p = round(open_p + move, 2)
    high_p = round(max(open_p, close_p) + random.uniform(2, 18), 2)
    low_p = round(min(open_p, close_p) - random.uniform(2, 18), 2)
    runtime_status["spot"] = close_p
    return {"open": open_p, "high": high_p, "low": low_p, "close": close_p, "volume": random.randint(800, 4000)}

def is_indian_market_open():
    now = ist_now()
    if now.weekday() >= 5:
        return False
    return dt_time(9, 15) <= now.time() <= dt_time(15, 30)

def anas_trading_loop():
    global sim_candles
    for _ in range(15):
        sim_candles.append(generate_sim_candle())

    cached_pdh_pdl = {}
    cached_expiry = {}

    while True:
        try:
            state = db.get_system_state()
            today_pnl = db.get_today_pnl()
            max_loss_amt = -(state["active_capital"] * (state["max_loss_pct"] / 100.0))

            if not state["kill_switch"] and today_pnl <= max_loss_amt:
                db.update_setting("kill_switch", True)
                send_telegram(f"🚨 <b>KILL SWITCH TRIGGERED!</b>\nDaily Max Loss ({today_pnl}) hit.")

            if state["kill_switch"]:
                runtime_status["last_msg"] = "KILL SWITCH ACTIVE"
                time.sleep(5)
                continue

            mode = state["mode"]
            sym = state["active_symbol"]
            open_trades = db.get_open_trades()

            # --- MODE 1: PAPER SIMULATION (24x7 Testing) ---
            if mode == "PAPER_SIM":
                new_c = generate_sim_candle()
                sim_candles.append(new_c)
                if len(sim_candles) > 50:
                    sim_candles.pop(0)

                live_prem = max(round(120.0 + (new_c["close"] - 24450.0) * 0.5, 2), 10.0)

                for t in open_trades:
                    tid, tsym, inst, side, reason, entry, sl, target, trail_sl, qty, is_exp = t
                    entry, target, trail_sl = float(entry), float(target), float(trail_sl)
                    curr_opt = live_prem if side == "BUY_CE" else round(240.0 - live_prem, 2)
                    risk = abs(entry - float(sl))

                    if curr_opt >= entry + risk and trail_sl < entry:
                        db.update_trailing_sl(tid, entry)
                        send_telegram(f"🛡️ <b>SL TRAILED TO COST</b>\n{tsym} | New SL: ₹{entry}")

                    if curr_opt <= trail_sl:
                        net = db.close_virtual_trade(tid, curr_opt, (curr_opt - entry) * qty)
                        send_telegram(f"🔴 <b>SL HIT EXIT</b>\n{tsym} @ ₹{curr_opt}\nNet P&L: ₹{net}")
                    elif curr_opt >= target:
                        net = db.close_virtual_trade(tid, curr_opt, (curr_opt - entry) * qty)
                        send_telegram(f"💰 <b>TARGET ACHIEVED!</b>\n{tsym} @ ₹{curr_opt}\nNet P&L: ₹{net}")

                if len(open_trades) == 0:
                    pdh = max(c["high"] for c in sim_candles[:-1]) - 5
                    pdl = min(c["low"] for c in sim_candles[:-1]) + 5
                    res = analyze_market_structure(sim_candles, pdh=pdh, pdl=pdl, is_expiry=False)
                    runtime_status["last_msg"] = res["reason"]

                    if res["signal"] in ("BUY_CE", "BUY_PE"):
                        side = res["signal"]
                        opt_entry = live_prem if side == "BUY_CE" else round(240.0 - live_prem, 2)
                        opt_sl = round(opt_entry * 0.85, 2)
                        opt_target = round(opt_entry * 1.30, 2)
                        strike = int(round(new_c["close"] / 50.0) * 50)
                        opt_sym = f"{sym} {strike} {'CE' if side == 'BUY_CE' else 'PE'}"
                        db.open_virtual_trade(opt_sym, sym, side, res["reason"], opt_entry, opt_sl, opt_target, 75, False)
                        send_telegram(f"🟢 <b>ANAS AI SIGNAL (SIM)</b>\n<b>{opt_sym}</b>\nEntry: ₹{opt_entry} | SL: ₹{opt_sl} | Target: ₹{opt_target}\nSetup: {res['reason']}")

            # --- MODE 2: LIVE DHAN DATA + PAPER EXECUTION ---
            elif mode == "PAPER_DHAN":
                if not dhan_bridge.is_dhan_configured():
                    runtime_status["last_msg"] = "DHAN_CLIENT_ID / ACCESS_TOKEN .env me missing hai"
                    time.sleep(5)
                    continue

                if not is_indian_market_open():
                    runtime_status["last_msg"] = "Market Closed (09:15 - 15:30 IST Mon-Fri)"
                    time.sleep(10)
                    continue

                today_str = ist_now().strftime("%Y-%m-%d")
                if sym not in cached_pdh_pdl or cached_pdh_pdl[sym]["date"] != today_str:
                    pdh, pdl = dhan_bridge.get_pdh_pdl(sym)
                    cached_pdh_pdl[sym] = {"date": today_str, "pdh": pdh, "pdl": pdl}
                if sym not in cached_expiry or cached_expiry[sym]["date"] != today_str:
                    exp = dhan_bridge.get_nearest_expiry(sym)
                    cached_expiry[sym] = {"date": today_str, "expiry": exp}

                pdh = cached_pdh_pdl[sym]["pdh"]
                pdl = cached_pdh_pdl[sym]["pdl"]
                expiry = cached_expiry[sym]["expiry"]
                is_exp = (expiry == today_str)
                runtime_status["is_expiry"] = is_exp

                candles = dhan_bridge.get_intraday_candles(sym, interval="5")
                oc_data = dhan_bridge.get_option_chain_snapshot(sym, expiry)
                runtime_status["spot"] = float(oc_data.get("last_price") or candles[-1]["close"])

                # Manage Open Virtual Trades with REAL Option Chain LTP
                for t in open_trades:
                    tid, tsym, inst, side, reason, entry, sl, target, trail_sl, qty, _ = t
                    entry, target, trail_sl = float(entry), float(target), float(trail_sl)
                    parts = tsym.split()
                    t_strike = int(parts[1])
                    leg_key = "ce" if parts[2] == "CE" else "pe"

                    curr_opt = None
                    for k, v in oc_data["oc"].items():
                        if int(round(float(k))) == t_strike:
                            curr_opt = float(v.get(leg_key, {}).get("last_price") or 0)
                            break

                    if not curr_opt or curr_opt <= 0:
                        continue

                    risk = abs(entry - float(sl))
                    if curr_opt >= entry + risk and trail_sl < entry:
                        db.update_trailing_sl(tid, entry)
                        send_telegram(f"🛡️ <b>SL TRAILED TO COST</b>\n{tsym} | New SL: ₹{entry}")

                    if curr_opt <= trail_sl:
                        net = db.close_virtual_trade(tid, curr_opt, (curr_opt - entry) * qty)
                        send_telegram(f"🔴 <b>SL HIT EXIT (DHAN)</b>\n{tsym} @ ₹{curr_opt}\nNet P&L: ₹{net}")
                    elif curr_opt >= target:
                        net = db.close_virtual_trade(tid, curr_opt, (curr_opt - entry) * qty)
                        send_telegram(f"💰 <b>TARGET HIT (DHAN)</b>\n{tsym} @ ₹{curr_opt}\nNet P&L: ₹{net}")

                if len(open_trades) == 0:
                    res = analyze_market_structure(candles, pdh=pdh, pdl=pdl, is_expiry=is_exp)
                    runtime_status["last_msg"] = res["reason"]

                    if res["signal"] in ("BUY_CE", "BUY_PE"):
                        side = res["signal"]
                        strike, opt_entry, pcr, oi_ok, lot = dhan_bridge.pick_pro_strike_and_pcr(sym, oc_data, side, is_exp)
                        runtime_status["pcr"] = pcr
                        if not oi_ok:
                            runtime_status["last_msg"] = f"Skipped by OI Filter (PCR={pcr})"
                        elif opt_entry > 5:
                            opt_sl = round(opt_entry * 0.85, 2)
                            opt_target = round(opt_entry * 1.30, 2)
                            opt_sym = f"{sym} {strike} {'CE' if side == 'BUY_CE' else 'PE'}"
                            db.open_virtual_trade(opt_sym, sym, side, f"{res['reason']} | PCR:{pcr}", opt_entry, opt_sl, opt_target, lot, is_exp)
                            send_telegram(
                                f"🟢 <b>ANAS AI LIVE SIGNAL</b>\n<b>{opt_sym}</b> {'⚠️ EXPIRY' if is_exp else ''}\n"
                                f"Entry: ₹{opt_entry} | SL: ₹{opt_sl} | Target: ₹{opt_target}\n"
                                f"PCR: {pcr} | Setup: {res['reason']}"
                            )
                time.sleep(5)
                continue

        except Exception as e:
            runtime_status["last_msg"] = f"Error: {str(e)[:80]}"
            print("Loop Error:", e)
        time.sleep(5)

def start_engine_once():
    global _engine_started
    with _engine_lock:
        if not _engine_started:
            db.init_db()
            t = threading.Thread(target=anas_trading_loop, daemon=True)
            t.start()
            _engine_started = True

start_engine_once()

@app.route("/")
def dashboard():
    state = db.get_system_state()
    today_pnl = db.get_today_pnl()
    trades = db.get_recent_trades(12)

    rows_html = ""
    for t in trades:
        color = "#22c55e" if float(t[8] or 0) >= 0 else "#ef4444"
        rows_html += f"""
        <tr>
          <td>{t[1]}</td><td>{t[2]}</td><td>{t[3]}</td>
          <td>₹{t[4]}</td><td>₹{t[5]}</td><td>₹{t[6]}</td>
          <td>{t[7] or '-'}</td>
          <td style="color:{color};font-weight:bold;">₹{t[8]}</td>
          <td>{t[9]}</td>
        </tr>"""

    kill_badge = "🔴 LOCKED (KILL SWITCH)" if state["kill_switch"] else "🟢 SCANNING"
    exp_badge = "⚠️ EXPIRY GUARD ON" if runtime_status["is_expiry"] else "Normal Day"
    return f"""
    <html>
    <head>
      <title>ANAS AI - Master Blaster</title>
      <meta name="viewport" content="width=device-width, initial-scale=1">
      <meta http-equiv="refresh" content="5">
      <style>
        body {{ background:#0b0f19; color:#e2e8f0; font-family:sans-serif; padding:16px; }}
        .grid {{ display:grid; grid-template-columns:repeat(auto-fit,minmax(145px,1fr)); gap:10px; margin-bottom:16px; }}
        .card {{ background:#161e2e; padding:14px; border-radius:10px; border:1px solid #263147; }}
        .val {{ font-size:20px; font-weight:bold; margin-top:6px; }}
        .btn {{ padding:8px 12px; border-radius:8px; color:white; font-weight:bold; text-decoration:none; display:inline-block; margin:4px 5px 4px 0; font-size:12px; }}
        table {{ width:100%; border-collapse:collapse; background:#161e2e; border-radius:10px; font-size:12px; }}
        th, td {{ padding:9px; border-bottom:1px solid #263147; text-align:left; }}
        th {{ background:#1e293b; }}
      </style>
    </head>
    <body>
      <h2>⚡ ANAS AI — Master Blaster v2</h2>
      <p style="font-size:13px;">
        Symbol: <b>{state['active_symbol']}</b> ({runtime_status['spot']}) |
        Mode: <b>{state['mode']}</b> | <b>{kill_badge}</b> | {exp_badge}
      </p>
      <p style="font-size:12px;color:#94a3b8;">Brain Status: {runtime_status['last_msg']}</p>

      <div class="grid">
        <div class="card">Active Capital<div class="val">₹{state['active_capital']:,.2f}</div></div>
        <div class="card">Safe Vault (40%)<div class="val" style="color:#38bdf8;">₹{state['safe_vault']:,.2f}</div></div>
        <div class="card">Today's Net P&L<div class="val" style="color:{'#22c55e' if today_pnl>=0 else '#ef4444'};">₹{today_pnl:,.2f}</div></div>
        <div class="card">Max Loss Limit<div class="val">-{state['max_loss_pct']}%</div></div>
      </div>

      <div style="margin-bottom:12px;">
        <a href="/kill/on" class="btn" style="background:#dc2626;">🚨 KILL SWITCH</a>
        <a href="/kill/off" class="btn" style="background:#16a34a;">🔓 UNLOCK</a>
        <a href="/ai-brief" class="btn" style="background:#6366f1;">🧠 GEMINI BRIEF</a>
      </div>

      <div style="margin-bottom:16px;">
        <b>Switch Mode:</b>
        <a href="/mode/PAPER_SIM" class="btn" style="background:#475569;">🧪 SIM MODE</a>
        <a href="/mode/PAPER_DHAN" class="btn" style="background:#0284c7;">📡 LIVE DHAN PAPER</a>
        <br><b>Index:</b>
        <a href="/sym/NIFTY" class="btn" style="background:#334155;">NIFTY</a>
        <a href="/sym/BANKNIFTY" class="btn" style="background:#334155;">BANKNIFTY</a>
        <a href="/sym/SENSEX" class="btn" style="background:#334155;">SENSEX</a>
      </div>

      <h3>Recent Signals & Trades</h3>
      <div style="overflow-x:auto;">
        <table>
          <tr><th>Symbol</th><th>Side</th><th>Setup</th><th>Entry</th><th>Trail SL</th><th>Target</th><th>Exit</th><th>Net P&L</th><th>Status</th></tr>
          {rows_html}
        </table>
      </div>
    </body>
    </html>
    """

@app.route("/kill/on")
def kill_on():
    db.update_setting("kill_switch", True)
    send_telegram("🚨 <b>MANUAL KILL SWITCH ACTIVATED!</b>")
    return redirect("/")

@app.route("/kill/off")
def kill_off():
    db.update_setting("kill_switch", False)
    return redirect("/")

@app.route("/mode/<m>")
def set_mode(m):
    if m in ("PAPER_SIM", "PAPER_DHAN"):
        db.update_setting("trading_mode", m)
    return redirect("/")

@app.route("/sym/<s>")
def set_sym(s):
    if s in ("NIFTY", "BANKNIFTY", "SENSEX"):
        db.update_setting("active_symbol", s)
    return redirect("/")

@app.route("/ai-brief")
def ai_brief():
    brief = generate_premarket_brief()
    return f"<body style='background:#0b0f19;color:#fff;font-family:sans-serif;padding:20px;'><h2>🧠 Gemini Pre-Market Brief</h2><pre style='white-space:pre-wrap;font-size:15px;'>{brief}</pre><br><a href='/' style='color:#38bdf8;'>← Back</a></body>"

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.getenv("PORT", 8000)), use_reloader=False)
