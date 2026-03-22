"""
APEX BOT — ELITE SNIPER FINAL
Smart Money + Liquidity Sweeps + Judas Traps + Sessions
Data: CoinGecko + OKX. Zero cost. 24/7 on Render.

ALERT TIERS:
  🔍 SCOUT     — Score 4–5  | Wide window | RR ≥ 1.2 | 0.5% SL/TP buffer
  ⚡ PRE-ALERT — Score 5    | "Sniper forming — get ready"
  🎯 SNIPER    — Score 6+   | Tight entry  | RR ≥ 1.5 | 0.2% SL/TP buffer
"""

import os
import time
import requests
import schedule
from datetime import datetime, timezone
from keep_alive import keep_alive

TELEGRAM_TOKEN   = os.environ.get("TELEGRAM_TOKEN", "")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "")

OKX  = "https://www.okx.com/api/v5"
INST = "BTC-USDT-SWAP"

keep_alive()

# ─── DATA FETCHERS ───────────────────────────

def get_price():
    try:
        r = requests.get(
            "https://api.coingecko.com/api/v3/simple/price",
            params={"ids": "bitcoin", "vs_currencies": "usd"},
            timeout=10
        )
        return float(r.json()["bitcoin"]["usd"])
    except:
        try:
            r = requests.get(f"{OKX}/market/ticker", params={"instId": INST}, timeout=10)
            return float(r.json()["data"][0]["last"])
        except:
            return 0


def get_candles(bar="5m", limit=50):
    try:
        r = requests.get(
            f"{OKX}/market/candles",
            params={"instId": INST, "bar": bar, "limit": str(limit)},
            timeout=10
        )
        return list(reversed(r.json()["data"]))
    except:
        return []


def get_rsi():
    try:
        candles = get_candles(bar="15m", limit=100)
        closes  = [float(c[4]) for c in candles]
        gains, losses = [], []
        for i in range(1, len(closes)):
            diff = closes[i] - closes[i - 1]
            (gains if diff > 0 else losses).append(abs(diff))
        avg_gain = sum(gains[-14:]) / 14 if gains else 0
        avg_loss = sum(losses[-14:]) / 14 if losses else 1
        rs  = avg_gain / avg_loss if avg_loss else 0
        rsi = round(100 - (100 / (1 + rs)), 2)
        print(f"[✓] RSI: {rsi}")
        return rsi
    except:
        return 50


def get_funding():
    try:
        r = requests.get(
            f"{OKX}/public/funding-rate",
            params={"instId": INST},
            timeout=10
        )
        rate = float(r.json()["data"][0]["fundingRate"])
        print(f"[✓] Funding: {rate}")
        return rate
    except:
        return 0


def get_oi():
    try:
        r = requests.get(
            f"{OKX}/rubik/stat/contracts/open-interest-volume",
            params={"ccy": "BTC", "period": "1H"},
            timeout=10
        )
        data = r.json().get("data", [])
        if len(data) >= 2:
            latest = float(data[-1][1])
            prev   = float(data[-2][1])
            change = round(((latest - prev) / prev) * 100, 3) if prev else 0
            print(f"[✓] OI: {change}%")
            return change
        return 0
    except:
        return 0


def get_ls():
    try:
        r = requests.get(
            f"{OKX}/rubik/stat/contracts/long-short-account-ratio",
            params={"ccy": "BTC", "period": "1H"},
            timeout=10
        )
        data = r.json().get("data", [])
        if data:
            ratio = float(data[-1][1])
            print(f"[✓] L/S: {round(ratio,3)}")
            return round(ratio, 3)
        return 1
    except:
        return 1


def get_fear_greed():
    try:
        r = requests.get("https://api.alternative.me/fng/?limit=1", timeout=10)
        d = r.json()["data"][0]
        return int(d["value"]), d["value_classification"]
    except:
        return 50, "Neutral"


# ─── SMART MONEY DETECTION ───────────────────

