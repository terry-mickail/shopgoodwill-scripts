"""
The eBay side of the sniper, via BidSlammer (see bidslammer.py).

BidSlammer holds the snipe and places the bid from its own servers. This module:
  * adds, changes and cancels snipes (always after an explicit confirmation)
  * keeps the parts BidSlammer does not store (your max, alerts on/off) in
    ebay_snipes.json next to config.json
  * builds watchlist rows with the same contest labels as ShopGoodwill
  * runs a watcher thread for time alerts and win/loss results

DRY RUN DOES NOT APPLY: once sent, a snipe is live on BidSlammer, so in dry-run
mode this module refuses to send anything rather than pretend.
"""

import datetime
import json
import logging
import os
import re
import threading
import time
from typing import Callable, Dict, List, Optional, Tuple
from zoneinfo import ZoneInfo

import bidslammer
import watchlist

PACIFIC = ZoneInfo("America/Los_Angeles")

# eBay URLs (www.ebay.com/itm/<id> or /itm/<slug>/<id>), or a bare 12-digit number
EBAY_URL = re.compile(r"ebay\.[a-z.]+/itm/(?:[^/?#]+/)?(\d{9,15})", re.I)
EBAY_BARE = re.compile(r"^\s*(\d{12})\s*$")

LIST_TTL = 60          # seconds; your snipe list
ITEM_TTL_NORMAL = 300  # item details and bid log
ITEM_TTL_ENDING = 30   # in an item's last 10 minutes
WATCH_INTERVAL = 30    # watcher loop
COMPLETED_INTERVAL = 300


def parse_ebay_item(text: str) -> Optional[int]:
    """The eBay item number in a URL or a bare 12-digit number, else None"""
    text = text or ""
    m = EBAY_URL.search(text) or EBAY_BARE.match(text)
    return int(m.group(1)) if m else None


class EbayStore:
    """Your max and alert choice per eBay item, plus results already reported"""

    def __init__(self, path: str) -> None:
        self.path = path
        self.lock = threading.Lock()
        self.data = {"snipes": {}, "reported": [], "initialized": False}
        if os.path.exists(path):
            with open(path, "r", encoding="utf-8") as f:
                self.data.update(json.load(f))

    def _save(self) -> None:
        tmp = self.path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(self.data, f, indent=2)
        os.replace(tmp, self.path)

    def get(self, item_no: int) -> Dict:
        with self.lock:
            return dict(self.data["snipes"].get(str(item_no), {}))

    def put(self, item_no: int, **fields) -> None:
        with self.lock:
            entry = self.data["snipes"].setdefault(str(item_no), {})
            entry.update(fields)
            self._save()

    def remove(self, item_no: int) -> None:
        with self.lock:
            self.data["snipes"].pop(str(item_no), None)
            self._save()

    def reported(self, key: str) -> bool:
        with self.lock:
            return key in self.data["reported"]

    def mark_reported(self, keys: List[str], initialized: bool = True) -> None:
        with self.lock:
            for k in keys:
                if k not in self.data["reported"]:
                    self.data["reported"].append(k)
            self.data["reported"] = self.data["reported"][-500:]
            self.data["initialized"] = initialized or self.data.get("initialized", False)
            self._save()


def pacific_str(dt: datetime.datetime) -> str:
    """A UTC datetime as the naive Pacific text the contest code parses"""
    return dt.astimezone(PACIFIC).strftime("%Y-%m-%dT%H:%M:%S")


def contest_input(item: Dict, bidders: List[Dict], now: Optional[datetime.datetime] = None) -> Dict:
    """
    Reshape a BidSlammer item + bid log into the item shape watchlist.contest_status
    reads, so eBay rows get the same Uncontested / Quiet / Active / Contested labels.

    getAllBidders' bidTime carries no time zone. fetchItem's endTime for the same
    listings is labelled PST/PDT, so bid times are read as Pacific too.
    """

    now = now or datetime.datetime.now(datetime.timezone.utc)
    log = []
    for b in bidders:
        bt = str(b.get("bidTime") or "").replace(" ", "T")
        if bt:
            log.append({
                "bidTime": bt,
                "bidderName": b.get("userID"),
                "isLoggedInBuyer": False,  # our eBay bids are last-second snipes, never in this log early
                "retracted": False,
            })
    return {
        "serverTime": pacific_str(now),
        "numberOfBids": item.get("bidCount", item.get("bids", len(log))),
        "currentPrice": item.get("currentPrice", item.get("cost")),
        "minimumBid": item.get("minimumToBid"),
        "bidHistory": {"bidComplete": log},
    }


