from keep_alive import keep_alive
keep_alive()
"""
APEX BOT v7 — ZERO BLOCK EDITION
CoinGecko (price) + OKX (funding/RSI/OI) + Alternative.me (Fear & Greed)
100% free. No API keys. No geo-blocking.
"""

import os
import time
import requests
import schedule
from datetime import datetime, timezone

TELEGRAM_TOKEN   = os.environ.get("TELEGRAM_TOKEN", "")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "")

OKX  = "https://www.okx.com/api/v5"
INST = "BTC-USDT-SWAP"


def validate_keys():
    missing = [k for k, v in {
        "TELEGRAM_TOKEN":   TELEGRAM_TOKEN,
        "TELEGRAM_CHAT_ID": TELEGRAM_CHAT_ID,
    }.items() if not v]
    if missing:
        print(f"[ERROR] Missing: {', '.join(missing)}")
        return False
    print("[✓] Keys loaded.")
    return True


# ─── DATA FETCHERS ───────────────────────────

def get_price():
    """
    BTC price from CoinGecko — free, no key, never geo-blocked.
    Fallback to OKX if CoinGecko fails.
    """
    try:
        r = requests.get(
            "https://api.coingecko.com/api/v3/simple/price",
            params={"ids": "bitcoin", "vs_currencies": "usd"},
            timeout=10
        )
        price = float(r.json()["bitcoin"]["usd"])
        print(f"[✓] Price (CoinGecko): ${price:,.2f}")
        return price
    except Exception as e:
        print(f"[WARN] CoinGecko failed: {e} — trying OKX...")

    # Fallback: OKX mark price
    try:
        r = requests.get(
            f"{OKX}/market/ticker",
            params={"instId": INST},
            timeout=10
        )
        price = float(r.json()["data"][0]["last"])
        print(f"[✓] Price (OKX fallback): ${price:,.2f}")
        return price
    except Exception as e:
        print(f"[WARN] OKX price also failed: {e}")
        return 0


def get_rsi():
    """
    RSI(14) from OKX 15m candles.
    <30 = oversold, >70 = overbought.
    """
    try:
        r = requests.get(
            f"{OKX}/market/candles",
            params={"instId": INST, "bar": "15m", "limit": "100"},
            timeout=10
        )
        # OKX returns newest first — index 4 = close price
        candles = list(reversed(r.json()["data"]))
        closes  = [float(c[4]) for c in candles]

        gains, losses = [], []
        for i in range(1, len(closes)):
            diff = closes[i] - closes[i - 1]
            (gains if diff > 0 else losses).append(abs(diff))

        avg_gain = sum(gains[-14:]) / 14 if gains else 0
        avg_loss = sum(losses[-14:]) / 14 if losses else 1
        rs  = avg_gain / avg_loss if avg_loss else 0
        rsi = round(100 - (100 / (1 + rs)), 2)
        print(f"[✓] RSI(14): {rsi}")
        return rsi
    except Exception as e:
        print(f"[WARN] RSI failed: {e}")
        return 50


def get_funding():
    """
    Current BTC perpetual funding rate from OKX.
    Positive = longs paying → short pressure.
    Negative = shorts paying → long pressure.
    """
    try:
        r = requests.get(
            f"{OKX}/public/funding-rate",
            params={"instId": INST},
            timeout=10
        )
        rate = float(r.json()["data"][0]["fundingRate"])
        print(f"[✓] Funding: {rate}")
        return rate
    except Exception as e:
        print(f"[WARN] Funding failed: {e}")
        return 0


def get_oi():
    """
    Open Interest change % over last hour from OKX.
    Rising OI = new positions opening.
    Falling OI = positions closing.
    """
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
            print(f"[✓] OI change: {change}%")
            return change
        print("[WARN] OI: not enough data")
        return 0
    except Exception as e:
        print(f"[WARN] OI failed: {e}")
        return 0


def get_ls_ratio():
    """
    Long/Short ratio from OKX taker volume.
    >1 = more buy volume (long pressure).
    <1 = more sell volume (short pressure).
    """
    try:
        r = requests.get(
            f"{OKX}/rubik/stat/contracts/long-short-account-ratio",
            params={"ccy": "BTC", "period": "1H"},
            timeout=10
        )
        data = r.json().get("data", [])
        if data:
            ratio = float(data[-1][1])
            long_pct  = round((ratio / (1 + ratio)) * 100, 1)
            short_pct = round(100 - long_pct, 1)
            print(f"[✓] L/S: {round(ratio,3)} ({long_pct}% long / {short_pct}% short)")
            return round(ratio, 3), long_pct, short_pct
        return 1, 50, 50
    except Exception as e:
        print(f"[WARN] L/S failed: {e}")
        return 1, 50, 50


def get_fear_greed():
    """
    Crypto Fear & Greed Index from alternative.me.
    0–25 = Extreme Fear (long opportunity)
    25–45 = Fear
    45–55 = Neutral
    55–75 = Greed
    75–100 = Extreme Greed (short opportunity)
    """
    try:
        r = requests.get(
            "https://api.alternative.me/fng/",
            params={"limit": 1},
            timeout=10
        )
        d     = r.json()["data"][0]
        value = int(d["value"])
        label = d["value_classification"]
        print(f"[✓] Fear & Greed: {value} ({label})")
        return value, label
    except Exception as e:
        print(f"[WARN] Fear & Greed failed: {e}")
        return 50, "Neutral"


