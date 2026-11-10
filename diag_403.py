#!/usr/bin/env python3
"""
Read-only diagnosis of HTTP 403s from ShopGoodwill. Never places a bid.

Uses the sniper's own client exactly as it runs (same headers, same login),
and reports what each kind of request gets back, so we can tell apart:
  - a block on the item endpoint only
  - a block on all API requests (logged in or not)
  - a temporary rate limit (the response usually says so)

Usage:
  python diag_403.py 283099586
"""

import json
import sys

import requests

import shopgoodwill

INTERESTING_HEADERS = (
    "server", "date", "retry-after", "cf-ray", "cf-mitigated", "x-cache",
    "x-iinfo", "x-cdn", "via", "content-type", "set-cookie",
)


def show(label: str, resp: requests.Response) -> None:
    print(f"\n=== {label} ===")
    print(f"status: {resp.status_code}")
    for h in INTERESTING_HEADERS:
        if h in resp.headers:
            value = resp.headers[h]
            if h == "set-cookie":
                value = "(present, value hidden)"
            print(f"{h}: {value}")
    body = resp.text or ""
    snippet = " ".join(body.split())[:300]
    print(f"body ({len(body)} chars): {snippet}")


def main() -> None:
    if len(sys.argv) != 2:
        print("usage: python diag_403.py <item_id>")
        sys.exit(2)
    item_id = int(sys.argv[1])
    item_url = f"{shopgoodwill.Shopgoodwill.API_ROOT}/itemDetail/GetItemDetailModelByItemId/{item_id}"

    # 1. not logged in, the sniper's client headers
    anon = shopgoodwill.Shopgoodwill()
    anon.shopgoodwill_session.hooks["response"] = []  # report errors instead of raising
    show("item details, NOT logged in", anon.shopgoodwill_session.get(item_url))

    # 2. logged in, same client the sniper uses
    with open("config.json") as f:
        config = json.load(f)
    try:
        authed = shopgoodwill.Shopgoodwill(config["auth_info"])
    except Exception as e:
        print(f"\n=== login ===\nFAILED: {type(e).__name__}: {e}")
        return
    print("\n=== login ===\nOK")
    authed.shopgoodwill_session.hooks["response"] = []
    show("item details, logged in", authed.shopgoodwill_session.get(item_url))

    # 3. a logged-in endpoint the sniper also uses (read-only)
    show(
        "favorites list, logged in",
        authed.shopgoodwill_session.post(
            f"{shopgoodwill.Shopgoodwill.API_ROOT}/Favorite/GetAllFavoriteItemsByType",
            params={"Type": "open"},
            json={},
        ),
    )

    print("\nNo bids were placed.")


if __name__ == "__main__":
    main()
