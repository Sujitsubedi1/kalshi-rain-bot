"""
Daily Telegram summary for the rain bot, once each day's positions have
fully settled (intended schedule: ~4:30 AM ET / 08:30 UTC, a safety
margin after the last timezone group -- Pacific cities -- settles around
3:30 AM ET; see rain_bot.py's settlement-timing notes).

Stateless by design, same principle as rain_bot.py's idempotency check:
Render's cron containers don't persist a local file between runs, so
every number here (today's result, cumulative P&L) is recomputed fresh
from Kalshi's own /portfolio/fills + settled-market results each time,
never from local state. A duplicate manual trigger just resends the same
correct numbers rather than double-counting anything.

Reuses the /portfolio/fills pagination + rounds-grouping pattern from
kalshi/strategy_alerts.py, with one deliberate improvement: fills carry
their own fee_cost field, so P&L here nets out real trading fees --
the existing crypto alerts report gross (count - cost), not net.
"""
import os
from collections import defaultdict

import requests

from kalshi_client import KalshiClient
from rain_bot import parse_event_date, VALIDATED_CITIES

TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")
SERIES_PREFIX = "KXRAIN-"


def send_telegram(message: str):
    print("TELEGRAM:", message)
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        raise RuntimeError("Missing TELEGRAM_BOT_TOKEN or TELEGRAM_CHAT_ID")
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    resp = requests.post(url, json={"chat_id": TELEGRAM_CHAT_ID, "text": message}, timeout=10)
    if resp.status_code != 200:
        print("Telegram send failed:", resp.status_code, resp.text)


def fetch_all_rain_rounds(c: KalshiClient) -> list:
    """Paginates the full /portfolio/fills history (no server-side ticker
    filter exists), keeps only KXRAIN fills, and groups them into rounds
    by order_id -- summing count, cost, AND fee per round."""
    all_fills = []
    cursor = ""
    for _ in range(200):
        params = {"limit": 1000}
        if cursor:
            params["cursor"] = cursor
        r = c.get("/portfolio/fills", params=params, auth=True)
        r.raise_for_status()
        data = r.json()
        page = data.get("fills", [])
        all_fills.extend(f for f in page if f["ticker"].startswith(SERIES_PREFIX))
        cursor = data.get("cursor") or ""
        if not cursor or not page:
            break

    rounds_by_order = {}
    for f in all_fills:
        city = f["ticker"].rsplit("-", 1)[-1]
        if city not in VALIDATED_CITIES:
            continue  # defensive: stay scoped to the backtested cities even
            # if rain_bot.py's own filtering ever changes
        cost = float(f["count_fp"]) * (
            float(f["yes_price_dollars"]) if f["side"] == "yes" else float(f["no_price_dollars"])
        )
        rnd = rounds_by_order.setdefault(f["order_id"], {
            "order_id": f["order_id"], "ticker": f["ticker"], "side": f["side"],
            "ts": f["ts"], "count": 0.0, "cost": 0.0, "fee": 0.0,
        })
        rnd["count"] += float(f["count_fp"])
        rnd["cost"] += cost
        rnd["fee"] += float(f.get("fee_cost", 0) or 0)
        rnd["ts"] = min(rnd["ts"], f["ts"])

    return sorted(rounds_by_order.values(), key=lambda r: r["ts"])


def fetch_all_rain_results(c: KalshiClient) -> dict:
    results = {}
    cursor = ""
    for _ in range(200):
        params = {"series_ticker": "KXRAIN", "status": "settled", "limit": 1000}
        if cursor:
            params["cursor"] = cursor
        r = c.get("/markets", params=params, auth=False)
        r.raise_for_status()
        data = r.json()
        page = data.get("markets", [])
        for m in page:
            results[m["ticker"]] = m.get("result", "")
        cursor = data.get("cursor") or ""
        if not cursor or not page:
            break
    return results


def net_pnl(rnd: dict, result: str) -> float:
    won = rnd["side"] == result
    return (rnd["count"] * 1.0 - rnd["cost"] - rnd["fee"]) if won else -(rnd["cost"] + rnd["fee"])


def main():
    c = KalshiClient.from_env()

    rounds = fetch_all_rain_rounds(c)
    results = fetch_all_rain_results(c)

    settled_rounds = [r for r in rounds if results.get(r["ticker"])]
    if not settled_rounds:
        print("No settled KXRAIN rounds yet -- nothing to report.")
        return

    # group settled rounds by event (the date-suffixed part of the ticker,
    # e.g. KXRAIN-26AUG27), report on the single latest fully-settled event
    by_event = defaultdict(list)
    for r in settled_rounds:
        event_ticker = r["ticker"].rsplit("-", 1)[0]
        by_event[event_ticker].append(r)

    latest_event = max(by_event.keys(), key=parse_event_date)
    todays_rounds = by_event[latest_event]

    total_staked = sum(r["cost"] + r["fee"] for r in todays_rounds)
    total_pnl = sum(net_pnl(r, results[r["ticker"]]) for r in todays_rounds)
    wins = sum(1 for r in todays_rounds if r["side"] == results[r["ticker"]])
    losses = len(todays_rounds) - wins

    cumulative_pnl = sum(net_pnl(r, results[r["ticker"]]) for r in settled_rounds)
    days_active = len({r["ticker"].rsplit("-", 1)[0] for r in settled_rounds})

    date_label = latest_event.replace(SERIES_PREFIX, "")
    message = (
        f"[Rain Bot] {date_label} settled: {len(todays_rounds)} positions, {wins}W-{losses}L\n"
        f"Staked ${total_staked:.2f} | Net {'+' if total_pnl >= 0 else ''}{total_pnl:.2f}\n"
        f"Cumulative since launch: {'+' if cumulative_pnl >= 0 else ''}{cumulative_pnl:.2f} ({days_active} days)"
    )
    send_telegram(message)


if __name__ == "__main__":
    main()
