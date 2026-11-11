#!/usr/bin/env python3
"""
Local web UI for the ShopGoodwill bid sniper.

Runs the sniper (bid_sniper.BidSniper) in a background thread and serves a
page at http://127.0.0.1:5000 for managing the watchlist, which is stored in
your ShopGoodwill favorites (one JSON note per item, see watchlist.py).

Always starts in DRY-RUN; switch to live from the page.

Usage:
  python webapp.py
  python webapp.py --port 5050 --no-browser
"""

import argparse
import collections
import datetime
import json
import logging
import os
import re
import threading
import time
import webbrowser
from typing import Dict, List, Optional, Tuple

import parsedatetime
from flask import Flask, jsonify, request, send_from_directory

import bid_sniper
import bidslammer
import ebay_desk
import notify
import shopgoodwill
import watchlist

HERE = os.path.dirname(os.path.abspath(__file__))
ITEM_ID_PATTERN = re.compile(r"(?:/item/)?(\d{5,12})(?:\D|$)")

# eBay lead time when none is configured (BidSlammer recommends 5; range 1-15)
DEFAULT_EBAY_SECONDS = 5

# How long cached data stays fresh (seconds). These set how often the page asks
# ShopGoodwill for anything; keep them gentle. The sniper's own pre-bid check
# fetches fresh details regardless, so these never affect bid timing.
ITEM_TTL_NORMAL = 300
ITEM_TTL_ENDING = 30  # for items ending within 10 minutes
FAVORITES_TTL = 120  # edits made from the page clear this immediately


class RingBufferHandler(logging.Handler):
    """
    Keeps the most recent log records in memory for the page's activity log
    """

    def __init__(self, capacity: int = 300) -> None:
        super().__init__(level=logging.INFO)
        self.records: "collections.deque[Dict]" = collections.deque(maxlen=capacity)
        # NOT self.lock: logging.Handler already uses self.lock (held while emit runs)
        self._buffer_lock = threading.Lock()

    def emit(self, record: logging.LogRecord) -> None:
        entry = {
            "time": datetime.datetime.fromtimestamp(record.created).strftime("%H:%M:%S"),
            "level": record.levelname,
            "message": record.getMessage(),
            "url": getattr(record, "notify_url", None),
        }
        with self._buffer_lock:
            self.records.append(entry)

    def recent(self) -> List[Dict]:
        with self._buffer_lock:
            return list(self.records)[::-1]


def with_one_retry(fn, *args, delay: float = 2.0, **kwargs):
    """
    Call fn; if it fails with a temporary HTTP error (see shopgoodwill.is_transient),
    wait `delay` seconds and call it once more. Any other failure, or a second
    failure, is raised.
    """

    try:
        return fn(*args, **kwargs)
    except Exception as e:
        if not shopgoodwill.is_transient(e):
            raise
        time.sleep(delay)
        return fn(*args, **kwargs)


def parse_item_id(text: str) -> Optional[int]:
    """
    Accept a ShopGoodwill item URL or a bare item number
    """

    match = ITEM_ID_PATTERN.search((text or "").strip())
    return int(match.group(1)) if match else None


def parse_time_deltas(texts: List[str]) -> List[datetime.timedelta]:
    """
    Parse strings like "1 hour", "10 minutes" (same parser bid_sniper uses).
    Raises ValueError naming any string that doesn't parse.
    """

    cal = parsedatetime.Calendar()
    deltas = []
    for text in texts:
        delta = cal.parseDT(text, sourceTime=datetime.datetime.min)[0] - datetime.datetime.min
        if delta <= datetime.timedelta(0):
            raise ValueError(f"Can't understand the time '{text}'")
        deltas.append(delta)
    return deltas


