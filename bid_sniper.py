#!/usr/bin/env python3

import argparse
import asyncio
import datetime
import json
import logging
import logging.config
import queue
from logging.handlers import QueueHandler, QueueListener
from typing import Any, Callable, Dict, Iterable, Optional, Tuple
from zoneinfo import ZoneInfo

import parsedatetime
from requests.exceptions import HTTPError
from requests.models import Response

import shopgoodwill
import watchlist


def get_timedelta_to_time(
    end_time: datetime.datetime, truncate_microseconds: Optional[bool] = True
) -> datetime.timedelta:
    """
    Given a datetime object (hopefully in the future),
    return a timedelta which is timezone aware or unaware,
    depending on the input datetime

    :param end_time: A datetime object (timezone aware or unaware)
        from which to get a timedelta from now until then
    :type end_time: datetime.datetime
    :param truncate_microseconds: If set, truncate microseconds from datetimes
    :type truncate_microseconds: Optional[bool]
    :return: A timedelta from end_time to now
    :rtype: datetime.timedelta
    """

    if truncate_microseconds:
        end_time = end_time.replace(microsecond=0)
        now = datetime.datetime.now().replace(microsecond=0)

    else:
        now = datetime.datetime.now()

    if end_time.tzinfo is None:
        return end_time - now

    else:
        # by default, astimezone will return the local timezone
        return end_time - now.astimezone()


