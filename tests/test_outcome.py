import asyncio, datetime, json
import responses
from conftest import API, item_payload, favorites_payload
import shopgoodwill as sgwmod

S = sgwmod.Shopgoodwill
HIGH = "<h3><b>Bid Received! </b></h3><p>You are <strong>currently</strong> the high bidder for this auction. </p><p>This item ends at 11/7/2026 9:19:00 PM PT, check back then for results. </p>"
OUTBID = "<h3><b>You have already been outbid. </b></h3><p>This occurred because someone specfied a higher maximum bid than you. </p>"

def test_classify_known_responses():
    assert S.classify_bid_response({"message": HIGH})["outcome"] == S.BID_HIGH_BIDDER
    assert S.classify_bid_response({"message": OUTBID})["outcome"] == S.BID_OUTBID
    closed = {"result": -3, "message": "Auction has closed. Auction ended as of 12/20/2022 7:01:00 PM PT", "status": False}
    assert S.classify_bid_response(closed)["outcome"] == S.BID_CLOSED
    r = S.classify_bid_response({"message": "<p>Something new</p>"})
    assert r["outcome"] == S.BID_UNKNOWN and r["message"] == "Something new"
    assert S.classify_bid_response(None)["outcome"] == S.BID_UNKNOWN

def _closed_item(end, me_won):
    d = item_payload(end, minimumBid=10.0)
    d["bidHistory"] = dict(d["bidHistory"], auctionClosed=True, itemCurrentPrice=8.5,
                           bidSummary=[{"bidderName": "x", "isLoggedInBuyer": me_won, "amount": 9.0}])
    return d

@responses.activate
def test_real_bid_reports_outcome_and_win(make_sniper, caplog):
    b = make_sniper(responses, extra_cfg={"result_check_delay_seconds": 0.2})
    now = datetime.datetime.now(datetime.timezone.utc)
    end = now + datetime.timedelta(seconds=1.2)
    responses.add(responses.POST, f"{API}/Favorite/GetAllFavoriteItemsByType", json=favorites_payload(1, end, 9))
    responses.add(responses.GET, f"{API}/itemDetail/GetItemDetailModelByItemId/1", json=item_payload(end, minimumBid=8.0))
    responses.add(responses.POST, f"{API}/ItemBid/PlaceBid", json={"message": HIGH})
    responses.add(responses.GET, f"{API}/itemDetail/GetItemDetailModelByItemId/1", json=_closed_item(end, True))
    async def run():
        await b.place_bid(1, end)
        await asyncio.sleep(0.6)
    asyncio.run(run())
    assert "you are the high bidder" in caplog.text
    assert "WON 'Test Amp' for 8.50" in caplog.text
    rec = [r for r in caplog.records if "WON" in r.getMessage()][0]
    assert rec.notify and rec.notify_url.endswith("/item/1")

@responses.activate
def test_outbid_then_lost(make_sniper, caplog):
    b = make_sniper(responses, extra_cfg={"result_check_delay_seconds": 0.2})
    end = datetime.datetime.now(datetime.timezone.utc) + datetime.timedelta(seconds=1.2)
    responses.add(responses.POST, f"{API}/Favorite/GetAllFavoriteItemsByType", json=favorites_payload(1, end, 9))
    responses.add(responses.GET, f"{API}/itemDetail/GetItemDetailModelByItemId/1", json=item_payload(end, minimumBid=8.0))
    responses.add(responses.POST, f"{API}/ItemBid/PlaceBid", json={"message": OUTBID})
    responses.add(responses.GET, f"{API}/itemDetail/GetItemDetailModelByItemId/1", json=_closed_item(end, False))
    async def run():
        await b.place_bid(1, end); await asyncio.sleep(0.6)
    asyncio.run(run())
    assert "already outbid" in caplog.text and "Lost 'Test Amp'" in caplog.text
