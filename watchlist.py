"""
Shared watchlist logic for bid_sniper.py and webapp.py:

  * Favorite notes: each favorite's note is a small JSON object,
        {"max_bid": 9.0, "alerts": true}
    max_bid  - the snipe amount; missing, null or 0 means "don't bid"
    alerts   - whether to send time alerts for this item (default true)
    A favorite with no note, or a note that isn't a JSON object,
    is not tracked at all (no bid, no alerts).

  * Contest detection from an item's public bid history.
"""

import datetime
import json
from typing import Dict, Optional

import shopgoodwill

UNCONTESTED = "Uncontested"
QUIET = "Quiet"
ACTIVE = "Active"
CONTESTED = "Contested"


def parse_note(note: Optional[str]) -> Optional[Dict]:
    """
    Parse a favorite's note.

    :param note: The raw note string from SGW (may be None or empty)
    :return: {"max_bid": float or None, "alerts": bool},
        or None if the favorite is not tracked
    :rtype: Optional[Dict]
    """

    if not note:
        return None
    try:
        data = json.loads(note)
    except (ValueError, TypeError):
        return None
    if not isinstance(data, dict):
        return None

    max_bid = data.get("max_bid", None)
    try:
        max_bid = float(max_bid) if max_bid not in (None, "") else None
    except (TypeError, ValueError):
        max_bid = None
    if max_bid is not None and max_bid <= 0:
        max_bid = None

    return {"max_bid": max_bid, "alerts": bool(data.get("alerts", True))}


def make_note(max_bid: Optional[float], alerts: bool = True) -> str:
    """
    Build a favorite note string. Kept short: SGW notes max out at 256 chars.

    :rtype: str
    """

    data = {"max_bid": round(float(max_bid), 2) if max_bid else None, "alerts": alerts}
    return json.dumps(data, separators=(", ", ": "))


def contest_status(
    item_info: Dict,
    window_minutes: float = 30,
    min_bids: int = 3,
    min_bidders: int = 2,
    max_bid: Optional[float] = None,
) -> Dict:
    """
    Classify how hotly an item is being bid on, from its public bid log.

    Labels:
        Uncontested - no bids at all
        Quiet       - has bids, but none in the last window_minutes
        Active      - some bids in the window, below the contested threshold
        Contested   - at least min_bids bids from at least min_bidders
                      different bidders in the window

    Bids placed by the logged-in account are excluded from the counts.
    The window is measured against the server's clock (serverTime),
    the same clock that stamps the bids.

    :param item_info: Response from Shopgoodwill.get_item_info
    :param max_bid: If given, priced_out and pct_of_max are filled in
    :rtype: Dict
    """

    history = item_info.get("bidHistory") or {}
    bid_log = [b for b in (history.get("bidComplete") or []) if not b.get("retracted")]
    others = [b for b in bid_log if not b.get("isLoggedInBuyer")]

    server_now = shopgoodwill.Shopgoodwill.parse_timestamp_precise(item_info["serverTime"])
    cutoff = server_now - datetime.timedelta(minutes=window_minutes)
    recent = [
        b
        for b in others
        if b.get("bidTime")
        and shopgoodwill.Shopgoodwill.parse_timestamp_precise(b["bidTime"]) >= cutoff
    ]
    recent_bidders = {b.get("bidderName") for b in recent}

    total_bids = int(item_info.get("numberOfBids") or len(bid_log))
    if total_bids == 0:
        label = UNCONTESTED
    elif len(recent) >= min_bids and len(recent_bidders) >= min_bidders:
        label = CONTESTED
    elif recent:
        label = ACTIVE
    else:
        label = QUIET

    current_price = float(item_info.get("currentPrice") or 0)
    minimum_bid = item_info.get("minimumBid")
    minimum_bid = float(minimum_bid) if minimum_bid is not None else None

    priced_out = None
    pct_of_max = None
    if max_bid:
        priced_out = minimum_bid is not None and minimum_bid > max_bid
        pct_of_max = round(100 * current_price / max_bid)

    if label == UNCONTESTED:
        detail = "no bids yet"
    elif label == QUIET:
        detail = f"{total_bids} bid(s), none in the last {window_minutes:g} min"
    else:
        detail = (
            f"{len(recent)} bid(s) from {len(recent_bidders)} bidder(s) "
            f"in the last {window_minutes:g} min"
        )

    return {
        "label": label,
        "detail": detail,
        "total_bids": total_bids,
        "recent_bids": len(recent),
        "recent_bidders": len(recent_bidders),
        "current_price": current_price,
        "minimum_bid": minimum_bid,
        "priced_out": priced_out,
        "pct_of_max": pct_of_max,
    }


def describe(status: Dict, bid: Optional[float], max_bid: Optional[float] = None) -> str:
    """
    One-line human summary used in alerts, eg.
    "Contested: 4 bid(s) from 3 bidder(s) in the last 30 min | $7.99 (min $8.99), your max $9.00 (89%)"

    bid is the amount that will actually be bid (max plus any tie-breaker), and is
    what status was computed against. Pass max_bid when they differ, so the text
    shows both: "your bid $9.37 (max $9.00 + $0.37)".
    """

    if bid and max_bid and round(bid - max_bid, 2) > 0:
        label = f"your bid ${bid:.2f} (max ${max_bid:.2f} + ${bid - max_bid:.2f})"
    elif bid:
        label = f"your max ${bid:.2f}"
    else:
        label = None

    price = f"${status['current_price']:.2f}"
    if status["minimum_bid"] is not None:
        price += f" (min ${status['minimum_bid']:.2f})"
    if label:
        if status["priced_out"]:
            price += f", PRICED OUT of {label}"
        else:
            price += f", {label} ({status['pct_of_max']}%)"
    else:
        price += ", no max set (alert only)"
    return f"{status['label']}: {status['detail']} | {price}"