class SniperApp:
    def __init__(self, config_path: str, live: bool = False) -> None:
        self.config_path = config_path
        with open(config_path, "r") as f:
            self.config = json.load(f)
        self.config_lock = threading.Lock()

        self.sniper = bid_sniper.BidSniper(self.config, dry_run=not live)

        # console output, activity log for the page, and notifications
        self.ring = RingBufferHandler()
        console = logging.StreamHandler()
        console.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s", "%H:%M:%S"))
        for name in ("shopgoodwill_bid_sniper", "shopgoodwill", "shopgoodwill_notify"):
            lg = logging.getLogger(name)
            lg.setLevel(logging.INFO)
            lg.propagate = False
            lg.addHandler(self.ring)
            lg.addHandler(console)
        self.notify_listener = notify.attach_notifiers(self.sniper.logger, self.config)

        # the web side uses its own client (and login), separate from the sniper's
        self.client = shopgoodwill.Shopgoodwill(self.config["auth_info"])

        self.item_cache: Dict[int, Tuple[float, Dict, datetime.timedelta]] = {}
        self.favorites_cache: Tuple[float, Dict] = (0.0, {})
        self.cache_lock = threading.Lock()
        self.clock_offset: Optional[datetime.timedelta] = None
        self._recent_errors: Dict[Tuple[str, str], float] = {}

        self.sniper_thread = threading.Thread(
            target=self.sniper.start, name="sniper", daemon=True
        )

        # eBay through BidSlammer: only if an API key is configured
        self.ebay: Optional[ebay_desk.EbayDesk] = None
        bs_key = (self.config.get("bidslammer") or {}).get("api_key", "").strip()
        if bs_key:
            sn = self.sniper
            self.ebay = ebay_desk.EbayDesk(
                client=bidslammer.BidSlammer(bs_key),
                store=ebay_desk.EbayStore(
                    os.path.join(os.path.dirname(os.path.abspath(config_path)), "ebay_snipes.json")
                ),
                logger=sn.logger,
                notify_extra=sn.notify_extra,
                tie_breaker=sn.tie_breaker,
                lead_seconds=lambda: float(
                    sn.config["bid_sniper"].get("ebay_snipe_seconds", DEFAULT_EBAY_SECONDS)
                ),
                is_dry_run=lambda: sn.dry_run,
                contest_settings=sn.contest_settings,
                alert_deltas=lambda: sn.alert_time_deltas,
            )

    def start_sniper(self) -> None:
        self.sniper_thread.start()
        if self.ebay:
            self.ebay.start_watcher()
            self.sniper.logger.info("eBay snipes enabled through BidSlammer")
        self.sniper.logger.info(
            "Sniper started in " + ("DRY-RUN" if self.sniper.dry_run else "LIVE") + " mode"
        )

    # ---------- data access ----------

    def get_favorites(self, force: bool = False) -> Dict[int, Dict]:
        with self.cache_lock:
            fetched_at, favorites = self.favorites_cache
            if not force and time.time() - fetched_at < FAVORITES_TTL:
                return favorites
        favorites = self.client.get_favorites()
        with self.cache_lock:
            self.favorites_cache = (time.time(), favorites)
        return favorites

    def get_item(self, item_id: int, end_utc: Optional[datetime.datetime] = None) -> Dict:
        ttl = ITEM_TTL_NORMAL
        if end_utc and end_utc - datetime.datetime.now(datetime.timezone.utc) < datetime.timedelta(minutes=10):
            ttl = ITEM_TTL_ENDING
        with self.cache_lock:
            cached = self.item_cache.get(item_id)
            if cached and time.time() - cached[0] < ttl:
                return cached[1]

        info, offset, _ = self.client.get_item_info_timed(item_id)
        with self.cache_lock:
            self.item_cache[item_id] = (time.time(), info, offset)
            self.clock_offset = offset
        return info

    def invalidate(self, item_id: Optional[int] = None) -> None:
        with self.cache_lock:
            self.favorites_cache = (0.0, {})
            if item_id is not None:
                self.item_cache.pop(item_id, None)
        self.sniper.request_refresh()

    def log_http_error(self, action: str, err: BaseException) -> None:
        """
        Record a failed request in the activity log with its full evidence.
        Repeats of the same failure within a minute are logged once, because the
        page polls every few seconds and would otherwise flood the log.
        """

        detail = shopgoodwill.describe_error(err)
        key = (action, detail.split(" | body:")[0])
        now = time.time()
        if now - self._recent_errors.get(key, 0) < 60:
            return
        self._recent_errors[key] = now
        self.sniper.logger.warning(f"Couldn't {action} - {detail}")

    def save_config(self) -> None:
        """
        Write the in-memory config back to disk (credentials included, unchanged).
        Written to a temp file first so a crash can't leave a half-written config.
        """

        tmp = self.config_path + ".tmp"
        with open(tmp, "w") as f:
            json.dump(self.config, f, indent=4)
            f.write("\n")
        os.replace(tmp, self.config_path)

    # ---------- view model ----------

    def settings_view(self) -> Dict:
        conf = self.config["bid_sniper"]
        notes = self.config.get("notifications", {})
        return {
            "snipe_seconds": self.sniper.bid_time_delta.total_seconds(),
            "ebay_snipe_seconds": int(conf.get("ebay_snipe_seconds", DEFAULT_EBAY_SECONDS)),
            "alert_times": conf.get("alert_time_deltas", []),
            "tie_breaker": self.sniper.tie_breaker(),
            "contest_window_minutes": float(conf.get("contest_window_minutes", 30)),
            "contest_min_bids": int(conf.get("contest_min_bids", 3)),
            "contest_min_bidders": int(conf.get("contest_min_bidders", 2)),
            "ntfy_server": notes.get("ntfy_server", notify.DEFAULT_NTFY_SERVER),
            "ntfy_topic": notes.get("ntfy_topic", ""),
            "windows_toast": bool(notes.get("windows_toast", True)),
        }

    def state(self) -> Dict:
        error = None
        items = []
        try:
            favorites = self.get_favorites()
        except Exception as e:
            favorites = {}
            error = f"Couldn't read favorites: {shopgoodwill.describe_error(e)}"
            self.log_http_error("read favorites", e)

        contest_conf = self.sniper.contest_settings()
        for item_id, fav in favorites.items():
            tracked = watchlist.parse_note(fav.get("notes"))
            try:
                end_utc = shopgoodwill.Shopgoodwill.parse_timestamp_precise(fav["endTime"])
            except Exception:
                end_utc = None
            row = {
                "id": item_id,
                "site": "sgw",
                "title": fav.get("title", str(item_id)),
                "url": f"https://shopgoodwill.com/item/{item_id}",
                "end_utc": end_utc.isoformat() if end_utc else None,
                "tracked": tracked is not None,
                "max_bid": tracked["max_bid"] if tracked else None,
                "alerts": tracked["alerts"] if tracked else False,
                "raw_note": None if tracked else (fav.get("notes") or ""),
            }
            if tracked:
                try:
                    info = self.get_item(item_id, end_utc)
                    bid = (
                        self.sniper.bid_amount(tracked["max_bid"])
                        if tracked["max_bid"]
                        else None
                    )
                    status = watchlist.contest_status(info, max_bid=bid, **contest_conf)
                    row.update(status)
                    row["bid_amount"] = bid
                    row["end_utc"] = shopgoodwill.Shopgoodwill.parse_timestamp_precise(
                        info["endTime"]
                    ).isoformat()
                except Exception as e:
                    row["error"] = shopgoodwill.describe_error(e)
                    self.log_http_error(f"load item {item_id}", e)
            items.append(row)

        if self.ebay:
            try:
                items.extend(self.ebay.rows())
            except Exception as e:
                self.log_http_error("read your BidSlammer snipes", e)
                ebay_error = f"Couldn't read your eBay snipes from BidSlammer: {shopgoodwill.describe_error(e)}"
                error = f"{error} | {ebay_error}" if error else ebay_error

        items.sort(key=lambda r: (not r["tracked"], r["end_utc"] or "9999"))

        offset = self.clock_offset
        return {
            "dry_run": self.sniper.dry_run,
            "ebay_enabled": self.ebay is not None,
            "sniper_alive": self.sniper_thread.is_alive(),
            "server_now_utc": (
                datetime.datetime.now(datetime.timezone.utc) + (offset or datetime.timedelta(0))
            ).isoformat(),
            "clock_offset_seconds": offset.total_seconds() if offset is not None else None,
            "ebay_round_trip_ms": self.ebay.client.round_trip_ms() if self.ebay else None,
            "settings": self.settings_view(),
            "items": items,
            "log": self.ring.recent()[:150],
            "error": error,
        }


