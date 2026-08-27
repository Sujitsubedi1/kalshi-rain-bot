"""
Daily forward-test bot for Kalshi's KXRAIN markets: buys NO on every
validated city whose price sits in the 10c-90c range, $1 notional each
(STAKE_DOLLARS env var). This is the live forward-test of the edge found
in kalshi_weather_testing/ (backtest_calibration.py + the train/test
split): market overprices YES/rain in this price band, +3.6%/+3.7% net
edge on two independent 34-day halves of real 2026 data.

Only trades the 20 originally-backtested cities (VALIDATED_CITIES) --
newer tickers like Newark/Trenton have zero track record and are
deliberately skipped, not traded at reduced size.

Safety:
- DRY_RUN defaults to true. Must explicitly set DRY_RUN=false to place
  real orders.
- Idempotent: re-running the same day skips any ticker Kalshi's own order
  history already shows an order for (queried live, not from a local
  file -- safe even on Render's ephemeral cron containers, where a local
  log wouldn't persist between runs).
- Checks Exchange 0 balance before placing anything; aborts if the day's
  total planned spend would exceed what's available.
- trade_log.csv is still written locally for visibility when run
  manually, but it's a convenience record only, not the source of truth
  for duplicate-prevention.

Order mechanics (endpoint, IOC settings, price/count field format) copied
from the proven live pattern in kalshi/martingale_bot_1h.py -- same
account, same working order contract, different strategy and completely
separate log/state.

Intended to run once per day via a scheduled job (cron / Render).
"""
import csv
import os
import re
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path

from kalshi_client import KalshiClient

EVT_DATE_RE = re.compile(r"KXRAIN-(\d{2})([A-Z]{3})(\d{2})")


def parse_event_date(event_ticker: str) -> datetime:
    # NOTE: sorting event tickers as plain strings is wrong -- month
    # abbreviations aren't alphabetically ordered (e.g. "JAN" > "FEB"),
    # so an alphabetical sort silently picks the wrong event at most
    # month boundaries. Always parse and compare real dates.
    m = EVT_DATE_RE.search(event_ticker)
    y, mon, day = m.groups()
    return datetime.strptime(f"20{y}-{mon}-{day}", "%Y-%b-%d")

DIR = Path(__file__).resolve().parent
LOG_PATH = DIR / "trade_log.csv"

VALIDATED_CITIES = {
    "ATL", "AUS", "BOS", "CHI", "DAL", "DC", "DEN", "HOU", "LAX", "LV",
    "MIA", "MIN", "NOLA", "NYC", "OKC", "PHIL", "PHX", "SATX", "SEA", "SFO",
}

STAKE_DOLLARS = float(os.getenv("STAKE_DOLLARS", "1"))
MIN_PRICE = float(os.getenv("MIN_PRICE", "0.10"))
MAX_PRICE = float(os.getenv("MAX_PRICE", "0.90"))
DRY_RUN = os.getenv("DRY_RUN", "true").lower() != "false"
FILL_RETRY_SECONDS = float(os.getenv("FILL_RETRY_SECONDS", "25"))
FILL_RETRY_INTERVAL = float(os.getenv("FILL_RETRY_INTERVAL", "5"))


def already_traded(c: KalshiClient, event_ticker: str) -> set:
    """Authoritative, stateless duplicate check against Kalshi's own order
    history for this event -- NOT the local trade_log.csv. Render's cron
    jobs run in fresh, ephemeral containers with no persistent disk by
    default, so a local file can't be trusted to survive between daily
    runs (same reason the live crypto bot reconstructs state from
    /portfolio/fills in kalshi/martingale_bot_1h.py instead of a local
    log). This makes a duplicate cron fire, or a manual re-run, safe
    regardless of whether any local state survived."""
    r = c.get("/portfolio/orders", params={"event_ticker": event_ticker, "limit": 200}, auth=True)
    r.raise_for_status()
    orders = r.json().get("orders") or []
    return {o["ticker"] for o in orders}


