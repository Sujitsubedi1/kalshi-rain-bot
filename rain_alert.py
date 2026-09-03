"""
Daily Telegram summary for the rain bot, once each day's positions have
fully settled. Intended schedule: 15:30 UTC. Originally set to 08:30 UTC
on the wrong assumption that settlement follows ~30min after each
market's own close_time -- real observed behavior (2026-08-28) is that
Kalshi settles in BATCHES roughly 7.5h after each timezone group's close,
not per-market: Eastern cities settled at 11:30:55 UTC, Central at
12:30:55 UTC (both to the exact second -- clearly a fixed batch job, not
a variable delay), Pacific cities not until ~14:30 UTC. 15:30 UTC leaves
a safety margin after the slowest (Pacific) group's batch.

Stateless by design, same principle as rain_bot.py's idempotency check:
Render's cron containers don't persist a local file between runs, so
every number here (today's result, cumulative P&L) is recomputed fresh
from Kalshi's own /portfolio/fills + settled-market results each time,
never from local state. A duplicate manual trigger just resends the same
correct numbers rather than double-counting anything.

Two bugs fixed 2026-08-28, both found by real Aug 27 settlement data
(the first time any KXRAIN market actually settled since launch):
1. Only reports on an event once ALL of our held tickers in it have a
   result -- Kalshi's staggered batch settlement means a day's cities can
   finish hours apart, so checking only "is there at least one settled
   round" could report a partial, understated day.
2. Drops any ticker we manually closed early (see
   drop_manually_closed_tickers) instead of scoring it against the
   eventual market result, which double-counted and misattributed the
   2026-08-27 emergency cash-out as both a natural win/loss AND a
   separate closing trade.

Reuses the /portfolio/fills pagination + rounds-grouping pattern from
kalshi/strategy_alerts.py, with one deliberate improvement: fills carry
their own fee_cost field, so P&L here nets out real trading fees --
the existing crypto alerts report gross (count - cost), not net.
"""
import os
from collections import defaultdict
from datetime import datetime

import requests

from kalshi_client import KalshiClient
from rain_bot import parse_event_date, VALIDATED_CITIES

TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")
SERIES_PREFIX = "KXRAIN-"

# Risk-management checkpoint (2026-09-03 decision, see project memory):
# +$21 cumulative by day 14 to justify doubling STAKE_DOLLARS; cumulative
# at or beyond STOP_LOSS_DOLLARS is a stop-loss flag -- deliberately NOT
# an auto-pause. User's explicit call: flag it in the alert and let them
# decide manually (via DRY_RUN=true on Render) whether to actually stop,
# rather than have the bot silently stop itself. Tightened from -$30 to
# -$20 on 2026-09-03 alongside the clean-slate reset below -- after two
# real bugs found in two weeks, more caution on the second real test.
STOP_LOSS_DOLLARS = float(os.getenv("STOP_LOSS_DOLLARS", "-20"))

# Clean-slate reset (2026-09-03): everything before this date was traded
# under either the sizing bug (fixed 2026-08-27) or the buy-timing bug
# (fixed 2026-09-03, see rain_bot.py's TIMING docstring) -- neither is a
# fair test of the actual strategy, so neither counts toward the scale-up/
# stop-loss tracking. KXRAIN-26SEP04 was still bought under the OLD
# 10:30 UTC schedule (fired before the timing fix deployed); the first
# event bought entirely by the new per-timezone-group schedule is
# KXRAIN-26SEP05, so that's the real day-1 of clean tracking.
CLEAN_START_DATE = datetime(2026, 9, 5)


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


def drop_manually_closed_tickers(rounds: list) -> list:
    """A ticker we manually closed early (e.g. the 2026-08-27 emergency
    cash-out) has TWO rounds: the original 'no' buy and a 'yes' buy-back
    to flatten it. Scoring either against the eventual market result is
    wrong -- we didn't hold a position by settlement time, so there's no
    real win/loss to attribute to the market outcome. Drop every round for
    any ticker that shows both sides, rather than try to reconstruct a
    P&L number from the two legs; the real number for those (-$18.10) is
    already known and tracked separately, by deliberate choice, not
    recomputed here."""
    sides_by_ticker = defaultdict(set)
    for r in rounds:
        sides_by_ticker[r["ticker"]].add(r["side"])
    closed_tickers = {t for t, sides in sides_by_ticker.items() if len(sides) > 1}
    if closed_tickers:
        print(f"Excluding {len(closed_tickers)} manually-closed tickers from scoring: {sorted(closed_tickers)}")
    return [r for r in rounds if r["ticker"] not in closed_tickers]


