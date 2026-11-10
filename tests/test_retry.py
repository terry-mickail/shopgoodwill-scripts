import asyncio, datetime
import responses
from conftest import API, item_payload, favorites_payload

HIGH = "<h3><b>Bid Received! </b></h3><p>You are <strong>currently</strong> the high bidder for this auction. </p>"

def _setup(make_sniper, bid_statuses, end_in=1.5, delta="1 seconds"):
    b = make_sniper(responses, extra_cfg={"result_check_delay_seconds": 30, "bid_snipe_time_delta": delta})
    end = datetime.datetime.now(datetime.timezone.utc) + datetime.timedelta(seconds=end_in)
    responses.add(responses.POST, f"{API}/Favorite/GetAllFavoriteItemsByType", json=favorites_payload(1, end, 20))
    responses.add(responses.GET, f"{API}/itemDetail/GetItemDetailModelByItemId/1", json=item_payload(end, minimumBid=8.0))
    for st in bid_statuses:
        if st == 200:
            responses.add(responses.POST, f"{API}/ItemBid/PlaceBid", json={"message": HIGH})
        else:
            responses.add(responses.POST, f"{API}/ItemBid/PlaceBid", status=st, body="nope")
    return b, end

def _bids():
    return [c for c in responses.calls if "PlaceBid" in c.request.url]

async def _run(b, end):
    await b.place_bid(1, end)

@responses.activate
def test_403_then_success(make_sniper, caplog):
    b, end = _setup(make_sniper, [403, 200], end_in=2.5, delta="2 seconds")
    asyncio.run(_run(b, end))
    assert len(_bids()) == 2 and "retrying once" in caplog.text and "high bidder" in caplog.text

@responses.activate
def test_403_twice_fails(make_sniper, caplog):
    b, end = _setup(make_sniper, [403, 403], end_in=2.5, delta="2 seconds")
    asyncio.run(_run(b, end))
    assert len(_bids()) == 2 and "FAILED - POST" in caplog.text and "HTTP 403" in caplog.text

@responses.activate
def test_permanent_error_not_retried(make_sniper, caplog):
    b, end = _setup(make_sniper, [400], end_in=2.5, delta="2 seconds")
    asyncio.run(_run(b, end))
    assert len(_bids()) == 1 and "retrying" not in caplog.text

@responses.activate
def test_no_retry_in_last_second(make_sniper, caplog):
    # fires 0.5s before end: not enough time left to retry
    b, end = _setup(make_sniper, [403, 200], end_in=1.3, delta="0.5 seconds")
    asyncio.run(_run(b, end))
    assert len(_bids()) == 1 and "FAILED" in caplog.text