class EbayDesk:
    def __init__(
        self,
        client: bidslammer.BidSlammer,
        store: EbayStore,
        logger: logging.Logger,
        notify_extra: Callable[..., Dict],
        tie_breaker: Callable[[], float],
        lead_seconds: Callable[[], float],
        is_dry_run: Callable[[], bool],
        contest_settings: Callable[[], Dict],
        alert_deltas: Callable[[], List[datetime.timedelta]],
    ) -> None:
        self.client = client
        self.store = store
        self.logger = logger
        self.notify_extra = notify_extra
        self.tie_breaker = tie_breaker
        self.lead_seconds = lead_seconds
        self.is_dry_run = is_dry_run
        self.contest_settings = contest_settings
        self.alert_deltas = alert_deltas
        self.lock = threading.Lock()
        self._list_cache: Tuple[float, List[Dict]] = (0.0, [])
        self._item_cache: Dict[int, Tuple[float, Dict, List[Dict]]] = {}
        self._sent_alerts: set = set()
        self._stop = threading.Event()
        self._last_completed = 0.0

    # ---------- helpers ----------

    @staticmethod
    def url(item_no: int) -> str:
        return f"https://www.ebay.com/itm/{item_no}"

    def delay(self) -> int:
        lead = int(round(float(self.lead_seconds())))
        return max(bidslammer.MIN_DELAY, min(bidslammer.MAX_DELAY, lead))

    def bid_for(self, max_bid: float) -> float:
        return round(float(max_bid) + self.tie_breaker(), 2)

    def invalidate(self, item_no: Optional[int] = None) -> None:
        with self.lock:
            self._list_cache = (0.0, [])
            if item_no is not None:
                self._item_cache.pop(int(item_no), None)

    def snipes(self) -> List[Dict]:
        with self.lock:
            fetched, items = self._list_cache
            if time.time() - fetched < LIST_TTL:
                return items
        items = self.client.list_snipes("current")
        now = time.time()
        for it in items:
            it["_fetched_at"] = now
        with self.lock:
            self._list_cache = (now, items)
        return items

    def details(self, item_no: int, end: Optional[datetime.datetime]) -> Tuple[Dict, List[Dict]]:
        ttl = ITEM_TTL_NORMAL
        if end and end - datetime.datetime.now(datetime.timezone.utc) < datetime.timedelta(minutes=10):
            ttl = ITEM_TTL_ENDING
        with self.lock:
            hit = self._item_cache.get(int(item_no))
            if hit and time.time() - hit[0] < ttl:
                return hit[1], hit[2]
        item = self.client.fetch_item(item_no)
        bids = self.client.bidders(item_no)
        with self.lock:
            self._item_cache[int(item_no)] = (time.time(), item, bids)
        return item, bids

    # ---------- actions (each needs confirm=True to send) ----------

    def preview(self, item_no: int, max_bid: float) -> Dict:
        """Everything the confirmation shows, and the checks that must pass first"""
        if self.is_dry_run():
            raise ValueError(
                "eBay snipes go live on BidSlammer the moment they're sent, so they can't be "
                "dry-run. Switch to LIVE to add or change one."
            )
        if not max_bid or max_bid <= 0:
            raise ValueError("eBay snipes need a max bid")
        item = self.client.fetch_item(item_no)
        if not bidslammer.is_auction(item):
            raise ValueError("That eBay listing isn't an auction, so there's nothing to snipe")
        seconds_left = float(item.get("endTimeStamp") or 0) - time.time()
        if seconds_left <= 60:
            raise ValueError("That auction ends within a minute or has ended")
        bid = self.bid_for(max_bid)
        minimum = bidslammer.money(item.get("minimumToBid"))
        if minimum is not None and bid < minimum:
            raise ValueError(
                f"Your bid ${bid:.2f} is below eBay's current minimum ${minimum:.2f}"
            )
        return {
            "item_no": int(item_no),
            "title": item.get("title") or str(item_no),
            "max_bid": round(float(max_bid), 2),
            "bid": bid,
            "tie_breaker": round(bid - round(float(max_bid), 2), 2),
            "current_price": bidslammer.money(item.get("currentPrice")),
            "minimum": minimum,
            "delay": self.delay(),
            "ends_in_seconds": int(seconds_left),
        }

    def place(self, item_no: int, max_bid: float, alerts: bool = True) -> Dict:
        """Send (or replace) the snipe after preview's checks; verifies what BidSlammer stored"""
        p = self.preview(item_no, max_bid)
        self.client.add_snipe(item_no, p["bid"], delay=p["delay"])
        status = self.client.snipe_status(item_no)
        stored = bidslammer.money((status or {}).get("snipe", {}).get("bid"))
        if stored is None or abs(stored - p["bid"]) >= 0.005:
            raise bidslammer.BidSlammerError(
                f"BidSlammer accepted the snipe but now shows {stored!r}, not {p['bid']:.2f}. "
                "Check it on the BidSlammer dashboard."
            )
        self.store.put(item_no, max_bid=p["max_bid"], alerts=bool(alerts), title=p["title"])
        self.invalidate(item_no)
        extra = f" (max {p['max_bid']:.2f} + tie-breaker {p['tie_breaker']:.2f})" if p["tie_breaker"] else ""
        self.logger.warning(
            f"eBay snipe set on BidSlammer: '{p['title']}' bids {p['bid']:.2f}{extra}, "
            f"{p['delay']}s before the end",
            extra=self.notify_extra("eBay snipe set", item_no, priority=3, url=self.url(item_no)),
        )
        return p

    def set_alerts(self, item_no: int, alerts: bool) -> None:
        self.store.put(item_no, alerts=bool(alerts))
        self.invalidate(item_no)

    def cancel(self, item_no: int) -> None:
        title = self.store.get(item_no).get("title") or str(item_no)
        self.client.delete_snipe(item_no)
        self.store.remove(item_no)
        self.invalidate(item_no)
        self.logger.warning(f"eBay snipe cancelled on BidSlammer: '{title}'")

    # ---------- rows for the page ----------

    def rows(self) -> List[Dict]:
        rows = []
        for s in self.snipes():
            item_no = int(s["item_no"])
            mine = self.store.get(item_no)
            bid = bidslammer.money(s.get("bid"))
            end = bidslammer.end_utc_from_overtime(s.get("overtime"), now=s.get("_fetched_at"))
            row = {
                "id": item_no,
                "site": "ebay",
                "title": s.get("title") or str(item_no),
                "url": s.get("item_url") or self.url(item_no),
                "end_utc": end.isoformat() if end else None,
                "tracked": True,
                "max_bid": mine.get("max_bid", bid),
                "bid_amount": bid,
                "alerts": mine.get("alerts", True),
                "current_price": bidslammer.money(s.get("cost")),
            }
            try:
                item, bids = self.details(item_no, end)
                status = watchlist.contest_status(
                    contest_input(item, bids), max_bid=bid, **self.contest_settings()
                )
                row.update(status)
                if item.get("endTimeStamp"):
                    row["end_utc"] = datetime.datetime.fromtimestamp(
                        float(item["endTimeStamp"]), tz=datetime.timezone.utc
                    ).isoformat()
            except Exception as e:
                row["error"] = f"{type(e).__name__}: {e}"
            rows.append(row)
        return rows

    # ---------- watcher: time alerts and results ----------

    def start_watcher(self) -> threading.Thread:
        t = threading.Thread(target=self._watch_loop, name="ebay-watcher", daemon=True)
        t.start()
        return t

    def stop(self) -> None:
        self._stop.set()

    def _watch_loop(self) -> None:
        while not self._stop.is_set():
            try:
                self.watch_once()
            except Exception as e:
                self.logger.warning(f"eBay watcher: {type(e).__name__}: {e}")
            self._stop.wait(WATCH_INTERVAL)

    def watch_once(self, now: Optional[datetime.datetime] = None) -> None:
        now = now or datetime.datetime.now(datetime.timezone.utc)
        self._time_alerts(now)
        if time.time() - self._last_completed >= COMPLETED_INTERVAL:
            self._last_completed = time.time()
            self._results()

    def _time_alerts(self, now: datetime.datetime) -> None:
        for s in self.snipes():
            item_no = int(s["item_no"])
            mine = self.store.get(item_no)
            if not mine.get("alerts", True):
                continue
            end = bidslammer.end_utc_from_overtime(s.get("overtime"), now=s.get("_fetched_at"))
            if not end:
                continue
            left = end - now
            for delta in self.alert_deltas():
                key = (item_no, int(delta.total_seconds()))
                # fire once, within one watcher interval after the alert time
                if key in self._sent_alerts:
                    continue
                if datetime.timedelta(0) < left <= delta and delta - left <= datetime.timedelta(seconds=WATCH_INTERVAL * 2):
                    self._sent_alerts.add(key)
                    bid = bidslammer.money(s.get("bid"))
                    message = f"Time alert (eBay) - {s.get('title')} ending in {str(left).split('.')[0]}"
                    try:
                        item, bids = self.details(item_no, end)
                        status = watchlist.contest_status(
                            contest_input(item, bids, now), max_bid=bid, **self.contest_settings()
                        )
                        message += " | " + watchlist.describe(status, bid, max_bid=mine.get("max_bid", bid))
                    except Exception as e:
                        self.logger.debug(f"eBay alert details: {e}")
                    self.logger.warning(
                        message,
                        extra=self.notify_extra(
                            f"eBay ending in {str(left).split('.')[0]}", item_no, priority=3, url=self.url(item_no)
                        ),
                    )
                elif left <= delta:
                    # already past this alert time when we first saw it: don't send it late
                    self._sent_alerts.add(key)

    def _results(self) -> None:
        """
        Report each finished snipe once. The list's own "success" field reads "0"
        while a snipe is still pending (seen live), so it cannot tell a loss from
        "no verdict yet". As BidSlammer's docs advise, wait 5 minutes after the
        close and ask checkSnipeStatus: success == 1 is a win, any other verdict
        is a loss with BidSlammer's reason.
        """

        completed = self.client.list_snipes("completed")
        keys = [f"{c.get('item_no')}:{c.get('snipe_id') or c.get('id')}" for c in completed]
        if not self.store.data.get("initialized"):
            # first run: don't announce old results
            self.store.mark_reported(keys)
            return
        for c, key in zip(completed, keys):
            if self.store.reported(key):
                continue
            since_end = bidslammer.money(c.get("overtime"))
            if since_end is not None and since_end < 300:
                continue  # too soon to trust a verdict
            item_no = int(c["item_no"])
            status = self.client.snipe_status(item_no)
            won = bidslammer.snipe_won(status)
            if won is None:
                continue  # no verdict yet; try again next pass
            result = status["result"]
            title = c.get("title") or str(item_no)
            price = bidslammer.money(c.get("cost"))
            bid = bidslammer.money(c.get("bid"))
            price_text = f"{price:.2f}" if price is not None else "?"
            if won:
                bid_text = f" (your bid {bid:.2f})" if bid is not None else ""
                self.logger.warning(
                    f"WON on eBay: '{title}' for {price_text}{bid_text}",
                    extra=self.notify_extra("You won (eBay)", item_no, priority=4, url=self.url(item_no)),
                )
            else:
                reason = result.get("status") or result.get("message") or f"result code {result.get('success')}"
                self.logger.warning(
                    f"Lost on eBay: '{title}' sold for {price_text} - {reason}",
                    extra=self.notify_extra("Auction lost (eBay)", item_no, priority=3, url=self.url(item_no)),
                )
            self.store.mark_reported([key])
            self.store.remove(item_no)