def merge_rounds_by_ticker(rounds: list) -> list:
    """place_no_with_retry can split one real bet across multiple orders
    (a main fill plus a small top-up retry for the last fraction of a
    cent -- confirmed for real on 2026-08-29: every one of 20 city-bets
    had a ~99%-sized round plus a ~1c top-up round, both sharing the same
    ticker but different order_ids). Counting rounds directly reports
    '40 positions' for what was actually 20 real bets, and double-counts
    the win/loss tally (though the dollar totals were still correct,
    since summing is order-independent). Merge same-ticker rounds into
    one before computing position counts / W-L, so the count reflects
    real bets, not order-attempt fragments."""
    by_ticker = {}
    for r in rounds:
        m = by_ticker.setdefault(r["ticker"], {
            "ticker": r["ticker"], "side": r["side"], "count": 0.0, "cost": 0.0, "fee": 0.0,
        })
        m["count"] += r["count"]
        m["cost"] += r["cost"]
        m["fee"] += r["fee"]
    return list(by_ticker.values())


def drop_pre_cutoff_rounds(rounds: list) -> list:
    """Excludes any round from an event before CLEAN_START_DATE -- see the
    constant's docstring. Applied once, up front, so every downstream
    computation (today's report, cumulative, days_active) is automatically
    scoped to the clean era without needing separate filtering."""
    kept = [r for r in rounds if parse_event_date(r["ticker"].rsplit("-", 1)[0]) >= CLEAN_START_DATE]
    dropped = len(rounds) - len(kept)
    if dropped:
        print(f"Excluding {dropped} pre-{CLEAN_START_DATE.date()} rounds (sizing/timing bug era, tracked separately)")
    return kept


def main():
    c = KalshiClient.from_env()

    rounds = fetch_all_rain_rounds(c)
    rounds = drop_manually_closed_tickers(rounds)
    rounds = merge_rounds_by_ticker(rounds)
    rounds = drop_pre_cutoff_rounds(rounds)
    results = fetch_all_rain_results(c)

    # group ALL our rounds (settled or not) by event, so we can tell
    # whether an event is fully done vs. only partially settled so far --
    # reporting on a partial day (some cities settled, others still
    # pending) would understate that day's real result. Kalshi settles in
    # batches by timezone group, roughly 7.5h after each group's close, so
    # a day's cities can finish hours apart (confirmed 2026-08-28: Eastern
    # settled ~11:30 UTC, Pacific not until ~14:30 UTC for the same event).
    by_event = defaultdict(list)
    for r in rounds:
        event_ticker = r["ticker"].rsplit("-", 1)[0]
        by_event[event_ticker].append(r)

    fully_settled_events = [
        evt for evt, evt_rounds in by_event.items()
        if all(results.get(r["ticker"]) for r in evt_rounds)
    ]
    if not fully_settled_events:
        print("No fully-settled KXRAIN event yet -- nothing to report.")
        return

    latest_event = max(fully_settled_events, key=parse_event_date)
    todays_rounds = by_event[latest_event]

    total_staked = sum(r["cost"] + r["fee"] for r in todays_rounds)
    total_pnl = sum(net_pnl(r, results[r["ticker"]]) for r in todays_rounds)
    wins = sum(1 for r in todays_rounds if r["side"] == results[r["ticker"]])
    losses = len(todays_rounds) - wins

    # cumulative must only include rounds from FULLY settled events, same
    # gate as "todays_rounds"/"days_active" -- summing over settled_rounds
    # (any ticker with a result, even from a still-partial day) silently
    # mixed in a future day's partial results and mismatched the "(N days)"
    # label with a dollar figure that actually reflected N+ days worth.
    cumulative_rounds = [r for evt in fully_settled_events for r in by_event[evt]]
    cumulative_pnl = sum(net_pnl(r, results[r["ticker"]]) for r in cumulative_rounds)
    days_active = len(fully_settled_events)

    date_label = latest_event.replace(SERIES_PREFIX, "")
    message = (
        f"[Rain Bot] {date_label} settled: {len(todays_rounds)} positions, {wins}W-{losses}L\n"
        f"Staked ${total_staked:.2f} | Net {'+' if total_pnl >= 0 else ''}{total_pnl:.2f}\n"
        f"Cumulative since launch: {'+' if cumulative_pnl >= 0 else ''}{cumulative_pnl:.2f} ({days_active} days)"
    )
    if cumulative_pnl <= STOP_LOSS_DOLLARS:
        message += (
            f"\n\nSTOP-LOSS THRESHOLD HIT: cumulative ${cumulative_pnl:.2f} is at or "
            f"beyond the ${STOP_LOSS_DOLLARS:.2f} stop-loss. Bot is still running -- "
            f"this is a flag only, not an auto-pause. Set DRY_RUN=true on the "
            f"kalshi-rain-bot Render service if you want to stop it."
        )
    send_telegram(message)


if __name__ == "__main__":
    main()