def log_trade(row: dict):
    is_new = not LOG_PATH.exists()
    with open(LOG_PATH, "a", newline="") as f:
        w = csv.DictWriter(f, fieldnames=[
            "placed_at", "ticker", "city", "event_ticker", "yes_bid", "yes_ask",
            "fill_count", "notional", "fee", "attempts", "status", "dry_run",
        ])
        if is_new:
            w.writeheader()
        w.writerow(row)


def target_event_ticker(c: KalshiClient) -> str:
    """The furthest-out currently-open KXRAIN event. Kalshi opens each
    day's event at a fixed ~09:10 UTC the day before -- computing 'tomorrow'
    from wall-clock time is fragile (only correct if the bot happens to run
    after that daily open), so instead just ask the API what's actually
    open and pick the latest one. That's always the freshest tradeable day
    regardless of what time this runs."""
    r = c.get("/markets", params={"series_ticker": "KXRAIN", "status": "open", "limit": 200}, auth=False)
    r.raise_for_status()
    markets = r.json().get("markets") or []
    events = sorted({m["event_ticker"] for m in markets}, key=parse_event_date)
    if not events:
        raise RuntimeError("No open KXRAIN events found at all")
    return events[-1]


def fetch_quote(c: KalshiClient, ticker: str):
    r = c.get(f"/markets/{ticker}", auth=False)
    r.raise_for_status()
    m = r.json()["market"]
    return float(m["yes_ask_dollars"]), float(m["yes_bid_dollars"])


def place_no_with_retry(c: KalshiClient, ticker: str, target_notional: float) -> dict:
    """Places IOC 'buy NO' orders against target_notional dollars, re-quoting
    and retrying whatever remains unfilled for up to FILL_RETRY_SECONDS.
    A single IOC can under-fill if the opposing book doesn't have enough
    size sitting at the aggressive price -- seen for real on the first
    live run (ATL filled 1.00 of a ~1.56-contract target, LV filled 1.00
    of a ~7.14-contract target). Mirrors the proven retry pattern in
    kalshi/martingale_bot_1h.py's place_with_retry, adapted to track total
    dollars spent rather than a fixed contract count, since retries can
    fill at different prices as the quote moves between attempts."""
    remaining_notional = target_notional
    total_filled = 0.0
    total_cost = 0.0
    total_fee = 0.0
    deadline = time.time() + FILL_RETRY_SECONDS
    attempt = 0
    last_yes_bid = last_yes_ask = None

    while remaining_notional > 0.005:
        attempt += 1
        yes_ask, yes_bid = fetch_quote(c, ticker)
        last_yes_bid, last_yes_ask = yes_bid, yes_ask
        # Kalshi's "price" field is always YES-denominated, even for a NO-side
        # order -- confirmed against real fills (yes_price_dollars + no_price_dollars
        # always sum to 1.00) and against /portfolio/positions' market_exposure_dollars
        # (which matched count * (1 - submitted_price) exactly, not count * submitted_price).
        # The true cost per NO contract is (1 - this price), NOT this price itself --
        # dividing the target dollar amount by the raw yes-denominated price was the
        # bug that caused $9-10 fills on a $1 target (see 2026-08-27 incident: ~$110
        # actually spent against a ~$28 target, emergency-closed 17 positions).
        submitted_yes_price = max(round(yes_bid - 0.01, 2), 0.01)
        true_no_price = round(1 - submitted_yes_price, 2)
        count = round(remaining_notional / true_no_price, 2)
        if count <= 0:
            break

        order = {
            "ticker": ticker,
            "side": "ask",
            "count": f"{count:.2f}",
            "price": f"{submitted_yes_price:.2f}",
            "time_in_force": "immediate_or_cancel",
            "self_trade_prevention_type": "taker_at_cross",
            "client_order_id": str(uuid.uuid4()),
        }

        if DRY_RUN:
            print(f"{ticker}: DRY_RUN attempt {attempt}, would place {order}")
            return {
                "filled": count, "cost": round(count * true_no_price, 2), "fee": 0.0,
                "yes_bid": yes_bid, "yes_ask": yes_ask, "attempts": attempt, "status": "dry_run",
            }

        resp = c.post("/portfolio/events/orders", json=order, auth=True)
        print(f"{ticker}: attempt {attempt} order response {resp.status_code} {resp.text[:250]}")

        if resp.status_code not in (200, 201):
            break

        body = resp.json()
        fill_count = float(body.get("fill_count", "0"))
        # average_fill_price from the order response is also YES-denominated
        # (same convention as the submitted price above) -- true cost is (1 - this).
        fill_price_yes = float(body.get("average_fill_price", submitted_yes_price))
        fill_price_no = round(1 - fill_price_yes, 4)
        fee_paid = float(body.get("average_fee_paid", "0")) * fill_count

        total_filled += fill_count
        total_cost += fill_count * fill_price_no
        total_fee += fee_paid
        remaining_notional = target_notional - total_cost

        if remaining_notional <= 0.005:
            break
        if time.time() >= deadline:
            print(f"{ticker}: gave up after {attempt} attempts, filled ${total_cost:.2f}/${target_notional:.2f}")
            break
        time.sleep(FILL_RETRY_INTERVAL)

    status = "filled" if total_filled > 0 else "no_fill"
    return {
        "filled": round(total_filled, 2), "cost": round(total_cost, 2), "fee": round(total_fee, 2),
        "yes_bid": last_yes_bid, "yes_ask": last_yes_ask, "attempts": attempt, "status": status,
    }


