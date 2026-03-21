"""
APEX BOT — ELITE SNIPER | MULTI-PAIR + HTF TREND FILTER
Pairs: BTC, ETH, SOL, XRP, BNB
Smart Money + Liquidity Sweeps + Judas Traps + Sessions
HTF Filter: 1H EMA20 + 4H EMA20 — signal must align with at least one
SILENT MODE: Only score 6+ signals fire to Telegram.
"""

import os
import time
import requests
import schedule
from datetime import datetime, timezone
from keep_alive import keep_alive

TELEGRAM_TOKEN   = os.environ.get("TELEGRAM_TOKEN", "")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "")

OKX = "https://www.okx.com/api/v5"

PAIRS = [
    {"symbol": "BTC", "inst": "BTC-USDT-SWAP", "ccy": "BTC"},
    {"symbol": "ETH", "inst": "ETH-USDT-SWAP", "ccy": "ETH"},
    {"symbol": "SOL", "inst": "SOL-USDT-SWAP", "ccy": "SOL"},
    {"symbol": "XRP", "inst": "XRP-USDT-SWAP", "ccy": "XRP"},
    {"symbol": "BNB", "inst": "BNB-USDT-SWAP", "ccy": "BNB"},
]

prev_rsi_map = {p["symbol"]: None for p in PAIRS}

keep_alive()

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


def get_rsi(inst):
    try:
        candles = get_candles(inst, bar="15m", limit=100)
        closes  = [float(c[4]) for c in candles]
        gains, losses = [], []
        for i in range(1, len(closes)):
            diff = closes[i] - closes[i - 1]
            (gains if diff > 0 else losses).append(abs(diff))
        avg_gain = sum(gains[-14:]) / 14 if gains else 0
        avg_loss = sum(losses[-14:]) / 14 if losses else 1
        rs  = avg_gain / avg_loss if avg_loss else 0
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


# ─── HTF TREND FILTER ────────────────────────

def calc_ema(closes, period):
    if len(closes) < period:
        return None
    k   = 2 / (period + 1)
    val = sum(closes[:period]) / period
    for price in closes[period:]:
        val = price * k + val * (1 - k)
    return round(val, 6)


def get_htf_trend(inst, price):
    """
    Returns (trend_1h, trend_4h, label)
    BULL = price above EMA20, BEAR = price below EMA20
    """
    try:
        candles_1h = get_candles(inst, bar="1H", limit=30)
        closes_1h  = [float(c[4]) for c in candles_1h]
        ema_1h     = calc_ema(closes_1h, 20)
        trend_1h   = ("BULL" if price > ema_1h else "BEAR") if ema_1h else "NEUTRAL"
    except:
        trend_1h = "NEUTRAL"

    try:
        candles_4h = get_candles(inst, bar="4H", limit=30)
        closes_4h  = [float(c[4]) for c in candles_4h]
        ema_4h     = calc_ema(closes_4h, 20)
        trend_4h   = ("BULL" if price > ema_4h else "BEAR") if ema_4h else "NEUTRAL"
    except:
        trend_4h = "NEUTRAL"

    label = f"1H:{trend_1h} 4H:{trend_4h}"
    return trend_1h, trend_4h, label


def htf_allows(signal, trend_1h, trend_4h):
    """
    LONG needs at least one HTF bullish.
    SHORT needs at least one HTF bearish.
    Both against = hard block.
    """
    if signal == "LONG":
        return trend_1h == "BULL" or trend_4h == "BULL"
    if signal == "SHORT":
        return trend_1h == "BEAR" or trend_4h == "BEAR"
    return True


# ─── SMART MONEY DETECTION ───────────────────

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

def confidence_score(rsi, prev_rsi, ls, oi, signal, funding, trend_1h, trend_4h):
    score = 0

    if signal == "LONG":
        if rsi < 40:           score += 2
        if prev_rsi < rsi:     score += 1
        if ls < 1.0:           score += 2
        if funding < -0.0001:  score += 1
        if rsi < 32:           score += 2
        if trend_1h == "BULL": score += 1  # 1H confirmation
        if trend_4h == "BULL": score += 2  # 4H carries more weight

    elif signal == "SHORT":
        if rsi > 60:           score += 2
        if prev_rsi > rsi:     score += 1
        if ls > 1.5:           score += 2
        if funding > 0.0001:   score += 1
        if rsi > 68:           score += 2
        if trend_1h == "BEAR": score += 1
        if trend_4h == "BEAR": score += 2

    if abs(oi) > 1.0:   score += 3
    elif abs(oi) > 0.5: score += 2
    elif abs(oi) > 0.2: score += 1

    if signal != "NEUTRAL": score += 1

    return min(score, 10)


# ─── DYNAMIC SL/TP ───────────────────────────

