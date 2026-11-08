#!/usr/bin/env python3
"""
Read-only access check for ShopGoodwill. NEVER places a bid.

Steps:
  1. Log in with the credentials in config.json
  2. Read your favorites and show which ones have a max_bid note
  3. Fetch details for one item and measure the server clock offset

Usage:
  python check_access.py
  python check_access.py --item 283277658
  python check_access.py --config other_config.json
"""

import argparse
import datetime
import json
import sys

from requests.exceptions import HTTPError, RequestException

import shopgoodwill


def fail(step: str, err: BaseException) -> None:
    status = None
    if isinstance(err, HTTPError) and err.response is not None:
        status = err.response.status_code

    print(f"\nFAILED at step: {step}")
    print(f"  {type(err).__name__}: {err}")
    if status == 403:
        print(
            "  HTTP 403 means ShopGoodwill refused the request. If this happens from\n"
            "  your home connection, stop here and report back before going further."
        )
    elif status == 401:
        print("  HTTP 401 means the login was rejected or the token is invalid.")
    sys.exit(1)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[1])
    parser.add_argument("--config", default="config.json")
    parser.add_argument(
        "--item",
        type=int,
        default=None,
        help="Item ID to test. Defaults to your first favorite.",
    )
    args = parser.parse_args()

    with open(args.config, "r") as f:
        config = json.load(f)

    # 1. login
    print("[1/3] Logging in...")
    try:
        client = shopgoodwill.Shopgoodwill(config["auth_info"])
    except Exception as e:
        fail("login", e)
    print("      OK, logged in")

    # 2. favorites
    print("[2/3] Reading favorites...")
    try:
        favorites = client.get_favorites()
    except (RequestException, Exception) as e:
        fail("read favorites", e)

    print(f"      OK, {len(favorites)} open favorite(s)")
    for item_id, fav in favorites.items():
        note = fav.get("notes") or ""
        try:
            max_bid = json.loads(note).get("max_bid") if note else None
        except (ValueError, AttributeError):
            max_bid = None
        label = f"max_bid {max_bid}" if max_bid is not None else "no max_bid, will NOT bid"
        print(f"      {item_id}  ends {fav['endTime']} PT  {label}  {fav['title'][:50]}")

    item_id = args.item or (next(iter(favorites)) if favorites else None)
    if item_id is None:
        print("\n[3/3] Skipped: no favorites and no --item given.")
        print("      Login and favorites work. Re-run with --item <id> to test the clock sync.")
        return

    # 3. item details + clock offset
    print(f"[3/3] Fetching item {item_id} and measuring server clock...")
    offsets = []
    info = None
    try:
        for _ in range(3):
            sent = datetime.datetime.now(datetime.timezone.utc)
            info = client.get_item_info(item_id)
            received = datetime.datetime.now(datetime.timezone.utc)
            rtt = received - sent
            server_now = shopgoodwill.Shopgoodwill.parse_timestamp_precise(
                info["serverTime"]
            )
            offsets.append(((server_now - (sent + rtt / 2)).total_seconds(), rtt))
    except Exception as e:
        fail("fetch item details", e)

    print(f"      OK, '{info['title'][:60]}'")
    print(
        f"      current price {info['currentPrice']}, minimum bid {info['minimumBid']}, "
        f"{info['numberOfBids']} bid(s), ends {info['endTime']} PT"
    )
    for off, rtt in offsets:
        print(
            f"      server clock vs yours: {off:+.3f}s  (round trip {rtt.total_seconds() * 1000:.0f}ms)"
        )

    print("\nAll checks passed. No bids were placed.")


if __name__ == "__main__":
    main()