def detect_liquidity_sweep(candles):
    try:
        if len(candles) < 6:
            return None, None
        highs  = [float(c[2]) for c in candles]
        lows   = [float(c[3]) for c in candles]
        closes = [float(c[4]) for c in candles]
        last_high  = highs[-1]
        prev_high  = max(highs[-6:-1])
        last_low   = lows[-1]
        prev_low   = min(lows[-6:-1])
        last_close = closes[-1]
        if last_high > prev_high and last_close < prev_high:
            return "SHORT", "Liquidity sweep above highs — trap confirmed"
        if last_low < prev_low and last_close > prev_low:
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
        last_open  = opens[-1]
        last_close = closes[-1]
        prev_close = closes[-2]
        move = (last_close - prev_close) / prev_close * 100
        if move > 0.5 and last_close < last_open:
            return "SHORT", "Judas pump — bearish reversal candle"
        if move < -0.5 and last_close > last_open:
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

def confidence_score(rsi, prev_rsi, ls, oi, signal, funding):
    score = 0

    if signal == "LONG":
        if rsi < 40:           score += 2
        if prev_rsi < rsi:     score += 1
        if ls < 1.0:           score += 2
        if funding < -0.0001:  score += 1
        if rsi < 32:           score += 2

    elif signal == "SHORT":
        if rsi > 60:           score += 2
        if prev_rsi > rsi:     score += 1
        if ls > 1.5:           score += 2
        if funding > 0.0001:   score += 1
        if rsi > 68:           score += 2

    # Momentum tiers — OI size matters
    if abs(oi) > 1.0:
        score += 3
    elif abs(oi) > 0.5:
        score += 2
    elif abs(oi) > 0.2:
        score += 1

    # Liquidity sweep bonus — highest quality SMC setup
    if signal != "NEUTRAL":
        score += 2

    return min(score, 10)


# ─── DYNAMIC SL/TP ───────────────────────────
# wide=True  → 0.5% buffer, for Scout / wide window trades
# wide=False → 0.2% buffer, for Sniper tight entries

def dynamic_levels(signal, candles, price, wide=False):
    try:
        buf = 0.005 if wide else 0.002          # 0.5% vs 0.2%
        highs = [float(c[2]) for c in candles[-10:]]
        lows  = [float(c[3]) for c in candles[-10:]]
        recent_high = max(highs)
        recent_low  = min(lows)
        if signal == "LONG":
            sl = round(recent_low  * (1 - buf), 2)
            tp = round(recent_high * (1 + buf), 2)
        elif signal == "SHORT":
            sl = round(recent_high * (1 + buf), 2)
            tp = round(recent_low  * (1 - buf), 2)
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


# ─── MAIN CYCLE ──────────────────────────────

prev_rsi          = None
last_alert_signal = None   # prevents spamming the same setup