def dynamic_levels(signal, candles, price):
    try:
        highs = [float(c[2]) for c in candles[-10:]]
        lows  = [float(c[3]) for c in candles[-10:]]
        if signal == "LONG":
            sl = round(min(lows)  * 0.998, 4)
            tp = round(max(highs) * 1.002, 4)
        elif signal == "SHORT":
            sl = round(max(highs) * 1.002, 4)
            tp = round(min(lows)  * 0.998, 4)
        else:
            return None, None, 0
        sl_dist = abs(price - sl)
        tp_dist = abs(price - tp)
        rr = round(tp_dist / sl_dist, 2) if sl_dist else 0
        return sl, tp, rr
    except:
        return None, None, 0


# ─── MASTER SIGNAL LOGIC ─────────────────────

def apex_sniper(price, rsi, prev_rsi, funding, ls, oi, candles):
    session_ok, session_name = session_filter()
    if not session_ok:
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
        return "LONG",  "RSI reversal from oversold + long crowd", session_name
    if prev_rsi and prev_rsi > 70 and rsi < prev_rsi and ls < 0.7:
        return "SHORT", "RSI reversal from overbought + short crowd", session_name

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
            print("[✓] Telegram sent.")
        else:
            print(f"[WARN] Telegram: {r.text}")
    except Exception as e:
        print(f"[ERROR] Telegram: {e}")


# ─── PER-PAIR CYCLE ──────────────────────────

def run_pair(pair, now, fg, fg_label):
    symbol = pair["symbol"]
    inst   = pair["inst"]
    ccy    = pair["ccy"]

    price   = get_price(inst)
    candles = get_candles(inst, bar="5m", limit=50)
    rsi     = get_rsi(inst)
    funding = get_funding(inst)
    ls      = get_ls(ccy)
    oi      = get_oi(ccy)

    trend_1h, trend_4h, htf_label = get_htf_trend(inst, price)

    print(f"{symbol}: ${price:,.4f} | RSI:{rsi} | L/S:{ls} | OI:{oi}% | {htf_label}")

    prev_rsi = prev_rsi_map[symbol]

    if prev_rsi is None:
        prev_rsi_map[symbol] = rsi
        print(f"  [i] First run — RSI stored.")
        return

    signal, reason, session = apex_sniper(price, rsi, prev_rsi, funding, ls, oi, candles)

    # ── HTF GATE ─────────────────────────────
    if signal != "NEUTRAL" and not htf_allows(signal, trend_1h, trend_4h):
        print(f"  [BLOCKED] {signal} — HTF against signal ({htf_label})")
        prev_rsi_map[symbol] = rsi
        return

    score = confidence_score(rsi, prev_rsi, ls, oi, signal, funding, trend_1h, trend_4h)
    print(f"  → {signal} | Score:{score}/10 | {reason}")

    if signal == "NEUTRAL" or score < 6:
        prev_rsi_map[symbol] = rsi
        return

    sl, tp, rr = dynamic_levels(signal, candles, price)
    if not sl or rr < 1.5:
        print(f"  [SKIP] RR {rr} too low")
        prev_rsi_map[symbol] = rsi
        return

    direction = "📈" if signal == "LONG" else "📉"
    bar = "█" * score + "░" * (10 - score)

    msg = (
        f"🎯 *APEX ELITE — {symbol} {signal}* `{now}`\n\n"
        f"*Reason:* {reason}\n"
        f"*Session:* {session}\n"
        f"*Trend:* {htf_label}\n\n"
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
        f"⚠️ _Smart Money Active_"
    )

    print(msg)
    send_telegram(msg)
    prev_rsi_map[symbol] = rsi


# ─── MAIN CYCLE ──────────────────────────────

def run():
    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    print(f"\n{'='*48}")
    print(f"  APEX ELITE MULTI — {now}")
    print(f"{'='*48}")

    fg, fg_label = get_fear_greed()
    print(f"F&G: {fg} — {fg_label}\n")

    for pair in PAIRS:
        try:
            run_pair(pair, now, fg, fg_label)
        except Exception as e:
            print(f"[ERROR] {pair['symbol']}: {e}")
        time.sleep(2)


def main():
    print("\n" + "█"*48)
    print("  APEX ELITE — MULTI-PAIR + HTF FILTER")
    print("  Pairs: BTC | ETH | SOL | XRP | BNB")
    print("  HTF: 1H EMA20 + 4H EMA20 confirmation")
    print("  Only 6/10+ signals fire to Telegram")
    print("█"*48 + "\n")

    if not TELEGRAM_TOKEN or not TELEGRAM_CHAT_ID:
        print("[ERROR] Missing TELEGRAM_TOKEN or TELEGRAM_CHAT_ID")
        return

    run()
    schedule.every(5).minutes.do(run)
    print("[✓] Monitoring silently — every 5 minutes.\n")

    while True:
        schedule.run_pending()
        time.sleep(10)


if __name__ == "__main__":
    main()
        