def create_app(sniper_app: SniperApp) -> Flask:
    app = Flask(__name__, static_folder=None)
    sa = sniper_app

    def bad(message: str, code: int = 400):
        return jsonify({"ok": False, "error": message}), code

    def read_max_bid(value) -> Optional[float]:
        if value in (None, ""):
            return None
        max_bid = float(value)
        if not (0 < max_bid < 100000):
            raise ValueError("Max bid must be between 0 and 100,000")
        return round(max_bid, 2)

    @app.get("/")
    def index():
        return send_from_directory(os.path.join(HERE, "web"), "index.html")

    @app.get("/api/state")
    def api_state():
        return jsonify(sa.state())

    def ebay_confirm_text(p: Dict) -> str:
        breakdown = (
            f" (max ${p['max_bid']:.2f} + tie-breaker ${p['tie_breaker']:.2f})"
            if p["tie_breaker"]
            else ""
        )
        price = f"${p['current_price']:.2f}" if p["current_price"] is not None else "?"
        minimum = f"${p['minimum']:.2f}" if p["minimum"] is not None else "?"
        return (
            f"Send this eBay snipe to BidSlammer?\n\n{p['title']}\n"
            f"Bid: ${p['bid']:.2f}{breakdown}\n"
            f"Current price {price}, minimum {minimum}\n"
            f"Fires {p['delay']} seconds before the end, from BidSlammer's servers.\n\n"
            "eBay auction wins can't be cancelled."
        )

    def ebay_send(item_id: int, body: Dict):
        """Preview (needs_confirm) or place an eBay snipe"""
        if not sa.ebay:
            return bad("eBay isn't set up: add your BidSlammer API key to config.json")
        try:
            max_bid = read_max_bid(body.get("max_bid"))
            alerts = bool(body.get("alerts", True))
            if not body.get("confirm"):
                p = sa.ebay.preview(item_id, max_bid or 0)
                return jsonify({"ok": True, "needs_confirm": True, "confirm_text": ebay_confirm_text(p), "preview": p})
            p = sa.ebay.place(item_id, max_bid or 0, alerts)
            return jsonify({"ok": True, "id": item_id, "placed": p})
        except (ValueError, bidslammer.BidSlammerError) as e:
            return bad(str(e))
        except Exception as e:
            sa.log_http_error(f"send eBay snipe {item_id}", e)
            return bad(f"Couldn't reach BidSlammer: {shopgoodwill.describe_error(e)}")

    @app.post("/api/items")
    def api_add_item():
        body = request.get_json(force=True) or {}
        ebay_id = ebay_desk.parse_ebay_item(str(body.get("item", "")))
        if ebay_id:
            return ebay_send(ebay_id, body)
        item_id = parse_item_id(str(body.get("item", "")))
        if not item_id:
            return bad("Paste a ShopGoodwill item URL or item number")
        try:
            max_bid = read_max_bid(body.get("max_bid"))
        except ValueError as e:
            return bad(str(e))
        alerts = bool(body.get("alerts", True))

        try:
            info = with_one_retry(sa.client.get_item_info, item_id)
        except Exception as e:
            sa.log_http_error(f"load item {item_id}", e)
            return bad(f"Couldn't load item {item_id}: {shopgoodwill.describe_error(e)}")
        if not info or not info.get("itemId"):
            return bad(f"Item {item_id} wasn't found")
        if info.get("isAuction") is False:
            return bad("That listing isn't an auction (Buy It Now only), so there's nothing to snipe")
        if info.get("isItemEndTimeExpire"):
            return bad("That auction has already ended")

        try:
            with_one_retry(sa.client.add_favorite, item_id, note=watchlist.make_note(max_bid, alerts))
        except Exception as e:
            sa.log_http_error(f"add item {item_id} to favorites", e)
            return bad(f"Couldn't add item {item_id} to favorites: {shopgoodwill.describe_error(e)}")
        sa.invalidate(item_id)
        what = f"max {max_bid:.2f}" if max_bid else "alerts only, no bid"
        sa.sniper.logger.info(f"Added '{info.get('title', item_id)}' ({what})")
        return jsonify({"ok": True, "id": item_id})

    @app.post("/api/items/<int:item_id>")
    def api_update_item(item_id: int):
        body = request.get_json(force=True) or {}
        try:
            max_bid = read_max_bid(body.get("max_bid"))
        except ValueError as e:
            return bad(str(e))
        alerts = bool(body.get("alerts", True))
        try:
            with_one_retry(sa.client.add_favorite_note, item_id, watchlist.make_note(max_bid, alerts))
        except Exception as e:
            sa.log_http_error(f"update item {item_id}", e)
            return bad(f"Couldn't update item {item_id}: {shopgoodwill.describe_error(e)}")
        sa.invalidate(item_id)
        what = f"max {max_bid:.2f}" if max_bid else "no bid"
        sa.sniper.logger.info(f"Updated item {item_id}: {what}, alerts {'on' if alerts else 'off'}")
        return jsonify({"ok": True})

    @app.post("/api/ebay/<int:item_id>")
    def api_ebay_update(item_id: int):
        return ebay_send(item_id, request.get_json(force=True) or {})

    @app.post("/api/ebay/<int:item_id>/alerts")
    def api_ebay_alerts(item_id: int):
        if not sa.ebay:
            return bad("eBay isn't set up")
        sa.ebay.set_alerts(item_id, bool((request.get_json(force=True) or {}).get("alerts", True)))
        return jsonify({"ok": True})

    @app.post("/api/ebay/<int:item_id>/cancel")
    def api_ebay_cancel(item_id: int):
        if not sa.ebay:
            return bad("eBay isn't set up")
        try:
            sa.ebay.cancel(item_id)
        except bidslammer.BidSlammerError as e:
            return bad(str(e))
        except Exception as e:
            sa.log_http_error(f"cancel eBay snipe {item_id}", e)
            return bad(f"Couldn't reach BidSlammer: {shopgoodwill.describe_error(e)}")
        return jsonify({"ok": True})

    @app.post("/api/dry-run")
    def api_dry_run():
        body = request.get_json(force=True) or {}
        enabled = bool(body.get("enabled", True))
        sa.sniper.set_dry_run(enabled)
        sa.sniper.logger.warning(
            "Switched to DRY-RUN: bids will NOT be placed"
            if enabled
            else "Switched to LIVE: real bids WILL be placed"
        )
        return jsonify({"ok": True, "dry_run": enabled})

    @app.post("/api/settings")
    def api_settings():
        body = request.get_json(force=True) or {}
        try:
            snipe_seconds = float(body["snipe_seconds"])
            if not (1 <= snipe_seconds <= 120):
                raise ValueError("ShopGoodwill snipe time must be between 1 and 120 seconds")
            ebay_seconds = body.get("ebay_snipe_seconds", DEFAULT_EBAY_SECONDS)
            if float(ebay_seconds) != int(float(ebay_seconds)) or not (
                bidslammer.MIN_DELAY <= int(float(ebay_seconds)) <= bidslammer.MAX_DELAY
            ):
                raise ValueError(
                    f"eBay snipe time must be a whole number from {bidslammer.MIN_DELAY} "
                    f"to {bidslammer.MAX_DELAY} seconds (BidSlammer's range)"
                )
            ebay_seconds = int(float(ebay_seconds))
            alert_times = [t.strip() for t in body.get("alert_times", []) if t.strip()]
            alert_deltas = parse_time_deltas(alert_times)
            window = float(body["contest_window_minutes"])
            min_bids = int(body["contest_min_bids"])
            min_bidders = int(body["contest_min_bidders"])
            if window <= 0 or min_bids < 1 or min_bidders < 1:
                raise ValueError("Contest settings must be positive")
            topic = str(body.get("ntfy_topic", "")).strip()
            if topic and not re.fullmatch(r"[A-Za-z0-9_-]{1,64}", topic):
                raise ValueError("ntfy topic may only use letters, numbers, - and _")
            server = str(body.get("ntfy_server") or notify.DEFAULT_NTFY_SERVER).strip()
            tie_breaker = round(float(body.get("tie_breaker", 0) or 0), 2)
            if not (0 <= tie_breaker <= sa.sniper.MAX_TIE_BREAKER):
                raise ValueError(
                    f"Tie-breaker must be between $0 and ${sa.sniper.MAX_TIE_BREAKER:.0f}"
                )
            if not server.startswith("https://"):
                raise ValueError("ntfy server must start with https://")
        except (KeyError, TypeError) as e:
            return bad(f"Missing or invalid setting: {e}")
        except ValueError as e:
            return bad(str(e))

        with sa.config_lock:
            conf = sa.config["bid_sniper"]
            conf["bid_snipe_time_delta"] = f"{snipe_seconds:g} seconds"
            conf["ebay_snipe_seconds"] = ebay_seconds
            conf["alert_time_deltas"] = alert_times
            conf["contest_window_minutes"] = window
            conf["contest_min_bids"] = min_bids
            conf["contest_min_bidders"] = min_bidders
            conf["tie_breaker"] = tie_breaker
            notes = sa.config.setdefault("notifications", {})
            notes["ntfy_server"] = server
            notes["ntfy_topic"] = topic
            notes["windows_toast"] = bool(body.get("windows_toast", True))

            # apply live
            sa.sniper.bid_time_delta = datetime.timedelta(seconds=snipe_seconds)
            sa.sniper.alert_time_deltas = alert_deltas

            sa.save_config()

        sa.sniper.logger.info(
            "Settings saved (new alert times apply to items scheduled from now on)"
        )
        return jsonify({"ok": True, "settings": sa.settings_view()})

    @app.post("/api/test-notify")
    def api_test_notify():
        result = notify.deliver(
            sa.config.get("notifications", {}),
            "Sniper test",
            "Test notification from the ShopGoodwill sniper. If you can read this, alerts work.",
            priority=3,
        )
        return jsonify({"ok": True, "result": result})

    return app


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", default="config.json")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=5000)
    parser.add_argument("--no-browser", action="store_true")
    parser.add_argument(
        "--live",
        action="store_true",
        help="Start with real bidding on (default: dry run)",
    )
    args = parser.parse_args()

    if args.host not in ("127.0.0.1", "localhost"):
        print(
            "WARNING: this page has no login. Anyone who can reach "
            f"{args.host}:{args.port} can change your bids."
        )

    sniper_app = SniperApp(args.config, live=args.live)
    sniper_app.start_sniper()
    app = create_app(sniper_app)

    url = f"http://127.0.0.1:{args.port}/"
    print(f"Sniper UI at {url}  (Ctrl+C to stop)")
    if not args.no_browser:
        threading.Timer(1.0, lambda: webbrowser.open(url)).start()

    # werkzeug's request log is noisy (the page polls every few seconds)
    logging.getLogger("werkzeug").setLevel(logging.WARNING)
    app.run(host=args.host, port=args.port, threaded=True, use_reloader=False)


if __name__ == "__main__":
    main()