# ─── SIGNAL ENGINE ────────────────────────────

def generate_signal(funding, ls, oi, rsi, fg):
    """
    Score each indicator for long or short.
    Needs score ≥ 2.5 to fire a trade signal.
    """
    reasons     = []
    long_score  = 0
    short_score = 0

    # Funding rate
    if funding < -0.0001:
        long_score += 1
        reasons.append(f"Funding {round(funding,5)} → shorts paying, long bias")
    elif funding > 0.0001:
        short_score += 1
        reasons.append(f"Funding {round(funding,5)} → longs paying, short bias")
    else:
        reasons.append(f"Funding {round(funding,5)} → neutral")

    # Long/Short ratio
    if ls < 0.9:
        long_score += 1
        reasons.append(f"L/S {ls} → short-heavy market, squeeze risk")
    elif ls > 1.1:
        short_score += 1
        reasons.append(f"L/S {ls} → long-heavy market, flush risk")
    else:
        reasons.append(f"L/S {ls} → balanced positioning")

    # Open Interest
    if oi > 0.1:
        long_score  += 0.5
        short_score += 0.5
        reasons.append(f"OI +{oi}% → new money entering, adds conviction")
    elif oi < -0.1:
        reasons.append(f"OI {oi}% → positions closing, reduce size")
    else:
        reasons.append(f"OI {oi}% → flat, no momentum")

    # RSI
    if rsi < 32:
        long_score += 1
        reasons.append(f"RSI {rsi} → oversold, bounce potential")
    elif rsi > 68:
        short_score += 1
        reasons.append(f"RSI {rsi} → overbought, rejection potential")
    else:
        reasons.append(f"RSI {rsi} → neutral zone")

    # Fear & Greed
    if fg <= 25:
        long_score += 1
        reasons.append(f"Fear & Greed {fg} → Extreme Fear, contrarian long")
    elif fg >= 75:
        short_score += 1
        reasons.append(f"Fear & Greed {fg} → Extreme Greed, contrarian short")
    else:
        reasons.append(f"Fear & Greed {fg} → no extreme reading")

    # Verdict
    if long_score >= 2.5:
        return "LONG",  min(10, int(long_score  * 2)), reasons
    elif short_score >= 2.5:
        return "SHORT", min(10, int(short_score * 2)), reasons
    return "NEUTRAL", 2, reasons


def calculate_levels(price, signal):
    if signal == "LONG":
        return round(price*0.985,2), round(price*1.02,2), round(price*1.035,2)
    if signal == "SHORT":
        return round(price*1.015,2), round(price*0.98,2), round(price*0.965,2)
    return None, None, None


# ─── TELEGRAM ─────────────────────────────────

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
        print(f"[ERROR] Telegram failed: {e}")


# ─── MAIN CYCLE ───────────────────────────────

def run():
    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    print(f"\n{'='*48}")
    print(f"  APEX v7 — {now}")
    print(f"{'='*48}")

    price              = get_price()
    rsi                = get_rsi()
    funding            = get_funding()
    ls, long_pct, short_pct = get_ls_ratio()
    oi                 = get_oi()
    fg, fg_label       = get_fear_greed()

    signal, conv, reasons = generate_signal(funding, ls, oi, rsi, fg)
    print(f"\n→ Signal: {signal} | Conviction: {conv}/10")

    reason_text = "\n".join([f"• {r}" for r in reasons])

    if signal == "NEUTRAL":
        msg = (
            f"⏸ *APEX — NO TRADE* `{now}`\n\n"
            f"No setup. Staying out.\n\n"
            f"*BTC:* ${price:,.2f}\n"
            f"*RSI:* {rsi}\n"
            f"*Funding:* {round(funding,5)}\n"
            f"*L/S:* {ls} ({long_pct}% / {short_pct}%)\n"
            f"*OI Δ:* {oi}%\n"
            f"*Fear & Greed:* {fg} — {fg_label}\n\n"
            f"*Read:*\n{reason_text}"
        )
    else:
        sl, tp1, tp2 = calculate_levels(price, signal)
        risk = "🔴 HIGH" if conv < 6 else "🟡 MEDIUM" if conv < 8 else "🟢 LOW"
        msg = (
            f"⚡ *APEX {signal}* `{now}`\n\n"
            f"*Entry:* ${price:,.2f}\n"
            f"*TP1:* ${tp1:,}\n"
            f"*TP2:* ${tp2:,}\n"
            f"*SL:* ${sl:,}\n\n"
            f"*Conviction:* {conv}/10\n"
            f"*Risk:* {risk}\n\n"
            f"*BTC:* ${price:,.2f} | *RSI:* {rsi}\n"
            f"*Funding:* {round(funding,5)} | *OI Δ:* {oi}%\n"
            f"*L/S:* {ls} ({long_pct}% / {short_pct}%)\n"
            f"*Fear & Greed:* {fg} — {fg_label}\n\n"
            f"*Signals:*\n{reason_text}"
        )

    print(msg)
    send_telegram(msg)


def main():
    print("\n" + "█"*48)
    print("  APEX BOT v7 — ZERO BLOCK — STARTING")
    print("█"*48 + "\n")

    if not validate_keys():
        return

    run()

    schedule.every(15).minutes.do(run)
    print("\n[✓] Scheduler active — every 15 min.\n")

    while True:
        schedule.run_pending()
        time.sleep(10)


if __name__ == "__main__":
    main()
    