def check_balance(c: KalshiClient, planned_spend: float):
    r = c.get("/portfolio/balance", auth=True)
    r.raise_for_status()
    breakdown = r.json().get("balance_breakdown", [])
    ex0 = next((b for b in breakdown if b.get("exchange_index") == 0), None)
    available = float(ex0["balance"]) if ex0 else 0.0
    print(f"Exchange 0 (Climate) balance: ${available:.2f}, planned spend: ${planned_spend:.2f}")
    if planned_spend > available:
        raise RuntimeError(f"Planned spend ${planned_spend:.2f} exceeds Exchange 0 balance ${available:.2f}")


def main():
    c = KalshiClient.from_env()

    if DRY_RUN:
        print("DRY_RUN=true -- no real orders will be placed. Set DRY_RUN=false to go live.")

    event_ticker = target_event_ticker(c)
    print(f"Target event: {event_ticker}")

    r = c.get("/markets", params={"event_ticker": event_ticker, "status": "open", "limit": 200}, auth=False)
    r.raise_for_status()
    markets = r.json().get("markets") or []
    if not markets:
        print(f"No open markets found for {event_ticker} yet -- nothing to do (try again later).")
        return

    done = already_traded(c, event_ticker)
    candidates = []
    skipped_untested = skipped_out_of_range = skipped_already_done = 0

    for m in markets:
        ticker = m["ticker"]
        city = ticker.rsplit("-", 1)[-1]

        if city not in VALIDATED_CITIES:
            skipped_untested += 1
            continue
        if ticker in done:
            skipped_already_done += 1
            continue

        yes_bid = float(m.get("yes_bid_dollars") or 0)
        yes_ask = float(m.get("yes_ask_dollars") or 0)
        mid = (yes_bid + yes_ask) / 2 if (yes_bid or yes_ask) else None

        if mid is None or not (MIN_PRICE <= mid <= MAX_PRICE):
            skipped_out_of_range += 1
            continue

        candidates.append((ticker, city, yes_bid, yes_ask))

    if not DRY_RUN and candidates:
        check_balance(c, STAKE_DOLLARS * len(candidates))

    placed = 0
    for ticker, city, yes_bid, yes_ask in candidates:
        result = place_no_with_retry(c, ticker, STAKE_DOLLARS)

        log_trade({
            "placed_at": datetime.now(timezone.utc).isoformat(),
            "ticker": ticker, "city": city, "event_ticker": event_ticker,
            "yes_bid": result["yes_bid"], "yes_ask": result["yes_ask"],
            "fill_count": result["filled"], "notional": result["cost"], "fee": result["fee"],
            "attempts": result["attempts"], "status": result["status"], "dry_run": DRY_RUN,
        })
        placed += 1

    print(f"\nDone. placed={placed}, skipped_untested_city={skipped_untested}, "
          f"skipped_out_of_range={skipped_out_of_range}, skipped_already_done={skipped_already_done}")


if __name__ == "__main__":
    main()
