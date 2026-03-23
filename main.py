"""
APEX BOT — ELITE FINAL DEBUG VERSION
Pairs  : BTC, ETH, SOL, XRP, BNB
Tiers  : 🎯 5m Sniper | 📈 15m Intraday | 📊 1H Swing | 🏹 4H Swing
Logic  : Liquidity Sweep + Judas + MSS + HTF Bias (EMA20/EMA50)
Levels : Entry Zone + SL + TP1 + TP2
Storage: SQLite — processed candle dedup + alert dedup
Debug  : Flask live route + startup ping + heartbeat logs
"""

import os
import time
import sqlite3
import requests
import schedule
from datetime import datetime, timezone
from flask import Flask
from threading import Thread

# ─────────────────────────────────────────────
# ENV
# ─────────────────────────────────────────────

TELEGRAM_TOKEN = os.environ.get("TELEGRAM_TOKEN", "")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "")

OKX = "https://www.okx.com/api/v5"
DB = "apex_signals.db"

PAIRS = [
    {"symbol": "BTC", "inst": "BTC-USDT-SWAP", "ccy": "BTC"},
    {"symbol": "ETH", "inst": "ETH-USDT-SWAP", "ccy": "ETH"},
    {"symbol": "SOL", "inst": "SOL-USDT-SWAP", "ccy": "SOL"},
    {"symbol": "XRP", "inst": "XRP-USDT-SWAP", "ccy": "XRP"},
    {"symbol": "BNB", "inst": "BNB-USDT-SWAP", "ccy": "BNB"},
]

STATUS = {
    "bot_started": False,
    "last_heartbeat": "not yet",
    "last_scan": "not yet",
    "last_error": None
}

# ─────────────────────────────────────────────
# FLASK KEEP-ALIVE
# ─────────────────────────────────────────────

app = Flask(__name__)

@app.route("/")
def home():
    return {
        "status": "alive",
        "bot_started": STATUS["bot_started"],
        "last_heartbeat": STATUS["last_heartbeat"],
        "last_scan": STATUS["last_scan"],
        "last_error": STATUS["last_error"]
    }

def run_web():
    app.run(host="0.0.0.0", port=8080)

def keep_alive():
    t = Thread(target=run_web, daemon=True)
    t.start()

# ─────────────────────────────────────────────
# SQLITE
# ─────────────────────────────────────────────

