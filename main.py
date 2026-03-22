"""
APEX BOT — ELITE SNIPER | FINAL
Pairs  : BTC, ETH, SOL, XRP, BNB
Tiers  : 🎯 Sniper (5m/15m) | 📊 Swing 1H | 🏹 Swing 4H
HTF    : EMA20 + EMA50 bias filter
Storage: SQLite — survives session restarts
Scan   : 5m → every 3 min | 1H → every 15 min | 4H → every 60 min
"""

import os
import time
import sqlite3
import requests
import schedule
from datetime import datetime, timezone
from keep_alive import keep_alive

TELEGRAM_TOKEN   = os.environ.get("TELEGRAM_TOKEN", "")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "")

OKX = "https://www.okx.com/api/v5"
DB  = "apex_signals.db"

PAIRS = [
    {"symbol": "BTC", "inst": "BTC-USDT-SWAP", "ccy": "BTC"},
    {"symbol": "ETH", "inst": "ETH-USDT-SWAP", "ccy": "ETH"},
    {"symbol": "SOL", "inst": "SOL-USDT-SWAP", "ccy": "SOL"},
    {"symbol": "XRP", "inst": "XRP-USDT-SWAP", "ccy": "XRP"},
    {"symbol": "BNB", "inst": "BNB-USDT-SWAP", "ccy": "BNB"},
]

# Per-pair RSI memory (still in memory — only needs one cycle to warm up)
prev_rsi_map = {p["symbol"]: None for p in PAIRS}

keep_alive()


# ─── SQLITE STORAGE ──────────────────────────

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
    conn.commit()
    conn.close()
    print("[✓] DB ready.")


def alert_exists(alert_id):
    conn = sqlite3.connect(DB)
    row  = conn.execute("SELECT 1 FROM sent_alerts WHERE id=?", (alert_id,)).fetchone()
    conn.close()
    return row is not None


def save_alert(alert_id, symbol, timeframe, signal, score):
    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    conn = sqlite3.connect(DB)
    conn.execute(
        "INSERT OR IGNORE INTO sent_alerts VALUES (?,?,?,?,?,?)",
        (alert_id, symbol, timeframe, signal, score, now)
    )
    conn.commit()
    conn.close()


def clear_old_alerts():
    """Remove alerts older than 48 hours to keep DB lean."""
    conn = sqlite3.connect(DB)
    conn.execute("""
        DELETE FROM sent_alerts
        WHERE created_at < datetime('now', '-48 hours')
    """)
    conn.commit()
    conn.close()


# ─── DATA FETCHERS ───────────────────────────

def get_price(inst):
    try:
        r = requests.get(f"{OKX}/market/ticker", params={"instId": inst}, timeout=10)
        return float(r.json()["data"][0]["last"])
    except:
        return 0


def get_candles(inst, bar="5m", limit=50):
    try:
        r = requests.get(
            f"{OKX}/market/candles",
            params={"instId": inst, "bar": bar, "limit": str(limit)},
            timeout=10
        )
        return list(reversed(r.json()["data"]))
    except:
        return []


def get_rsi(candles):
    try:
        closes = [float(c[4]) for c in candles]
        gains, losses = [], []
        for i in range(1, len(closes)):
            diff = closes[i] - closes[i - 1]
            (gains if diff > 0 else losses).append(abs(diff))
        avg_gain = sum(gains[-14:]) / 14 if gains else 0
        avg_loss = sum(losses[-14:]) / 14 if losses else 1
        rs = avg_gain / avg_loss if avg_loss else 0
        return round(100 - (100 / (1 + rs)), 2)
    except:
        return 50


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
            prev   = float(data[-2][1])
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


# ─── HTF BIAS (EMA20 + EMA50) ────────────────

def calc_ema(closes, period):
    if len(closes) < period:
        return None
    k   = 2 / (period + 1)
    val = sum(closes[:period]) / period
    for p in closes[period:]:
        val = p * k + val * (1 - k)
    return val