class BidSniper:
    def outage_check_hook(self, http_response: Response, *args, **kwargs):
        if http_response.status_code in range(500, 600):
            if self.outage_start_time is None:
                # start tracking this outage
                self.outage_start_time = datetime.datetime.now(datetime.timezone.utc)
                self.logger.error(
                    f"Outage detected - SGW returned HTTP {http_response.status_code} for URL {http_response.url}"
                )
            else:
                # already tracking this error, don't do anything
                pass

        else:
            # If there's no error and there was an ongoing outage,
            # stop tracking it and alert on the elapsed time
            if self.outage_start_time is not None:
                elapsed_outage_time = (
                    datetime.datetime.now(datetime.timezone.utc)
                    - self.outage_start_time
                )
                self.outage_start_time = None

                self.logger.info(f"Outage ended - time elapsed: {elapsed_outage_time}")

        # TODO this should instead call SGW's shopgoodwill_err_hook method
        # TODO move the above from a function to a method!
        http_response.raise_for_status()

    def __init__(self, config: Dict, dry_run: bool = False) -> None:
        self.config = config
        self.dry_run = dry_run
        self.dry_run_msg = "DRY-RUN: " if dry_run else ""

        self.event_loop = asyncio.new_event_loop()

        self.outage_start_time = None

        # if set, the default note to assign favorites that don't have a note
        self.default_note = self.config["bid_sniper"].get("favorite_default_note", None)

        self.date_format = "%Y-%m-%dT%H:%M:%S"

        # logging setup
        logging_conf = config.get("logging", dict())
        self.logger = logging.getLogger("shopgoodwill_bid_sniper")
        # check if we're using logging.config.dictConfig or not
        #
        # load entire logging config from dictConfig format
        if logging_conf.get("version", 0) >= 1:
            logging.config.dictConfig(logging_conf)

        # legacy logging config format
        else:
            logging.basicConfig()
            self.logger.setLevel(logging_conf.get("log_level", logging.INFO))
            log_queue = queue.Queue()
            queue_handler = QueueHandler(log_queue)

            if "gotify" in logging_conf:
                from gotify_handler import GotifyHandler

                queue_listener = QueueListener(
                    log_queue, GotifyHandler(**logging_conf["gotify"])
                )
                self.logger.addHandler(queue_handler)
                queue_listener.start()

        # TODO since this is a daemon,
        # we'll actually have to bother refreshing the token!

        # check if the user wants to use separate accounts for commands/bids
        # (purely for ban evasion)
        if config["auth_info"].get("auth_type", "universal") == "command_bid":
            self.shopgoodwill_client = shopgoodwill.Shopgoodwill(
                config["auth_info"]["command_account"]
            )
            self.bid_shopgoodwill_client = shopgoodwill.Shopgoodwill(
                config["auth_info"]["bid_account"]
            )

        else:
            self.shopgoodwill_client = shopgoodwill.Shopgoodwill(config["auth_info"])
            self.bid_shopgoodwill_client = self.shopgoodwill_client

        # modify the hooks for our shopgoodwill sessions
        self.shopgoodwill_client.shopgoodwill_session.hooks["response"] = (
            self.outage_check_hook
        )
        self.bid_shopgoodwill_client.shopgoodwill_session.hooks["response"] = (
            self.outage_check_hook
        )

        # custom time alerting setup
        self.alert_time_deltas = list()
        cal = parsedatetime.Calendar()
        for time_delta_str in config["bid_sniper"].get("alert_time_deltas", list()):
            time_delta = (
                cal.parseDT(time_delta_str, sourceTime=datetime.datetime.min)[0]
                - datetime.datetime.min
            )
            if time_delta != datetime.timedelta(0):
                self.alert_time_deltas.append(time_delta)
            else:
                self.logger.warning("Invalid time delta string '{time_delta_str}'")

        # bid placing setup
        bid_time_delta_str = self.config["bid_sniper"].get(
            "bid_snipe_time_delta", "5 seconds"
        )
        self.bid_time_delta = (
            cal.parseDT(bid_time_delta_str, sourceTime=datetime.datetime.min)[0]
            - datetime.datetime.min
        )
        if self.bid_time_delta == datetime.timedelta(0):
            self.logger.warning("Invalid time delta string '{time_delta_str}'")

        # The pre-bid check (refresh favorites, fetch item details,
        # measure the server clock offset) runs this long before the bid
        # fires, so that the only request made at fire time is the bid itself
        self.bid_preflight_delta = datetime.timedelta(
            seconds=float(self.config["bid_sniper"].get("bid_preflight_seconds", 20))
        )

        # after a real bid, how long to wait (per attempt) before checking the result
        self.result_check_delay_seconds = float(
            self.config["bid_sniper"].get("result_check_delay_seconds", 60)
        )

        self.favorites_cache = {
            "last_updated": datetime.datetime(1970, 1, 1, tzinfo=datetime.timezone.utc),
            "favorites": dict(),
        }

        self.scheduled_tasks = (
            set()
        )  # Will contain itemIds tentatively scheduled for actions

        return

    @staticmethod
    def notify_extra(title: str, item_id: int, priority: int = 3) -> Dict:
        """
        Build the logging `extra` dict that marks a log record
        for push/desktop notification (see notify.py)

        :param title: Short notification title
        :param item_id: Item the notification is about (used for the click-through URL)
        :param priority: 1 (min) to 5 (max), ntfy-style
        :rtype: Dict
        """

        return {
            "notify": True,
            "notify_title": title,
            "notify_url": f"https://shopgoodwill.com/item/{item_id}",
            "notify_priority": priority,
        }

    def update_favorites_cache(self, max_cache_time: int) -> None:
        """
        Simple function to update the local favorites cache

        :param max_cache_time: The number of seconds
            after which the cache should be refreshed
        :type max_cache_time: int
        :rtype: None
        """

        # total_seconds, not .seconds: .seconds ignores whole days, so a cache
        # stamped in 1970 (the initial value) could look fresh
        if (
            datetime.datetime.now(datetime.timezone.utc)
            - self.favorites_cache["last_updated"]
        ).total_seconds() > max_cache_time:
            try:
                self.favorites_cache = {
                    "favorites": self.shopgoodwill_client.get_favorites(),
                    "last_updated": datetime.datetime.now(datetime.timezone.utc),
                }

            except BaseException as be:
                # TODO this should list all possible exceptions that SGW could raise
                if self.outage_start_time is not None:
                    self.logger.error(
                        f"{type(be).__name__} updating favorites cache - {be}"
                    )

    def task_err_handler(self, finished_task: asyncio.Task) -> None:
        """
        Simple Task callback function to log exceptions from tasks, if present

        :param finished_task:
        :type finished_task: asyncio.Task
        :rtype: None
        """

        coro_exception = finished_task.exception()
        if coro_exception:
            self.logger.error(
                f'Exception in coroutine "{getattr(finished_task.get_coro(), "__name__", "null")}" - {type(coro_exception).__name__} - {coro_exception}'
            )

    async def schedule_task(
        self,
        coroutine,
        execution_datetime: datetime.datetime,
        callbacks: Optional[Iterable[Callable[[asyncio.Task], Any]]] = None,
    ) -> None:
        """
        Simple function to delay a coroutine's execution
        until a given timezone-aware datetime

        :param coroutine: The coroutine to execute at execution_datetime
        :param execution_datetime: A timezone-aware datetime
        :type execution_datetime: datetime.datetime
        :param callbacks: If specified, an iterable of callback functions
            to be applied to the task to schedule
        :type callbacks: Optional[Iterable[Callable[[asyncio.Task], Any]]]
        :rtype: None
        """

        now = datetime.datetime.now(datetime.timezone.utc)
        await asyncio.sleep((execution_datetime - now).total_seconds())
        task = self.event_loop.create_task(coroutine)

        if callbacks:
            for callback in callbacks:
                task.add_done_callback(callback)
                callbacks = [self.task_err_handler]

    async def time_alert(self, item_id: int, end_time: datetime.datetime) -> None:
        """
        Simply logs an alert to remind the user that an auction is ending

        :param item_id: A valid ShopGoodwill item ID
        :type item_id: int
        :param end_time: The end time of the auction
        :type end_time: datetime.datetime
        :rtype: None
        """

        self.update_favorites_cache(
            self.config["bid_sniper"].get("favorites_max_cache_seconds", 60)
        )

        # Check if we still want to alert on this item:
        # it must still be a favorite, be tracked, and have alerts on
        favorite = self.favorites_cache["favorites"].get(item_id, None)
        if not favorite:
            return None
        tracked = watchlist.parse_note(favorite.get("notes", None))
        if not tracked or not tracked["alerts"]:
            return None

        delta_until_end = end_time.replace(microsecond=0) - datetime.datetime.now(
            datetime.timezone.utc
        ).replace(microsecond=0)
        message = f"Time alert - {favorite['title']} ending in {delta_until_end}"

        # add the contest label and price vs max, if item details are available
        try:
            item_info = self.shopgoodwill_client.get_item_info(item_id)
            status = watchlist.contest_status(
                item_info, max_bid=tracked["max_bid"], **self.contest_settings()
            )
            message += " | " + watchlist.describe(status, tracked["max_bid"])
        except BaseException as be:
            self.logger.warning(
                f"Couldn't get item details for the time alert - {shopgoodwill.describe_error(be)}"
            )

        self.logger.warning(
            message,
            extra=self.notify_extra(f"Ending in {delta_until_end}", item_id, priority=3),
        )
        return None

    def contest_settings(self) -> Dict:
        """
        Contest-detection thresholds from config (see watchlist.contest_status)
        """

        conf = self.config["bid_sniper"]
        return {
            "window_minutes": float(conf.get("contest_window_minutes", 30)),
            "min_bids": int(conf.get("contest_min_bids", 3)),
            "min_bidders": int(conf.get("contest_min_bidders", 2)),
        }

    def fetch_item_info_timed(
        self, item_id: int
    ) -> Tuple[Dict, datetime.timedelta, datetime.timedelta]:
        """
        Fetch item details and the server clock offset
        (see Shopgoodwill.get_item_info_timed)
        """

        return self.shopgoodwill_client.get_item_info_timed(item_id)

    async def place_bid(
        self, item_id: int, fallback_end_time: datetime.datetime
    ) -> None:
        """
        Called bid_preflight_seconds before the bid should fire.
        Given an item ID, do the following:
            Check if it's still in our favorites
            If it is, see if we've set a max bid amount for it
            Fetch item details, sync to the server clock,
                and run the friend_list check
            Sleep until bid_snipe_time_delta before the server's end time
            Place a bid for the max bid amount

        :param item_id: A valid ShopGoodwill item ID
        :type item_id: int
        :param fallback_end_time: The end time from the favorites list,
            used with the local clock if item details can't be fetched
        :type fallback_end_time: datetime.datetime
        :rtype: None
        """

        # we must be _absolutely_ sure that the user still wishes to place a bid -
        # the config could've changed between scheduling and this function call

        # force an update of the favorites cache,
        # as we don't want to place erronous bids
        self.update_favorites_cache(5)

        # find the max_bid amount (if present) for this itemId
        favorite = self.favorites_cache["favorites"].get(item_id, None)
        if not favorite:
            return None

        tracked = watchlist.parse_note(favorite.get("notes", None))
        if not tracked or not tracked["max_bid"]:
            return None
        max_bid = tracked["max_bid"]

        # Fetch item details once, now (during the preflight window):
        # it gives us the server clock, the authoritative end time,
        # and the bid history for the friend_list check.
        #
        # If this fails, fall back to the favorites end time and the local clock
        # rather than skipping the bid.
        fire_at = None
        try:
            item_info, clock_offset, rtt = self.fetch_item_info_timed(item_id)
            server_end_time = shopgoodwill.Shopgoodwill.parse_timestamp_precise(
                item_info["endTime"]
            )
            # convert the server's end time into local-clock terms
            local_end_time = server_end_time - clock_offset
            fire_at = local_end_time - self.bid_time_delta
            self.logger.info(
                f"Clock sync for '{favorite['title']}' - local clock is "
                f"{-clock_offset.total_seconds():+.3f}s vs server "
                f"(round trip {rtt.total_seconds() * 1000:.0f}ms), "
                f"bid fires {self.bid_time_delta.total_seconds():g}s before end"
            )

            # Don't send a bid that's sure to be rejected:
            # minimumBid is the lowest amount SGW will accept right now
            minimum_bid = item_info.get("minimumBid", None)
            if minimum_bid is not None and float(minimum_bid) > max_bid:
                self.logger.warning(
                    f"Priced out of '{favorite['title']}' - minimum bid is "
                    f"{float(minimum_bid):.2f}, your max is {max_bid:.2f}. Not bidding."
                )
                return None

            # if we want to use the friend_list feature,
            # don't bid if the current highest bidder is on our friend list
            if self.config.get("friend_list", list()):
                bid_summary = item_info["bidHistory"].get("bidSummary", list())
                if bid_summary:
                    bidder_name = bid_summary[0]["bidderName"]
                    if bidder_name in self.config.get("friend_list", list()):
                        self.logger.info(
                            f"Canceling bid due to friendship for item '{favorite['title']}' - current high bidder {bidder_name}"
                        )
                        return None

        except BaseException as be:
            self.logger.error(
                f"Couldn't get item details for '{favorite['title']}' - "
                f"{shopgoodwill.describe_error(be)} - continuing with local clock"
            )
            fire_at = fallback_end_time - self.bid_time_delta

        # Wait for the precise moment, then place the bid
        delay = (fire_at - datetime.datetime.now(datetime.timezone.utc)).total_seconds()
        if delay > 0:
            await asyncio.sleep(delay)

        # Finally place a bid
        title = favorite["title"]
        if self.dry_run:
            self.logger.warning(
                f"{self.dry_run_msg}Placing bid on '{title}' for {max_bid}",
                extra=self.notify_extra("Dry-run bid", item_id, priority=3),
            )
            return None

        # TODO in the future, address how SGW uses quantity
        # I don't think they use it in auctions
        #
        # Note that the account used is bidding_shopgoodwill_client
        try:
            result = self.bid_shopgoodwill_client.place_bid(
                item_id, max_bid, favorite["sellerId"], quantity=1
            )
        except HTTPError as he:
            self.logger.error(
                f"Bid on '{title}' FAILED - {shopgoodwill.describe_error(he)}",
                extra=self.notify_extra("Bid FAILED", item_id, priority=5),
            )
            return None

        outcome = result["outcome"]
        if outcome == shopgoodwill.Shopgoodwill.BID_HIGH_BIDDER:
            self.logger.warning(
                f"Bid {max_bid:.2f} placed on '{title}' - you are the high bidder",
                extra=self.notify_extra("Bid placed: high bidder", item_id, priority=4),
            )
        elif outcome == shopgoodwill.Shopgoodwill.BID_OUTBID:
            self.logger.warning(
                f"Bid {max_bid:.2f} placed on '{title}' - already outbid "
                "(someone else has a higher max)",
                extra=self.notify_extra("Bid placed: outbid", item_id, priority=4),
            )
        elif outcome == shopgoodwill.Shopgoodwill.BID_CLOSED:
            self.logger.error(
                f"Bid on '{title}' rejected - auction already closed: {result['message']}",
                extra=self.notify_extra("Bid too late", item_id, priority=5),
            )
            return None
        else:
            self.logger.warning(
                f"Bid {max_bid:.2f} placed on '{title}' - unrecognized response: "
                f"{result['message'] or result['raw']}",
                extra=self.notify_extra("Bid placed: check result", item_id, priority=4),
            )

        # check whether we won once the auction has had time to close
        asyncio.get_running_loop().create_task(
            self.check_result(item_id, title, max_bid)
        ).add_done_callback(self.task_err_handler)

        return None

    async def check_result(
        self, item_id: int, title: str, max_bid: float, attempts: int = 3
    ) -> None:
        """
        After a real bid, wait for the auction to close and report
        whether the logged-in bidding account won, and at what price.

        :param item_id: A valid ShopGoodwill item ID
        :param title: The item title, for messages
        :param max_bid: The max bid that was placed
        :param attempts: How many times to check (60s apart) for the close
        :rtype: None
        """

        for _ in range(attempts):
            await asyncio.sleep(self.result_check_delay_seconds)
            try:
                info = self.bid_shopgoodwill_client.get_item_info(item_id)
            except BaseException as be:
                self.logger.error(
                    f"Couldn't check the result for '{title}' - {shopgoodwill.describe_error(be)}"
                )
                continue

            history = info.get("bidHistory") or {}
            if not history.get("auctionClosed"):
                continue

            summary = history.get("bidSummary") or []
            price = history.get("itemCurrentPrice", info.get("currentPrice"))
            if summary and summary[0].get("isLoggedInBuyer"):
                self.logger.warning(
                    f"WON '{title}' for {float(price):.2f} (your max was {max_bid:.2f})",
                    extra=self.notify_extra("You won", item_id, priority=4),
                )
            else:
                self.logger.warning(
                    f"Lost '{title}' - sold for {float(price):.2f} "
                    f"(your max was {max_bid:.2f})",
                    extra=self.notify_extra("Auction lost", item_id, priority=3),
                )
            return None

        self.logger.warning(f"Could not confirm the result for '{title}' - check the site")
        return None

    def request_refresh(self) -> None:
        """
        Thread-safe: make the main loop re-read favorites and schedule
        new items right away, instead of waiting up to refresh_seconds.
        Used by the web UI after adding or editing an item.
        """

        self.favorites_cache["last_updated"] = datetime.datetime(
            1970, 1, 1, tzinfo=datetime.timezone.utc
        )
        wake = getattr(self, "_wake", None)
        if wake is not None:
            self.event_loop.call_soon_threadsafe(wake.set)

    def set_dry_run(self, dry_run: bool) -> None:
        """
        Switch dry-run on or off; applies to the next bid that fires
        """

        self.dry_run = dry_run
        self.dry_run_msg = "DRY-RUN: " if dry_run else ""

    def start(self) -> None:
        """
        Simple method to start the bid sniper instance's event loop

        :rtype: None
        """

        self.event_loop.create_task(self.main_loop())
        self.event_loop.run_forever()

    async def main_loop(self) -> None:
        refresh_seconds = self.config["bid_sniper"].get("refresh_seconds", 300)
        favorites_cache_max_seconds = self.config["bid_sniper"].get(
            "favorites_max_cache_seconds", 60
        )

        self._wake = asyncio.Event()

        while True:
            now = datetime.datetime.now(datetime.timezone.utc)

            # sort all tasks to schedule (time alerts and bid placing) from "soonest" to "futhest"
            # once sorted, use the "soonest" delta to determine when to schedule events.
            # Recomputed every pass so settings changed from the web UI take effect.
            min_scheduling_timedelta = sorted(
                self.alert_time_deltas
                + [self.bid_time_delta + self.bid_preflight_delta]
            )[::-1][0]

            # update favorites
            self.update_favorites_cache(favorites_cache_max_seconds)

            for item_id, favorite_info in self.favorites_cache["favorites"].items():
                # Don't double-schedule tasks
                if item_id in self.scheduled_tasks:
                    continue

                # SGW has been including microseconds inconsistently,
                # and one is liable to see '2025-04-29T23:00:17.45' in
                # the same response as '2025-05-01T22:09:00'
                end_time = favorite_info["endTime"]
                date_format = self.date_format
                if "." in end_time:
                    date_format += ".%f"

                # so close but yet so far away from ISO-8601
                # SGW simply trims the "PDT" (or PST?) off of the timestamps
                # TODO validate that the site only uses a single timezone!
                end_time = (
                    datetime.datetime.strptime(end_time, date_format)
                    .replace(tzinfo=ZoneInfo("America/Los_Angeles"))
                    .astimezone(ZoneInfo("Etc/UTC"))
                )

                # only schedule tasks for the item if the "nearest" task is within refresh_seconds * 3 seconds
                # TODO flip this to "if less than, schedule thing"
                if (end_time - min_scheduling_timedelta) <= now + datetime.timedelta(
                    seconds=refresh_seconds * 3
                ):
                    # schedule reminders for whenever the user configured
                    for alert_time_delta in self.alert_time_deltas:
                        execution_datetime = end_time - alert_time_delta

                        # skip events in the past
                        if execution_datetime < now:
                            continue

                        self.event_loop.create_task(
                            self.schedule_task(
                                self.time_alert(item_id, end_time),
                                execution_datetime,
                                [self.task_err_handler],
                            )
                        ).add_done_callback(self.task_err_handler)

                    # schedule a tentative max_bid for this item
                    # (place_bid itself sleeps until the precise fire time)
                    self.event_loop.create_task(
                        self.schedule_task(
                            self.place_bid(item_id, end_time),
                            end_time - self.bid_time_delta - self.bid_preflight_delta,
                            [self.task_err_handler],
                        )
                    ).add_done_callback(self.task_err_handler)

                    # mark this item ID as "scheduled"
                    self.logger.debug(
                        f"Scheduled events for item {favorite_info['title']}"
                    )
                    self.scheduled_tasks.add(item_id)

                # if configured, set notes for entries that do not have notes
                if self.default_note and not favorite_info.get("notes", ""):
                    self.shopgoodwill_client.add_favorite(
                        item_id, note=self.default_note
                    )

            # sleep until the next refresh, or until request_refresh() wakes us
            try:
                await asyncio.wait_for(self._wake.wait(), timeout=refresh_seconds)
            except asyncio.TimeoutError:
                pass
            self._wake.clear()


def parse_args():
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--config",
        type=str,
        default="config.json",
        help="Path to config file - defaults to ./config.json",
    )
    parser.add_argument(
        "-n",
        "--dry-run",
        action="store_true",
        help="If set, do not perform any actions "
        "that modify state on ShopGoodwill (eg. placing bids)",
    )
    args = parser.parse_args()

    return args


def main():
    args = parse_args()
    with open(args.config, "r") as f:
        config = json.load(f)

    bid_sniper = BidSniper(config, args.dry_run)

    # phone push / Windows notifications (see notify.py)
    import notify

    notify.attach_notifiers(bid_sniper.logger, config)

    bid_sniper.start()


if __name__ == "__main__":
    # TODO this doesn't seem to call main()...
    # with daemon.DaemonContext():
    #    main()
    main()
