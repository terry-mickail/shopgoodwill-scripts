"""
Client for the BidSlammer API (https://bidslammer.com/help/api-documentation),
used to snipe eBay auctions. BidSlammer places the bid from its own servers,
so an eBay snipe does not depend on this PC being on.

Behaviour confirmed against live responses on 2026-11-10 (probe_bidslammer.py,
probe_write.py), where the published docs were silent:

  * Every call is a GET with the API key as a query parameter, and every
    response is HTTP 200 - including errors. Success or failure is the body's
    "status" field: "OK" or "NOK" (with a readable "message" for most errors,
    eg. "Invalid API key." or "Bid at least 148.").
  * getUserItems returns a bare JSON list (empty when there is nothing), and
    its numbers are strings ("bid": "13.00").
  * checkSnipeStatus and getAllBidders nest their status ("item_info.status",
    "status_data.status"); NOK there means "no snipe / no data", not a failure.
  * fetchItem works for any eBay item, not only ones with a snipe.
  * Time remaining comes as "overtime" (negative seconds before the end) or
    "seconds_left", so no time-zone parsing is needed for countdowns.
  * Nothing in any response reflects addBid's `enabled` flag, so it is never
    relied on: this app only ever adds live snipes.
"""

import datetime
import time
from typing import Any, Dict, List, Optional

import requests

API_ROOT = "https://api.bidslammer.com/"
DEFAULT_TIMEOUT = 20
PAGE_SIZE = 20  # getUserItems' documented maximum per call

# eBay's own lead-time range in BidSlammer (premium accounts may go down to 1)
MIN_DELAY, MAX_DELAY = 1, 15


class BidSlammerError(Exception):
    """BidSlammer answered with status NOK, or with something unexpected"""

    def __init__(self, message: str, response: Any = None) -> None:
        super().__init__(message)
        self.response = response


def money(value: Any) -> Optional[float]:
    """BidSlammer sends numbers as numbers or as strings; None for blanks"""
    if value in (None, ""):
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


class BidSlammer:
    def __init__(self, api_key: str, session: Optional[requests.Session] = None) -> None:
        if not api_key:
            raise ValueError("A BidSlammer API key is required")
        self.api_key = api_key
        self.session = session or requests.Session()
        self.session.headers.setdefault("User-Agent", "terry-sniper/1.0 (+BidSlammer API)")

    # ---------- transport ----------

    def _get(self, method: str, **params) -> Any:
        params = {k: v for k, v in params.items() if v is not None}
        res = self.session.get(
            API_ROOT + method, params={"key": self.api_key, **params}, timeout=DEFAULT_TIMEOUT
        )
        res.raise_for_status()
        try:
            return res.json()
        except ValueError:
            raise BidSlammerError(
                f"{method}: response was not JSON ({res.text[:200]!r})", res.text
            )

    @staticmethod
    def _check(method: str, data: Any) -> Dict:
        """Raise unless a dict response says status OK"""
        if not isinstance(data, dict):
            raise BidSlammerError(f"{method}: unexpected response {str(data)[:200]}", data)
        if data.get("status") != "OK":
            message = data.get("message") or data.get("short_message") or "request refused"
            raise BidSlammerError(f"{method}: {message}", data)
        return data

    # ---------- calls ----------

    def test(self) -> Dict:
        """Checks the key. Raises BidSlammerError("... Invalid API key.") if it is wrong."""
        return self._check("justTesting", self._get("justTesting", arg1="ping"))

    def fetch_item(self, item_no: int) -> Dict:
        """Live details for any eBay item (price, minimumToBid, endTimeStamp, ...)"""
        return self._check("fetchItem", self._get("fetchItem", item_no=item_no))

    def add_snipe(self, item_no: int, bid: float, delay: int) -> Dict:
        """
        Add (or replace) a live snipe. bid is the exact amount BidSlammer will
        place; delay is seconds before the end. Raises with BidSlammer's own
        message on refusal, eg. "Bid at least 148."
        """

        if not (MIN_DELAY <= int(delay) <= MAX_DELAY):
            raise ValueError(f"delay must be {MIN_DELAY} to {MAX_DELAY} seconds")
        data = self._check(
            "addBid",
            self._get("addBid", item_no=item_no, bid=f"{float(bid):.2f}", delay=int(delay), force=0),
        )
        placed = money(data.get("bid"))
        if placed is None or abs(placed - round(float(bid), 2)) >= 0.005:
            raise BidSlammerError(
                f"addBid: BidSlammer recorded {data.get('bid')!r}, not {float(bid):.2f}", data
            )
        return data

    def delete_snipe(self, item_no: int) -> Dict:
        """Remove a snipe. Raises if there was no snipe on that item."""
        return self._check("deleteBid", self._get("deleteBid", item_no=item_no))

    def list_snipes(self, flt: str = "current") -> List[Dict]:
        """
        Every snipe in one list (current, watched, completed, archive),
        paging past the 20-per-call cap
        """

        out: List[Dict] = []
        offset = 0
        while True:
            page = self._get("getUserItems", filter=flt, limit=PAGE_SIZE, offset=offset)
            if isinstance(page, dict):
                self._check("getUserItems", page)  # an error object, eg. a bad key
                page = []
            if not isinstance(page, list):
                raise BidSlammerError(f"getUserItems: unexpected response {str(page)[:200]}", page)
            out.extend(page)
            if len(page) < PAGE_SIZE:
                return out
            offset += PAGE_SIZE

    def snipe_status(self, item_no: int) -> Optional[Dict]:
        """
        {"result": bidResult (empty until BidSlammer has a verdict), "snipe": item_info},
        or None when there is no snipe on that item
        """

        data = self._get("checkSnipeStatus", item_no=item_no)
        if not isinstance(data, dict):
            raise BidSlammerError(f"checkSnipeStatus: unexpected response {str(data)[:200]}", data)
        info = data.get("item_info") or {}
        if info.get("status") != "OK":
            return None
        return {"result": data.get("bidResult") or {}, "snipe": info}

    def bidders(self, item_no: int) -> List[Dict]:
        """An item's bid log (userID masked, maxBid, bidTime); [] when there is none"""
        data = self._get("getAllBidders", item_no=item_no)
        if not isinstance(data, dict):
            return []
        return [b for b in (data.get("bidders") or []) if isinstance(b, dict)]


# ---------- helpers shared with the web UI ----------

def end_utc_from_overtime(overtime: Any, now: Optional[float] = None) -> Optional[datetime.datetime]:
    """overtime = seconds since the end (negative before it), measured when fetched"""
    seconds = money(overtime)
    if seconds is None:
        return None
    base = time.time() if now is None else now
    return datetime.datetime.fromtimestamp(base - seconds, tz=datetime.timezone.utc)


def is_auction(item: Dict) -> bool:
    """eBay's API names a standard auction listing "Chinese"; anything else is Buy It Now etc."""
    return item.get("listingType") == "Chinese"


def snipe_won(status: Optional[Dict]) -> Optional[bool]:
    """
    True if won, False if lost, None if no verdict yet. BidSlammer's docs say a
    win is only certain when bidResult.success == 1 (and to wait ~5 minutes
    after the close before asking).
    """

    if not status:
        return None
    result = status.get("result")
    if not isinstance(result, dict) or "success" not in result:
        return None
    try:
        return int(result["success"]) == 1
    except (TypeError, ValueError):
        return None
