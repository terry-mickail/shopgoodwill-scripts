import asyncio, datetime, json, time
import responses
from conftest import API, item_payload, favorites_payload, pt_str
import shopgoodwill as sgwmod

def test_precise_parse_fraction_lengths():
    p = sgwmod.Shopgoodwill.parse_timestamp_precise
    assert p("2026-11-07T20:16:24.61").microsecond == 610000
    assert p("2026-11-07T20:16:24.6123456").microsecond == 612345
    assert p("2026-11-07T20:16:24").microsecond == 0
    assert p("2026-11-07T20:16:24").utcoffset() == datetime.timedelta(0)
    assert p("2026-11-07T20:16:24").hour == 4  # PST is UTC-8

@responses.activate
def test_offset_estimate(make_sniper):
    b = make_sniper(responses)
    end = datetime.datetime.now(datetime.timezone.utc) + datetime.timedelta(hours=1)
    responses.add(responses.GET, f"{API}/itemDetail/GetItemDetailModelByItemId/1",
                  json=item_payload(end, server_offset_s=2.5))
    _, off, rtt = b.fetch_item_info_timed(1)
    assert abs(off.total_seconds() - 2.5) < 0.05

@responses.activate
def test_bid_fires_on_server_clock(make_sniper):
    """Server clock runs 3s ahead: bid must fire 3s earlier than the local-clock end time implies."""
    b = make_sniper(responses)
    now = datetime.datetime.now(datetime.timezone.utc)
    server_end = now + datetime.timedelta(seconds=5.0)      # end time in server terms
    local_fallback_end = now + datetime.timedelta(seconds=8.0)  # what local clock naively thinks
    responses.add(responses.POST, f"{API}/Favorite/GetAllFavoriteItemsByType",
                  json=favorites_payload(1, server_end, 40))
    # server clock is 3s ahead of local, so its "now" reads now+3 and end reads now+3+5
    responses.add(responses.GET, f"{API}/itemDetail/GetItemDetailModelByItemId/1",
                  json=item_payload(server_end + datetime.timedelta(seconds=3), server_offset_s=3.0, minimumBid=10.0))
    fired = {}
    def bid_cb(req):
        fired["t"] = datetime.datetime.now(datetime.timezone.utc)
        fired["body"] = json.loads(req.body)
        return (200, {}, json.dumps({"message": "<h3><b>Bid Received! </b></h3>"}))
    responses.add_callback(responses.POST, f"{API}/ItemBid/PlaceBid", callback=bid_cb)
    asyncio.run(b.place_bid(1, local_fallback_end))
    # expected fire (local) = server_end_local(now+5) - 1s delta = now+4
    err = (fired["t"] - (now + datetime.timedelta(seconds=4))).total_seconds()
    assert abs(err) < 0.15, err
    assert fired["body"]["bidAmount"] == "40.00" and fired["body"]["sellerId"] == 148

@responses.activate
def test_item_fetch_failure_falls_back_to_local_clock(make_sniper):
    b = make_sniper(responses)
    now = datetime.datetime.now(datetime.timezone.utc)
    end = now + datetime.timedelta(seconds=2.0)
    responses.add(responses.POST, f"{API}/Favorite/GetAllFavoriteItemsByType",
                  json=favorites_payload(1, end, 40))
    responses.add(responses.GET, f"{API}/itemDetail/GetItemDetailModelByItemId/1", status=503)
    fired = {}
    def bid_cb(req):
        fired["t"] = datetime.datetime.now(datetime.timezone.utc)
        return (200, {}, json.dumps({"message": "Bid Received!"}))
    responses.add_callback(responses.POST, f"{API}/ItemBid/PlaceBid", callback=bid_cb)
    asyncio.run(b.place_bid(1, end))
    assert abs((fired["t"] - (end - datetime.timedelta(seconds=1))).total_seconds()) < 0.15

@responses.activate
def test_dry_run_never_posts_bid(make_sniper, caplog):
    b = make_sniper(responses, dry_run=True)
    now = datetime.datetime.now(datetime.timezone.utc)
    end = now + datetime.timedelta(seconds=1.2)
    responses.add(responses.POST, f"{API}/Favorite/GetAllFavoriteItemsByType",
                  json=favorites_payload(1, end, 40))
    responses.add(responses.GET, f"{API}/itemDetail/GetItemDetailModelByItemId/1",
                  json=item_payload(end, minimumBid=10.0))
    asyncio.run(b.place_bid(1, end))
    assert not [c for c in responses.calls if "PlaceBid" in c.request.url]
    # prove it got past the priced-out check to the dry-run branch
    assert "DRY-RUN" in caplog.text

@responses.activate
def test_priced_out_skips_bid(make_sniper):
    b = make_sniper(responses)
    now = datetime.datetime.now(datetime.timezone.utc)
    end = now + datetime.timedelta(seconds=1.2)
    responses.add(responses.POST, f"{API}/Favorite/GetAllFavoriteItemsByType",
                  json=favorites_payload(1, end, 40))
    responses.add(responses.GET, f"{API}/itemDetail/GetItemDetailModelByItemId/1",
                  json=item_payload(end, minimumBid=752.0))
    asyncio.run(b.place_bid(1, end))
    assert not [c for c in responses.calls if "PlaceBid" in c.request.url]
