import asyncio, datetime, json
import responses
from conftest import API, item_payload, favorites_payload, REAL_ITEM
import watchlist

HIGH = "<h3><b>Bid Received! </b></h3><p>You are <strong>currently</strong> the high bidder for this auction. </p>"

def _run(make_sniper, tie, max_bid=9.0, min_bid=8.0, dry=False):
    b = make_sniper(responses, extra_cfg={"tie_breaker": tie, "result_check_delay_seconds": 30}, dry_run=dry)
    end = datetime.datetime.now(datetime.timezone.utc) + datetime.timedelta(seconds=1.2)
    responses.add(responses.POST, f"{API}/Favorite/GetAllFavoriteItemsByType", json=favorites_payload(1, end, max_bid))
    responses.add(responses.GET, f"{API}/itemDetail/GetItemDetailModelByItemId/1", json=item_payload(end, minimumBid=min_bid))
    responses.add(responses.POST, f"{API}/ItemBid/PlaceBid", json={"message": HIGH})
    asyncio.run(b.place_bid(1, end))
    return b, [json.loads(c.request.body) for c in responses.calls if "PlaceBid" in c.request.url]

@responses.activate
def test_bid_is_max_plus_tie_breaker(make_sniper, caplog):
    _, bids = _run(make_sniper, 0.37)
    assert bids[0]["bidAmount"] == "9.37"
    assert "Bid 9.37 (max 9.00 + tie-breaker 0.37) placed" in caplog.text

@responses.activate
def test_zero_tie_breaker_unchanged(make_sniper, caplog):
    _, bids = _run(make_sniper, 0)
    assert bids[0]["bidAmount"] == "9.00" and "Bid 9.00 placed" in caplog.text

@responses.activate
def test_priced_out_uses_bid_amount(make_sniper, caplog):
    # minimum 9.20 is over the 9.00 max but under the 9.37 bid: still bids
    _, bids = _run(make_sniper, 0.37, min_bid=9.20)
    assert bids and bids[0]["bidAmount"] == "9.37"

@responses.activate
def test_priced_out_beyond_tie_breaker(make_sniper, caplog):
    _, bids = _run(make_sniper, 0.37, min_bid=9.50)
    assert not bids and "your bid would be 9.37 (max 9.00 + tie-breaker 0.37)" in caplog.text

@responses.activate
def test_typo_is_capped(make_sniper):
    b = make_sniper(responses, extra_cfg={"tie_breaker": 37})
    assert b.tie_breaker() == 5.0 and b.bid_amount(9) == 14.0
    b.config["bid_sniper"]["tie_breaker"] = "junk"
    assert b.tie_breaker() == 0.0
    b.config["bid_sniper"]["tie_breaker"] = -2
    assert b.tie_breaker() == 0.0

@responses.activate
def test_dry_run_shows_breakdown(make_sniper, caplog):
    _, bids = _run(make_sniper, 0.37, dry=True)
    assert not bids and "DRY-RUN: Placing bid on 'Test Amp' for 9.37 (max 9.00 + tie-breaker 0.37)" in caplog.text

def test_describe_with_tie_breaker():
    s = watchlist.contest_status(REAL_ITEM, max_bid=800.37)
    assert "your bid $800.37 (max $800.00 + $0.37) (94%)" in watchlist.describe(s, 800.37, max_bid=800.0)
    assert "your max $800.00 (94%)" in watchlist.describe(watchlist.contest_status(REAL_ITEM, max_bid=800), 800, max_bid=800)