def get_bias(inst, price, bar="1H"):
    """
    BULL  = price > EMA20 > EMA50
    BEAR  = price < EMA20 < EMA50
    NEUTRAL = mixed
    """
    try:
        candles = get_candles(inst, bar=bar, limit=60)
        closes  = [float(c[4]) for c in candles]
        ema20   = calc_ema(closes, 20)
        ema50   = calc_ema(closes, 50)
        if not ema20 or not ema50:
            return "NEUTRAL"
        if price > ema20 > ema50:
            return "BULL"
        elif price < ema20 < ema50:
            return "BEAR"
        return "NEUTRAL"
    except:
        return "NEUTRAL"


def bias_allows(signal, bias_1h, bias_4h):
    """
    Need at least one HTF aligned.
    Both against = hard block.
    """
    if signal == "LONG":
        return bias_1h == "BULL" or bias_4h == "BULL"
    if signal == "SHORT":
        return bias_1h == "BEAR" or bias_4h == "BEAR"
    return True


# ─── SMART MONEY DETECTORS ───────────────────

def detect_liquidity_sweep(candles):
    try:
        if len(candles) < 6:
            return None, None
        highs  = [float(c[2]) for c in candles]
        lows   = [float(c[3]) for c in candles]
        closes = [float(c[4]) for c in candles]
        if highs[-1] > max(highs[-6:-1]) and closes[-1] < max(highs[-6:-1]):
            return "SHORT", "Liquidity sweep above highs — trap confirmed"
        if lows[-1] < min(lows[-6:-1]) and closes[-1] > min(lows[-6:-1]):
            return "LONG", "Liquidity sweep below lows — trap confirmed"
        return None, None
    except:
        return None, None


def detect_judas(candles):
    try:
        if len(candles) < 3:
            return None, None
        opens  = [float(c[1]) for c in candles]
        closes = [float(c[4]) for c in candles]
        move = (closes[-1] - closes[-2]) / closes[-2] * 100
        if move > 0.5 and closes[-1] < opens[-1]:
            return "SHORT", "Judas pump — bearish reversal candle"
        if move < -0.5 and closes[-1] > opens[-1]:
            return "LONG", "Judas dump — bullish reversal candle"
        return None, None
    except:
        return None, None


# ─── SESSION FILTER ──────────────────────────

def session_filter():
    hour = datetime.now(timezone.utc).hour
    if 7 <= hour <= 10:
        return True, "London Session"
    if 12 <= hour <= 15:
        return True, "New York Session"
    return False, f"Dead Zone ({hour:02d}:00 UTC)"


def momentum_ok(oi):
    return abs(oi) > 0.1


# ─── CONFIDENCE SCORE ────────────────────────

def confidence_score(rsi, prev_rsi, ls, oi, signal, funding, bias_1h, bias_4h):
    score = 0

    if signal == "LONG":
        if rsi < 40:            score += 2
        if prev_rsi < rsi:      score += 1
        if ls < 1.0:            score += 2
        if funding < -0.0001:   score += 1
        if rsi < 32:            score += 2
        if bias_1h == "BULL":   score += 1
        if bias_4h == "BULL":   score += 2

    elif signal == "SHORT":
        if rsi > 60:            score += 2
        if prev_rsi > rsi:      score += 1
        if ls > 1.5:            score += 2
        if funding > 0.0001:    score += 1
        if rsi > 68:            score += 2
        if bias_1h == "BEAR":   score += 1
        if bias_4h == "BEAR":   score += 2

    if abs(oi) > 1.0:    score += 3
    elif abs(oi) > 0.5:  score += 2
    elif abs(oi) > 0.2:  score += 1

    if signal != "NEUTRAL": score += 1

    return min(score, 10)


# ─── DYNAMIC SL/TP ───────────────────────────

SL_BUFFERS = {
    "5m":  0.002,   # 0.2% — sniper tight
    "15m": 0.003,   # 0.3%
    "1H":  0.010,   # 1.0% — swing room
    "4H":  0.015,   # 1.5% — wider swing
}

def dynamic_levels(signal, candles, price, tf="5m"):
    try:
        buf   = SL_BUFFERS.get(tf, 0.002)
        highs = [float(c[2]) for c in candles[-10:]]
        lows  = [float(c[3]) for c in candles[-10:]]
        if signal == "LONG":
            sl = round(min(lows)  * (1 - buf), 4)
            tp = round(max(highs) * (1 + buf), 4)
        elif signal == "SHORT":
            sl = round(max(highs) * (1 + buf), 4)
            tp = round(min(lows)  * (1 - buf), 4)
        else:
            return None, None, 0
        sl_dist = abs(price - sl)
        tp_dist = abs(price - tp)
        rr = round(tp_dist / sl_dist, 2) if sl_dist else 0
        return sl, tp, rr
    except:
        return None, None, 0