def init_db():
    conn = sqlite3.connect(DB)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS sent_alerts (
            id          TEXT PRIMARY KEY,
            symbol      TEXT,
            timeframe   TEXT,
            signal      TEXT,
            score       INTEGER,
            created_at  TEXT
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS processed_candles (
            symbol        TEXT,
            timeframe     TEXT,
            candle_ts     TEXT,
            processed_at  TEXT,
            PRIMARY KEY (symbol, timeframe, candle_ts)
        )
    """)
    conn.commit()
    conn.close()
    print("[✓] DB ready.")

def utc_now_iso():
    return datetime.now(timezone.utc).isoformat()

def alert_exists(alert_id):
    conn = sqlite3.connect(DB)
    row = conn.execute("SELECT 1 FROM sent_alerts WHERE id=?", (alert_id,)).fetchone()
    conn.close()
    return row is not None

def save_alert(alert_id, symbol, timeframe, signal, score):
    conn = sqlite3.connect(DB)
    conn.execute(
        "INSERT OR IGNORE INTO sent_alerts VALUES (?,?,?,?,?,?)",
        (alert_id, symbol, timeframe, signal, score, utc_now_iso())
    )
    conn.commit()
    conn.close()

def candle_already_processed(symbol, timeframe, candle_ts):
    conn = sqlite3.connect(DB)
    row = conn.execute("""
        SELECT 1 FROM processed_candles
        WHERE symbol=? AND timeframe=? AND candle_ts=?
    """, (symbol, timeframe, candle_ts)).fetchone()
    conn.close()
    return row is not None

def mark_candle_processed(symbol, timeframe, candle_ts):
    conn = sqlite3.connect(DB)
    conn.execute("""
        INSERT OR IGNORE INTO processed_candles
        (symbol, timeframe, candle_ts, processed_at)
        VALUES (?, ?, ?, ?)
    """, (symbol, timeframe, candle_ts, utc_now_iso()))
    conn.commit()
    conn.close()

def clear_old_data():
    conn = sqlite3.connect(DB)
    conn.execute("""
        DELETE FROM sent_alerts
        WHERE datetime(created_at) < datetime('now', '-48 hours')
    """)
    conn.execute("""
        DELETE FROM processed_candles
        WHERE datetime(processed_at) < datetime('now', '-7 days')
    """)
    conn.commit()
    conn.close()
    print("[✓] Old data cleaned.")

# ─────────────────────────────────────────────
# DATA FETCHERS
# ─────────────────────────────────────────────

def get_candles(inst, bar="5m", limit=100):
    try:
        r = requests.get(
            f"{OKX}/market/candles",
            params={"instId": inst, "bar": bar, "limit": str(limit)},
            timeout=10
        )
        data = r.json().get("data", [])
        return list(reversed(data)) if data else []
    except Exception as e:
        print(f"[ERROR] get_candles {inst} {bar}: {e}")
        return []

def get_last_closed_candles(inst, bar="5m", limit=100):
    candles = get_candles(inst, bar=bar, limit=limit)
    return candles[:-1] if len(candles) >= 3 else []

def parse_okx_ts(ms):
    try:
        return datetime.fromtimestamp(int(ms) / 1000, tz=timezone.utc)
    except:
        return None

def get_last_closed_candle_ts(inst, bar="5m"):
    candles = get_last_closed_candles(inst, bar=bar, limit=5)
    if not candles:
        return None
    ts = parse_okx_ts(candles[-1][0])
    return ts.isoformat() if ts else None

def get_rsi_from_closes(closes, period=14):
    try:
        if len(closes) < period + 2:
            return 50, 50

        gains = [max(closes[i] - closes[i-1], 0) for i in range(1, len(closes))]
        losses = [abs(min(closes[i] - closes[i-1], 0)) for i in range(1, len(closes))]

        def rsi_calc(g, l):
            avg_gain = sum(g[-period:]) / period
            avg_loss = sum(l[-period:]) / period or 1e-9
            rs = avg_gain / avg_loss
            return round(100 - (100 / (1 + rs)), 2)

        current_rsi = rsi_calc(gains, losses)
        prev_rsi = rsi_calc(gains[:-1], losses[:-1])
        return current_rsi, prev_rsi
    except:
        return 50, 50

def get_funding(inst):
    try:
        r = requests.get(f"{OKX}/public/funding-rate", params={"instId": inst}, timeout=10)
        return float(r.json()["data"][0]["fundingRate"])
    except:
        return 0

def get_oi(ccy):
    try:
        r = requests.get(
            f"{OKX}/rubik/stat/contracts/open-interest-volume",
            params={"ccy": ccy, "period": "1H"},
            timeout=10
        )
        data = r.json().get("data", [])
        if len(data) >= 2:
            latest = float(data[-1][1])
            prev = float(data[-2][1])
            return round(((latest - prev) / prev) * 100, 3) if prev else 0
        return 0
    except:
        return 0

def get_ls(ccy):
    try:
        r = requests.get(
            f"{OKX}/rubik/stat/contracts/long-short-account-ratio",
            params={"ccy": ccy, "period": "1H"},
            timeout=10
        )
        data = r.json().get("data", [])
        return round(float(data[-1][1]), 3) if data else 1
    except:
        return 1

def get_fear_greed():
    try:
        r = requests.get("https://api.alternative.me/fng/?limit=1", timeout=10)
        d = r.json()["data"][0]
        return int(d["value"]), d["value_classification"]
    except:
        return 50, "Neutral"

# ─────────────────────────────────────────────
# HTF BIAS
# ─────────────────────────────────────────────

def calc_ema(closes, period):
    if len(closes) < period:
        return None
    k = 2 / (period + 1)
    ema = sum(closes[:period]) / period
    for p in closes[period:]:
        ema = p * k + ema * (1 - k)
    return ema

def get_bias(inst, bar="1H"):
    try:
        candles = get_last_closed_candles(inst, bar=bar, limit=80)
        closes = [float(c[4]) for c in candles]
        if len(closes) < 50:
            return "NEUTRAL"
        price = closes[-1]
        ema20 = calc_ema(closes, 20)
        ema50 = calc_ema(closes, 50)
        if ema20 is None or ema50 is None:
            return "NEUTRAL"
        if price > ema20 > ema50:
            return "BULL"
        if price < ema20 < ema50:
            return "BEAR"
        return "NEUTRAL"
    except:
        return "NEUTRAL"

def bias_allows(signal, bias_1h, bias_4h):
    if signal == "LONG":
        return bias_1h == "BULL" or bias_4h == "BULL"
    if signal == "SHORT":
        return bias_1h == "BEAR" or bias_4h == "BEAR"
    return True

# ─────────────────────────────────────────────
# SMC DETECTORS
# ─────────────────────────────────────────────

def detect_liquidity_sweep(candles, lookback=8):
    try:
        if len(candles) < lookback + 2:
            return None

        ref = candles[-(lookback+1):-1]
        last = candles[-1]

        ref_high = max(float(c[2]) for c in ref)
        ref_low = min(float(c[3]) for c in ref)
        last_high = float(last[2])
        last_low = float(last[3])
        last_close = float(last[4])

        if last_high > ref_high and last_close < ref_high:
            return {
                "signal": "SHORT",
                "reason": "Liquidity sweep above prior highs",
                "sweep_level": ref_high,
                "extreme": last_high,
                "close": last_close
            }

        if last_low < ref_low and last_close > ref_low:
            return {
                "signal": "LONG",
                "reason": "Liquidity sweep below prior lows",
                "sweep_level": ref_low,
                "extreme": last_low,
                "close": last_close
            }

        return None
    except:
        return None

def detect_mss(candles, direction, lookback=6):
    try:
        if len(candles) < lookback + 2:
            return False, None

        prior = candles[-(lookback+1):-1]
        last_close = float(candles[-1][4])
        prior_high = max(float(c[2]) for c in prior)
        prior_low = min(float(c[3]) for c in prior)

        if direction == "LONG" and last_close > prior_high:
            return True, prior_high
        if direction == "SHORT" and last_close < prior_low:
            return True, prior_low
        return False, None
    except:
        return False, None

def detect_judas(candles):
    try:
        if len(candles) < 4:
            return None

        prev = candles[-2]
        last = candles[-1]

        prev_high = float(prev[2])
        prev_low = float(prev[3])
        o = float(last[1])
        h = float(last[2])
        l = float(last[3])
        c = float(last[4])

        body_pct = abs(c - o) / max(h - l, 1e-9)
        if body_pct < 0.35:
            return None

        if h > prev_high and c < o and c < prev_high:
            return {
                "signal": "SHORT",
                "reason": "Judas pump — expansion above prior high rejected",
                "extreme": h
            }

        if l < prev_low and c > o and c > prev_low:
            return {
                "signal": "LONG",
                "reason": "Judas dump — expansion below prior low reclaimed",
                "extreme": l
            }

        return None
    except:
        return None

# ─────────────────────────────────────────────
# FILTERS / SCORE
# ─────────────────────────────────────────────

def session_filter():
    hour = datetime.now(timezone.utc).hour
    if 7 <= hour <= 10:
        return True, "London Session"
    if 12 <= hour <= 15:
        return True, "New York Session"
    return False, f"Dead Zone ({hour:02d}:00 UTC)"

def momentum_ok(oi):
    return abs(oi) > 0.1

def confidence_score(rsi, prev_rsi, ls, oi, signal, funding,
                     bias_1h, bias_4h, setup_type=None, has_mss=False):
    score = 0

    if signal == "LONG":
        if rsi < 45: score += 1
        if rsi < 38: score += 1
        if prev_rsi < rsi: score += 1
        if ls < 1.0: score += 1
        if funding < -0.0001: score += 1
        if bias_1h == "BULL": score += 1
        if bias_4h == "BULL": score += 2

    elif signal == "SHORT":
        if rsi > 55: score += 1
        if rsi > 62: score += 1
        if prev_rsi > rsi: score += 1
        if ls > 1.5: score += 1
        if funding > 0.0001: score += 1
        if bias_1h == "BEAR": score += 1
        if bias_4h == "BEAR": score += 2

    if setup_type in ("sweep", "judas"):
        score += 2

    if has_mss:
        score += 2

    if abs(oi) > 1.0:
        score += 2
    elif abs(oi) > 0.5:
        score += 1

    if signal != "NEUTRAL":
        score += 1

    return min(score, 10)

# ─────────────────────────────────────────────
# TRADE LEVELS
# ─────────────────────────────────────────────

def round_price(x):
    if x >= 1000:
        return round(x, 2)
    if x >= 100:
        return round(x, 3)
    if x >= 1:
        return round(x, 4)
    return round(x, 5)

TF_BUFFER = {
    "5m": 0.0015,
    "15m": 0.0025,
    "1H": 0.006,
    "4H": 0.010
}

def build_trade_levels(signal, candles, price, tf, setup_data):
    try:
        buf = TF_BUFFER.get(tf, 0.002)
        highs = [float(c[2]) for c in candles[-12:]]
        lows = [float(c[3]) for c in candles[-12:]]
        recent_high = max(highs)
        recent_low = min(lows)

        if signal == "LONG":
            extreme = setup_data.get("extreme", recent_low)
            entry_low = round_price(price * (1 - buf * 0.35))
            entry_high = round_price(price * (1 + buf * 0.15))
            sl = round_price(extreme * (1 - buf))
            risk = price - sl
            if risk <= 0:
                return None
            tp1 = round_price(price + risk * 1.5)
            tp2 = round_price(max(recent_high, price + risk * 2.2))
            rr = round((tp2 - price) / risk, 2)

        elif signal == "SHORT":
            extreme = setup_data.get("extreme", recent_high)
            entry_low = round_price(price * (1 - buf * 0.15))
            entry_high = round_price(price * (1 + buf * 0.35))
            sl = round_price(extreme * (1 + buf))
            risk = sl - price
            if risk <= 0:
                return None
            tp1 = round_price(price - risk * 1.5)
            tp2 = round_price(min(recent_low, price - risk * 2.2))
            rr = round((price - tp2) / risk, 2)

        else:
            return None

        return {
            "entry_low": entry_low,
            "entry_high": entry_high,
            "sl": sl,
            "tp1": tp1,
            "tp2": tp2,
            "rr": rr
        }
    except:
        return None

# ─────────────────────────────────────────────
# SIGNAL ENGINE
# ─────────────────────────────────────────────

def detect_signal(price, rsi, prev_rsi, funding, ls, oi, candles, tf):
    session_ok, session_name = session_filter()

    if tf in ["5m", "15m"] and not session_ok:
        return {
            "signal": "NEUTRAL",
            "reason": f"Dead zone — {session_name}",
            "session": session_name,
            "setup_type": None,
            "has_mss": False,
            "setup_data": {}
        }

    if not momentum_ok(oi):
        return {
            "signal": "NEUTRAL",
            "reason": f"No momentum (OI {oi}%)",
            "session": session_name,
            "setup_type": None,
            "has_mss": False,
            "setup_data": {}
        }

    sweep = detect_liquidity_sweep(candles, lookback=8)
    if sweep:
        mss_ok, mss_level = detect_mss(candles, sweep["signal"], lookback=6)
        rsi_ok = (
            (sweep["signal"] == "LONG" and rsi < 48) or
            (sweep["signal"] == "SHORT" and rsi > 52)
        )
        if rsi_ok:
            return {
                "signal": sweep["signal"],
                "reason": sweep["reason"] + (" + MSS confirmed" if mss_ok else ""),
                "session": session_name,
                "setup_type": "sweep",
                "has_mss": mss_ok,
                "setup_data": {**sweep, "mss_level": mss_level}
            }

    judas = detect_judas(candles)
    if judas:
        mss_ok, mss_level = detect_mss(candles, judas["signal"], lookback=5)
        return {
            "signal": judas["signal"],
            "reason": judas["reason"] + (" + MSS confirmed" if mss_ok else ""),
            "session": session_name,
            "setup_type": "judas",
            "has_mss": mss_ok,
            "setup_data": {**judas, "mss_level": mss_level}
        }

    if prev_rsi < 30 and rsi > prev_rsi and ls > 1.5:
        return {
            "signal": "LONG",
            "reason": "RSI reversal from oversold + crowd long",
            "session": session_name,
            "setup_type": "rsi_reversal",
            "has_mss": False,
            "setup_data": {}
        }

    if prev_rsi > 70 and rsi < prev_rsi and ls < 0.7:
        return {
            "signal": "SHORT",
            "reason": "RSI reversal from overbought + crowd short",
            "session": session_name,
            "setup_type": "rsi_reversal",
            "has_mss": False,
            "setup_data": {}
        }

    return {
        "signal": "NEUTRAL",
        "reason": "No SMC setup",
        "session": session_name,
        "setup_type": None,
        "has_mss": False,
        "setup_data": {}
    }

# ─────────────────────────────────────────────
# TELEGRAM
# ─────────────────────────────────────────────

def send_telegram(msg):
    try:
        r = requests.post(
            f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage",
            json={"chat_id": TELEGRAM_CHAT_ID, "text": msg, "parse_mode": "Markdown"},
            timeout=10
        )
        if r.status_code == 200:
            print("[✓] Telegram sent.")
        else:
            print("[WARN] Telegram failed:", r.text)
    except Exception as e:
        print("[ERROR] Telegram exception:", e)

def send_startup_ping():
    send_telegram("✅ APEX bot booted on Render. Scheduler loop started.")

# ─────────────────────────────────────────────
# MESSAGE BUILDER
# ─────────────────────────────────────────────

def build_message(tier, symbol, tf, signal, reason, session,
                  price, levels, score, bar,
                  prev_rsi, rsi, funding, ls, oi,
                  fg, fg_label, bias_1h, bias_4h, candle_ts):

    labels = {
        "pre": f"⚡ *PRE-ALERT — {symbol} {tf} {signal}*",
        "sniper": f"🎯 *APEX SNIPER — {symbol} {signal}*",
        "intraday": f"📈 *APEX INTRADAY — {symbol} {signal}*",
        "swing1h": f"📊 *APEX SWING 1H — {symbol} {signal}*",
        "swing4h": f"🏹 *APEX SWING 4H — {symbol} {signal}*",
    }

    notes = {
        "sniper": "⚠️ _Fast setup — act on next candle open_",
        "intraday": "🕒 _Intraday — easier to manage during work_",
        "swing1h": "🕐 _Swing — hold for hours_",
        "swing4h": "🗓 _Swing — hold for days_",
    }

    header = labels.get(tier, f"📌 *APEX — {symbol} {signal}*")

    if tier == "pre":
        return (
            f"{header}\n"
            f"`Candle: {candle_ts}`\n\n"
            f"*Reason:* {reason}\n"
            f"*Session:* {session}\n"
            f"*Bias:* 1H:{bias_1h} | 4H:{bias_4h}\n"
            f"*Price:* ${round_price(price)}\n"
            f"*Confidence:* {score}/10 `{bar}`\n\n"
            f"_Developing — wait for next candle confirmation._"
        )

    return (
        f"{header}\n"
        f"`Candle: {candle_ts}`\n\n"
        f"*Reason:* {reason}\n"
        f"*Session:* {session}\n"
        f"*Bias:* 1H:{bias_1h} | 4H:{bias_4h}\n\n"
        f"*Entry Zone:* ${levels['entry_low']} — ${levels['entry_high']}\n"
        f"*SL:* ${levels['sl']}\n"
        f"*TP1:* ${levels['tp1']}\n"
        f"*TP2:* ${levels['tp2']}\n"
        f"*R/R:* {levels['rr']}x\n\n"
        f"*Confidence:* {score}/10 `{bar}`\n"
        f"*RSI:* {prev_rsi} → {rsi}\n"
        f"*Funding:* {round(funding, 6)}\n"
        f"*L/S:* {ls} | *OI:* {oi}%\n"
        f"*F&G:* {fg} — {fg_label}\n\n"
        f"{notes.get(tier, '')}"
    )

# ─────────────────────────────────────────────
# CORE SCAN
# ─────────────────────────────────────────────

def scan_pair_tf(pair, tf, fg, fg_label):
    symbol = pair["symbol"]
    inst = pair["inst"]
    ccy = pair["ccy"]

    candle_ts = get_last_closed_candle_ts(inst, bar=tf)
    if not candle_ts:
        print(f"[{symbol}/{tf}] No closed candle.")
        return

    if candle_already_processed(symbol, tf, candle_ts):
        print(f"[{symbol}/{tf}] Already processed {candle_ts}")
        return

    limit = 100 if tf in ["1H", "4H"] else 80
    candles = get_last_closed_candles(inst, bar=tf, limit=limit)

    if len(candles) < 25:
        print(f"[{symbol}/{tf}] Not enough candles.")
        mark_candle_processed(symbol, tf, candle_ts)
        return

    closes = [float(c[4]) for c in candles]
    price = closes[-1]
    rsi, prev_rsi = get_rsi_from_closes(closes, 14)

    funding = get_funding(inst)
    ls = get_ls(ccy)
    oi = get_oi(ccy)

    bias_1h = get_bias(inst, "1H")
    bias_4h = get_bias(inst, "4H")

    print(f"[SCAN] {symbol}/{tf} | price={round_price(price)} | RSI {prev_rsi}->{rsi} | L/S={ls} | OI={oi}% | 1H={bias_1h} 4H={bias_4h}")

    sig_data = detect_signal(price, rsi, prev_rsi, funding, ls, oi, candles, tf)
    signal = sig_data["signal"]
    reason = sig_data["reason"]
    session = sig_data["session"]
    setup_type = sig_data["setup_type"]
    has_mss = sig_data["has_mss"]
    setup_data = sig_data["setup_data"]

    if signal != "NEUTRAL" and not bias_allows(signal, bias_1h, bias_4h):
        print(f"[BLOCKED] {symbol}/{tf} {signal} blocked by HTF bias.")
        mark_candle_processed(symbol, tf, candle_ts)
        return

    score = confidence_score(
        rsi, prev_rsi, ls, oi, signal, funding,
        bias_1h, bias_4h, setup_type, has_mss
    )

    print(f"[RESULT] {symbol}/{tf} | signal={signal} | type={setup_type} | MSS={has_mss} | score={score}/10 | reason={reason}")

    if signal == "NEUTRAL" or score < 4:
        mark_candle_processed(symbol, tf, candle_ts)
        return

    bar = "█" * score + "░" * (10 - score)

    if score <= 5:  # pre-alert for scores 4 AND 5
        pre_id = f"PRE-{symbol}-{tf}-{signal}-{candle_ts}"
        if not alert_exists(pre_id):
            msg = build_message(
                "pre", symbol, tf, signal, reason, session,
                price, None, score, bar,
                prev_rsi, rsi, funding, ls, oi,
                fg, fg_label, bias_1h, bias_4h, candle_ts
            )
            send_telegram(msg)
            save_alert(pre_id, symbol, tf, signal, score)

        mark_candle_processed(symbol, tf, candle_ts)
        return

    levels = build_trade_levels(signal, candles, price, tf, setup_data)
    if not levels or levels["rr"] < 1.5:
        print(f"[SKIP] {symbol}/{tf} RR too low or bad levels.")
        mark_candle_processed(symbol, tf, candle_ts)
        return

    if tf == "5m":
        tier = "sniper"
        alert_id = f"SNIPER-{symbol}-{tf}-{signal}-{candle_ts}"
    elif tf == "15m":
        tier = "intraday"
        alert_id = f"INTRADAY-{symbol}-{tf}-{signal}-{candle_ts}"
    elif tf == "1H":
        tier = "swing1h"
        alert_id = f"SWING1H-{symbol}-{tf}-{signal}-{candle_ts}"
    else:
        tier = "swing4h"
        alert_id = f"SWING4H-{symbol}-{tf}-{signal}-{candle_ts}"

    if not alert_exists(alert_id):
        msg = build_message(
            tier, symbol, tf, signal, reason, session,
            price, levels, score, bar,
            prev_rsi, rsi, funding, ls, oi,
            fg, fg_label, bias_1h, bias_4h, candle_ts
        )
        send_telegram(msg)
        save_alert(alert_id, symbol, tf, signal, score)

    mark_candle_processed(symbol, tf, candle_ts)

# ─────────────────────────────────────────────
# JOBS
# ─────────────────────────────────────────────

def scan_sniper():
    STATUS["last_scan"] = f"5m sniper scan @ {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M:%S UTC')}"
    print(f"\n[SNIPER 5m SCAN] {STATUS['last_scan']}")
    fg, fg_label = get_fear_greed()
    for pair in PAIRS:
        try:
            scan_pair_tf(pair, "5m", fg, fg_label)
        except Exception as e:
            STATUS["last_error"] = str(e)
            print(f"[ERROR] {pair['symbol']} 5m: {e}")
        time.sleep(1)

def scan_intraday():
    STATUS["last_scan"] = f"15m intraday scan @ {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M:%S UTC')}"
    print(f"\n[INTRADAY 15m SCAN] {STATUS['last_scan']}")
    fg, fg_label = get_fear_greed()
    for pair in PAIRS:
        try:
            scan_pair_tf(pair, "15m", fg, fg_label)
        except Exception as e:
            STATUS["last_error"] = str(e)
            print(f"[ERROR] {pair['symbol']} 15m: {e}")
        time.sleep(1)

def scan_swing_1h():
    STATUS["last_scan"] = f"1H swing scan @ {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M:%S UTC')}"
    print(f"\n[SWING 1H SCAN] {STATUS['last_scan']}")
    fg, fg_label = get_fear_greed()
    for pair in PAIRS:
        try:
            scan_pair_tf(pair, "1H", fg, fg_label)
        except Exception as e:
            STATUS["last_error"] = str(e)
            print(f"[ERROR] {pair['symbol']} 1H: {e}")
        time.sleep(1)

def scan_swing_4h():
    STATUS["last_scan"] = f"4H swing scan @ {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M:%S UTC')}"
    print(f"\n[SWING 4H SCAN] {STATUS['last_scan']}")
    fg, fg_label = get_fear_greed()
    for pair in PAIRS:
        try:
            scan_pair_tf(pair, "4H", fg, fg_label)
        except Exception as e:
            STATUS["last_error"] = str(e)
            print(f"[ERROR] {pair['symbol']} 4H: {e}")
        time.sleep(1)

# ─────────────────────────────────────────────
# MAIN
# ─────────────────────────────────────────────

def main():
    print("\n" + "█" * 60)
    print("  APEX ELITE — FINAL DEBUG BUILD STARTING")
    print("█" * 60 + "\n")

    print("[DEBUG] TELEGRAM_TOKEN exists:", bool(TELEGRAM_TOKEN))
    print("[DEBUG] TELEGRAM_CHAT_ID exists:", bool(TELEGRAM_CHAT_ID))

    keep_alive()
    print("[BOOT] Flask keep-alive thread started.")

    if not TELEGRAM_TOKEN or not TELEGRAM_CHAT_ID:
        STATUS["last_error"] = "Missing TELEGRAM_TOKEN or TELEGRAM_CHAT_ID"
        print("[ERROR] Missing TELEGRAM_TOKEN or TELEGRAM_CHAT_ID")
        return

    STATUS["bot_started"] = True

    init_db()
    print("[BOOT] DB initialized.")

    # Wait for network to fully settle before pinging Telegram
    print("[BOOT] Waiting 5s for network to settle...")
    time.sleep(5)

    try:
        send_startup_ping()
        print("[BOOT] Startup Telegram ping sent.")
    except Exception as e:
        STATUS["last_error"] = str(e)
        print("[BOOT ERROR] Telegram startup ping failed:", e)

    try:
        print("[BOOT] Running immediate startup scans...")
        scan_sniper()
        scan_intraday()
        scan_swing_1h()
        scan_swing_4h()  # was missing from boot
        print("[BOOT] Startup scans complete.")
    except Exception as e:
        STATUS["last_error"] = str(e)
        print("[BOOT ERROR] Startup scans failed:", e)

    schedule.every(3).minutes.do(scan_sniper)
    schedule.every(5).minutes.do(scan_intraday)
    schedule.every(15).minutes.do(scan_swing_1h)
    schedule.every(60).minutes.do(scan_swing_4h)
    schedule.every().day.at("00:10").do(clear_old_data)

    print("[✓] Scheduler started.")

    while True:
        try:
            STATUS["last_heartbeat"] = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
            print(f"[HEARTBEAT] {STATUS['last_heartbeat']} loop alive")
            schedule.run_pending()
        except Exception as e:
            STATUS["last_error"] = str(e)
            print("[ERROR] Scheduler loop:", e)
        time.sleep(30)

if __name__ == "__main__":
    main()
