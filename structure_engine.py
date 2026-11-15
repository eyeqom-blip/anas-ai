from datetime import datetime, timedelta, time as dt_time

def ist_now():
    return datetime.utcnow() + timedelta(hours=5, minutes=30)

def calculate_vwap(candles):
    cum_pv = 0.0
    cum_vol = 0.0
    for c in candles:
        typical = (c["high"] + c["low"] + c["close"]) / 3.0
        vol = max(c.get("volume", 1), 1)
        cum_pv += typical * vol
        cum_vol += vol
    return round(cum_pv / cum_vol, 2) if cum_vol else candles[-1]["close"]

def is_expiry_guarded(is_expiry: bool, now_time: dt_time):
    return bool(is_expiry and now_time >= dt_time(14, 15))

def analyze_market_structure(candles, pdh: float, pdl: float, is_expiry: bool = False):
    if len(candles) < 12:
        return {"signal": "WAIT", "reason": "Insufficient Candles"}

    if is_expiry_guarded(is_expiry, ist_now().time()):
        return {"signal": "BLOCKED", "reason": "Expiry 2:15 PM Gamma Guard Active"}

    last = candles[-1]
    prev_swing_high = max(c["high"] for c in candles[-10:-1])
    prev_swing_low = min(c["low"] for c in candles[-10:-1])
    vwap = calculate_vwap(candles[-20:])

    avg_range = sum(c["high"] - c["low"] for c in candles[-10:]) / 10.0
    if avg_range < (last["close"] * 0.0003):
        return {"signal": "CHOPPY", "reason": "Low Volatility Choppy Market - Stay Out"}

    if last["low"] < pdl and last["close"] > pdl and last["close"] > last["open"]:
        return {"signal": "BUY_CE", "reason": "PDL Liquidity Sweep (Bear Trap) + Reclaim", "vwap": vwap}

    if last["high"] > pdh and last["close"] < pdh and last["close"] < last["open"]:
        return {"signal": "BUY_PE", "reason": "PDH Liquidity Sweep (Bull Trap) + Rejection", "vwap": vwap}

    if last["close"] > prev_swing_high and last["close"] > vwap and last["open"] <= vwap * 1.002:
        return {"signal": "BUY_CE", "reason": "Bullish BOS + VWAP Confluence", "vwap": vwap}

    if last["close"] < prev_swing_low and last["close"] < vwap and last["open"] >= vwap * 0.998:
        return {"signal": "BUY_PE", "reason": "Bearish BOS + VWAP Confluence", "vwap": vwap}

    return {"signal": "WAIT", "reason": f"No Clean Structure | VWAP: {vwap}"}