# ─── SIGNAL DETECTION ────────────────────────

def detect_signal(price, rsi, prev_rsi, funding, ls, oi, candles, tf):
    session_ok, session_name = session_filter()

    # Swing TFs don't need session filter — they run all day
    if tf in ["5m", "15m"] and not session_ok:
        return "NEUTRAL", f"Dead zone — {session_name}", session_name

    if not momentum_ok(oi):
        return "NEUTRAL", f"No momentum (OI {oi}%)", session_name

    sweep_sig, sweep_reason = detect_liquidity_sweep(candles)
    if sweep_sig:
        if sweep_sig == "LONG"  and rsi < 45:
            return "LONG",  sweep_reason, session_name
        if sweep_sig == "SHORT" and rsi > 55:
            return "SHORT", sweep_reason, session_name

    judas_sig, judas_reason = detect_judas(candles)
    if judas_sig:
        return judas_sig, judas_reason, session_name

    if prev_rsi and prev_rsi < 30 and rsi > prev_rsi and ls > 1.5:
        return "LONG",  "RSI reversal from oversold + crowd long", session_name
    if prev_rsi and prev_rsi > 70 and rsi < prev_rsi and ls < 0.7:
        return "SHORT", "RSI reversal from overbought + crowd short", session_name

    return "NEUTRAL", "No SMC setup", session_name


# ─── TELEGRAM ────────────────────────────────

def send_telegram(msg):
    try:
        r = requests.post(
            f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage",
            json={"chat_id": TELEGRAM_CHAT_ID, "text": msg, "parse_mode": "Markdown"},
            timeout=10
        )
        if r.status_code == 200:
            print("  [✓] Telegram sent.")
        else:
            print(f"  [WARN] Telegram: {r.text}")
    except Exception as e:
        print(f"  [ERROR] Telegram: {e}")


# ─── ALERT BUILDER ───────────────────────────

def build_message(tier, symbol, tf, signal, reason, session,
                  price, tp, sl, rr, score, bar,
                  prev_rsi, rsi, funding, ls, oi,
                  fg, fg_label, bias_1h, bias_4h, now):

    direction = "📈" if signal == "LONG" else "📉"

    TIER_LABELS = {
        "pre":    f"⚡ *PRE-ALERT — {symbol} {tf} {signal} FORMING*",
        "scout":  f"🔍 *APEX SCOUT — {symbol} {tf} {signal}*",
        "sniper": f"🎯 *APEX SNIPER — {symbol} {signal}*",
        "swing1h":f"📊 *APEX SWING 1H — {symbol} {signal}*",
        "swing4h":f"🏹 *APEX SWING 4H — {symbol} {signal}*",
    }

    header = TIER_LABELS.get(tier, f"📌 *APEX — {symbol} {signal}*")

    if tier == "pre":
        return (
            f"{header} `{now}`\n\n"
            f"*Score:* {score}/10 — `{bar}`\n"
            f"*Reason:* {reason}\n"
            f"*Session:* {session}\n"
            f"*Bias:* 1H:{bias_1h} 4H:{bias_4h}\n"
            f"*{symbol}:* ${price:,.4f}\n\n"
            f"_Get chart ready — entry may fire next candle._"
        )

    hold_note = {
        "scout":   "📐 _Wide window — larger SL, more room_",
        "sniper":  "⚠️ _Smart Money Active — TIGHT ENTRY_",
        "swing1h": "🕐 _Swing trade — set & walk away. Hold hours._",
        "swing4h": "🗓 _Swing trade — set & walk away. Hold days._",
    }.get(tier, "")

    return (
        f"{header} `{now}`\n\n"
        f"*Reason:* {reason}\n"
        f"*Session:* {session}\n"
        f"*Bias:* 1H:{bias_1h} 4H:{bias_4h}\n\n"
        f"*Entry:* ${price:,.4f}\n"
        f"*TP:* ${tp:,}\n"
        f"*SL:* ${sl:,}\n"
        f"*R/R:* {rr}x {direction}\n\n"
        f"*Confidence:* {score}/10\n"
        f"`{bar}`\n\n"
        f"*RSI:* {prev_rsi} → {rsi}\n"
        f"*Funding:* {round(funding,6)}\n"
        f"*L/S:* {ls} | *OI:* {oi}%\n"
        f"*F&G:* {fg} — {fg_label}\n\n"
        f"{hold_note}"
    )