def run():
    global prev_rsi, last_alert_signal
    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    print(f"\n{'='*48}")
    print(f"  APEX ELITE — {now}")
    print(f"{'='*48}")

    price        = get_price()
    candles      = get_candles(bar="5m", limit=50)
    rsi          = get_rsi()
    funding      = get_funding()
    ls           = get_ls()
    oi           = get_oi()
    fg, fg_label = get_fear_greed()

    print(f"BTC: ${price:,.2f} | RSI: {rsi} | L/S: {ls} | OI: {oi}% | F&G: {fg}")

    # First run — store RSI, stay silent
    if prev_rsi is None:
        prev_rsi = rsi
        print("[i] First run — RSI stored. Monitoring...")
        return

    signal, reason, session = apex_sniper(price, rsi, prev_rsi, funding, ls, oi, candles)
    score = confidence_score(rsi, prev_rsi, ls, oi, signal, funding)

    print(f"Signal: {signal} | Score: {score}/10 | {reason}")

    # Nothing actionable
    if signal == "NEUTRAL" or score < 4:
        last_alert_signal = None   # reset cooldown — market cleared
        prev_rsi = rsi
        return

    direction = "📈" if signal == "LONG" else "📉"
    bar       = "█" * score + "░" * (10 - score)

    # ── TIER 1: SNIPER (6+) ──────────────────────────────────────────────
    if score >= 6:
        sl, tp, rr = dynamic_levels(signal, candles, price, wide=False)
        if not sl or rr < 1.5:
            print(f"[SKIP] Sniper RR {rr} too low")
            prev_rsi = rsi
            return

        # Avoid re-firing the exact same sniper direction
        if last_alert_signal == f"SNIPER-{signal}":
            print("[i] Sniper already alerted for this setup — skipping duplicate")
            prev_rsi = rsi
            return

        msg = (
            f"🎯 *APEX SNIPER — {signal}* `{now}`\n\n"
            f"*Reason:* {reason}\n"
            f"*Session:* {session}\n\n"
            f"*Entry:* ${price:,.2f}\n"
            f"*TP:* ${tp:,}\n"
            f"*SL:* ${sl:,}\n"
            f"*R/R:* {rr}x {direction}\n\n"
            f"*Confidence:* {score}/10\n"
            f"`{bar}`\n\n"
            f"*RSI:* {prev_rsi} → {rsi}\n"
            f"*Funding:* {round(funding,5)}\n"
            f"*L/S:* {ls} | *OI:* {oi}%\n"
            f"*F&G:* {fg} — {fg_label}\n\n"
            f"⚠️ _Smart Money Active — TIGHT ENTRY_"
        )
        print(msg)
        send_telegram(msg)
        last_alert_signal = f"SNIPER-{signal}"

    # ── TIER 2: SCOUT wide window (4–5) ──────────────────────────────────
    elif 4 <= score <= 5:
        sl, tp, rr = dynamic_levels(signal, candles, price, wide=True)
        if not sl or rr < 1.2:
            print(f"[SKIP] Scout RR {rr} too low")

            # Still send pre-alert if score is exactly 5 (sniper forming)
            if score == 5 and last_alert_signal != f"PRE-{signal}":
                pre_msg = (
                    f"⚡ *PRE-ALERT — {signal} FORMING* `{now}`\n\n"
                    f"*Score:* {score}/10 — `{bar}`\n"
                    f"*Reason:* {reason}\n"
                    f"*Session:* {session}\n"
                    f"*BTC:* ${price:,.2f}\n\n"
                    f"_RR not clean yet — get chart ready. Sniper may fire next candle._"
                )
                print(pre_msg)
                send_telegram(pre_msg)
                last_alert_signal = f"PRE-{signal}"

            prev_rsi = rsi
            return

        # Avoid duplicate Scout alerts
        if last_alert_signal == f"SCOUT-{signal}":
            print("[i] Scout already alerted — skipping duplicate")
            prev_rsi = rsi
            return

        # Score 5 = also send the pre-alert first
        if score == 5 and last_alert_signal != f"PRE-{signal}":
            pre_msg = (
                f"⚡ *PRE-ALERT — {signal} FORMING* `{now}`\n\n"
                f"*Score:* {score}/10 — `{bar}`\n"
                f"*Reason:* {reason}\n"
                f"*Session:* {session}\n"
                f"*BTC:* ${price:,.2f}\n\n"
                f"_Setup building — chart open, sniper may fire next candle._"
            )
            print(pre_msg)
            send_telegram(pre_msg)

        scout_msg = (
            f"🔍 *APEX SCOUT — {signal}* `{now}`\n\n"
            f"*Reason:* {reason}\n"
            f"*Session:* {session}\n\n"
            f"*Entry:* ${price:,.2f}\n"
            f"*TP:* ${tp:,}\n"
            f"*SL:* ${sl:,}\n"
            f"*R/R:* {rr}x {direction}\n\n"
            f"*Confidence:* {score}/10\n"
            f"`{bar}`\n\n"
            f"*RSI:* {prev_rsi} → {rsi}\n"
            f"*Funding:* {round(funding,5)}\n"
            f"*L/S:* {ls} | *OI:* {oi}%\n"
            f"*F&G:* {fg} — {fg_label}\n\n"
            f"📐 _Wide Window Entry — Larger SL, more room to breathe_"
        )
        print(scout_msg)
        send_telegram(scout_msg)
        last_alert_signal = f"SCOUT-{signal}"

    prev_rsi = rsi


def main():
    print("\n" + "█"*48)
    print("  APEX ELITE — 3-TIER ALERT MODE")
    print("  🔍 Scout (4-5) | ⚡ Pre-Alert (5) | 🎯 Sniper (6+)")
    print("█"*48 + "\n")

    if not TELEGRAM_TOKEN or not TELEGRAM_CHAT_ID:
        print("[ERROR] Missing TELEGRAM_TOKEN or TELEGRAM_CHAT_ID")
        return

    run()
    schedule.every(5).minutes.do(run)
    print("[✓] Monitoring — every 5 minutes.\n")

    while True:
        schedule.run_pending()
        time.sleep(10)


if __name__ == "__main__":
    main()