# ─── CORE SCAN FUNCTION ──────────────────────

def scan_pair_tf(pair, tf, now, fg, fg_label):
    symbol  = pair["symbol"]
    inst    = pair["inst"]
    ccy     = pair["ccy"]

    # Candle limit — more history for higher TFs
    limit   = 100 if tf in ["1H", "4H"] else 60
    rsi_tf  = "15m" if tf == "5m" else tf

    candles  = get_candles(inst, bar=tf,    limit=limit)
    rsi_c    = get_candles(inst, bar=rsi_tf, limit=100)
    price    = get_price(inst)
    rsi      = get_rsi(rsi_c)
    funding  = get_funding(inst)
    ls       = get_ls(ccy)
    oi       = get_oi(ccy)
    bias_1h  = get_bias(inst, price, bar="1H")
    bias_4h  = get_bias(inst, price, bar="4H")

    prev_rsi = prev_rsi_map[symbol]
    if prev_rsi is None:
        prev_rsi_map[symbol] = rsi
        print(f"  [{symbol}] First run — RSI stored.")
        return

    print(f"  {symbol}/{tf}: ${price:,.4f} | RSI:{rsi} | L/S:{ls} | OI:{oi}% | 1H:{bias_1h} 4H:{bias_4h}")

    signal, reason, session = detect_signal(price, rsi, prev_rsi, funding, ls, oi, candles, tf)

    # HTF gate
    if signal != "NEUTRAL" and not bias_allows(signal, bias_1h, bias_4h):
        print(f"  [{symbol}] BLOCKED — bias against {signal} (1H:{bias_1h} 4H:{bias_4h})")
        prev_rsi_map[symbol] = rsi
        return

    score = confidence_score(rsi, prev_rsi, ls, oi, signal, funding, bias_1h, bias_4h)
    print(f"  [{symbol}] {signal} | Score:{score}/10 | {reason}")

    # Score thresholds by timeframe
    min_score = 6 if tf in ["5m", "15m"] else 5

    if signal == "NEUTRAL" or score < 4:
        prev_rsi_map[symbol] = rsi
        return

    bar = "█" * score + "░" * (10 - score)

    # ── SNIPER TIER (5m/15m, score 6+) ───────
    if tf in ["5m", "15m"]:

        if score == 5:
            pre_id = f"PRE-{symbol}-{tf}-{signal}-{now[:13]}"
            if not alert_exists(pre_id):
                msg = build_message("pre", symbol, tf, signal, reason, session,
                                    price, None, None, None, score, bar,
                                    prev_rsi, rsi, funding, ls, oi,
                                    fg, fg_label, bias_1h, bias_4h, now)
                send_telegram(msg)
                save_alert(pre_id, symbol, tf, signal, score)

        elif score >= 6:
            sl, tp, rr = dynamic_levels(signal, candles, price, tf="5m")
            if not sl or rr < 1.5:
                print(f"  [SKIP] Sniper RR {rr} too low")
                prev_rsi_map[symbol] = rsi
                return

            sniper_id = f"SNIPER-{symbol}-{tf}-{signal}-{now[:13]}"
            if alert_exists(sniper_id):
                print(f"  [DUP] Sniper already sent")
                prev_rsi_map[symbol] = rsi
                return

            msg = build_message("sniper", symbol, tf, signal, reason, session,
                                 price, tp, sl, rr, score, bar,
                                 prev_rsi, rsi, funding, ls, oi,
                                 fg, fg_label, bias_1h, bias_4h, now)
            send_telegram(msg)
            save_alert(sniper_id, symbol, tf, signal, score)

    # ── SWING TIER (1H/4H, score 5+) ─────────
    elif tf in ["1H", "4H"]:

        if score < min_score:
            prev_rsi_map[symbol] = rsi
            return

        tier   = "swing1h" if tf == "1H" else "swing4h"
        rr_min = 1.5

        # Pre-alert for swing — fires one candle early
        if score == 5:
            pre_id = f"PRE-{symbol}-{tf}-{signal}-{now[:13]}"
            if not alert_exists(pre_id):
                msg = build_message("pre", symbol, tf, signal, reason, session,
                                    price, None, None, None, score, bar,
                                    prev_rsi, rsi, funding, ls, oi,
                                    fg, fg_label, bias_1h, bias_4h, now)
                send_telegram(msg)
                save_alert(pre_id, symbol, tf, signal, score)

        elif score >= 6:
            sl, tp, rr = dynamic_levels(signal, candles, price, tf=tf)
            if not sl or rr < rr_min:
                print(f"  [SKIP] Swing {tf} RR {rr} too low")
                prev_rsi_map[symbol] = rsi
                return

            swing_id = f"{tier.upper()}-{symbol}-{signal}-{now[:13]}"
            if alert_exists(swing_id):
                print(f"  [DUP] Swing already sent")
                prev_rsi_map[symbol] = rsi
                return

            msg = build_message(tier, symbol, tf, signal, reason, session,
                                 price, tp, sl, rr, score, bar,
                                 prev_rsi, rsi, funding, ls, oi,
                                 fg, fg_label, bias_1h, bias_4h, now)
            send_telegram(msg)
            save_alert(swing_id, symbol, tf, signal, score)

    prev_rsi_map[symbol] = rsi


# ─── SCHEDULED SCAN JOBS ─────────────────────

def scan_sniper():
    """5m candles — runs every 3 minutes."""
    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    print(f"\n[SNIPER SCAN] {now}")
    fg, fg_label = get_fear_greed()
    for pair in PAIRS:
        try:
            scan_pair_tf(pair, "5m", now, fg, fg_label)
        except Exception as e:
            print(f"  [ERROR] {pair['symbol']} 5m: {e}")
        time.sleep(2)


def scan_swing_1h():
    """1H candles — runs every 15 minutes."""
    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    print(f"\n[SWING 1H SCAN] {now}")
    fg, fg_label = get_fear_greed()
    for pair in PAIRS:
        try:
            scan_pair_tf(pair, "1H", now, fg, fg_label)
        except Exception as e:
            print(f"  [ERROR] {pair['symbol']} 1H: {e}")
        time.sleep(2)


def scan_swing_4h():
    """4H candles — runs every 60 minutes."""
    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    print(f"\n[SWING 4H SCAN] {now}")
    fg, fg_label = get_fear_greed()
    for pair in PAIRS:
        try:
            scan_pair_tf(pair, "4H", now, fg, fg_label)
        except Exception as e:
            print(f"  [ERROR] {pair['symbol']} 4H: {e}")
        time.sleep(2)


def cleanup():
    """Remove old alerts daily."""
    clear_old_alerts()
    print("[✓] Old alerts cleaned.")


# ─── MAIN ────────────────────────────────────

def main():
    print("\n" + "█"*50)
    print("  APEX ELITE — MULTI-PAIR FINAL")
    print("  Pairs : BTC | ETH | SOL | XRP | BNB")
    print("  Tiers : 🎯 Sniper | 📊 Swing 1H | 🏹 Swing 4H")
    print("  HTF   : EMA20 + EMA50 bias filter")
    print("  Store : SQLite dedup")
    print("█"*50 + "\n")

    if not TELEGRAM_TOKEN or not TELEGRAM_CHAT_ID:
        print("[ERROR] Missing TELEGRAM_TOKEN or TELEGRAM_CHAT_ID")
        return

    init_db()

    # First pass — warm up RSI memory for all pairs
    print("[i] Warming up — initial scan...\n")
    fg, fg_label = get_fear_greed()
    for pair in PAIRS:
        try:
            scan_pair_tf(pair, "5m", "warmup", fg, fg_label)
        except Exception as e:
            print(f"  [ERROR] warmup {pair['symbol']}: {e}")
        time.sleep(2)

    # Schedule all three scan tiers
    schedule.
