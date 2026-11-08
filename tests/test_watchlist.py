import copy, datetime, json
import responses, asyncio
from conftest import REAL_ITEM, API, pt_str, item_payload, favorites_payload
import watchlist as w

def test_parse_note():
    assert w.parse_note(None) is None and w.parse_note("") is None
    assert w.parse_note("buy for mom") is None and w.parse_note("[1,2]") is None
    assert w.parse_note('{"max_bid": 9.0}') == {"max_bid": 9.0, "alerts": True}
    assert w.parse_note('{"max_bid": "12.5", "alerts": false}') == {"max_bid": 12.5, "alerts": False}
    assert w.parse_note('{"max_bid": 0}')["max_bid"] is None
    assert w.parse_note('{"max_bid": null}')["max_bid"] is None
    assert w.parse_note('{"max_bid": "abc"}')["max_bid"] is None
    assert w.parse_note(w.make_note(9.999, alerts=False)) == {"max_bid": 10.0, "alerts": False}
    assert w.parse_note(w.make_note(None))["max_bid"] is None
    assert len(w.make_note(123456.78)) < 256

def test_real_amp_is_quiet():
    # real capture: 10 bids, last one about 20 hours before serverTime
    s = w.contest_status(REAL_ITEM, max_bid=800)
    assert s["label"] == w.QUIET and s["total_bids"] == 10
    assert s["priced_out"] is False and s["pct_of_max"] == 94

def _item_with_bids(bids, server_now):
    d = copy.deepcopy(REAL_ITEM)
    d["serverTime"] = pt_str(server_now)
    d["numberOfBids"] = len(bids)
    d["bidHistory"]["bidComplete"] = [
        {"bidTime": pt_str(server_now - datetime.timedelta(minutes=m)), "bidderName": n,
         "isLoggedInBuyer": me, "retracted": False}
        for m, n, me in bids]
    return d

def test_labels():
    now = datetime.datetime(2026, 11, 7, 12, 0, tzinfo=datetime.timezone.utc)
    assert w.contest_status(_item_with_bids([], now))["label"] == w.UNCONTESTED
    assert w.contest_status(_item_with_bids([(5, "a", False)], now))["label"] == w.ACTIVE
    # 3 recent bids but one bidder: not contested
    one = [(1, "a", False), (2, "a", False), (3, "a", False)]
    assert w.contest_status(_item_with_bids(one, now))["label"] == w.ACTIVE
    war = [(1, "a", False), (2, "b", False), (3, "a", False)]
    assert w.contest_status(_item_with_bids(war, now))["label"] == w.CONTESTED
    # my own bids don't count toward a war
    mine = [(1, "a", False), (2, "me", True), (3, "me", True)]
    assert w.contest_status(_item_with_bids(mine, now))["label"] == w.ACTIVE
    old = [(45, "a", False), (50, "b", False), (55, "c", False)]
    assert w.contest_status(_item_with_bids(old, now))["label"] == w.QUIET
    assert w.contest_status(_item_with_bids(old, now), window_minutes=60)["label"] == w.CONTESTED

def test_describe_priced_out():
    d = copy.deepcopy(REAL_ITEM)
    s = w.contest_status(d, max_bid=9)
    text = w.describe(s, 9)
    assert "PRICED OUT" in text and "$751.00 (min $752.00)" in text

@responses.activate
def test_time_alert_respects_alerts_flag_and_adds_status(make_sniper, caplog):
    b = make_sniper(responses)
    end = datetime.datetime.now(datetime.timezone.utc) + datetime.timedelta(minutes=10)
    fav = favorites_payload(1, end, 9)
    fav["data"].append({"itemId": 2, "title": "Muted Item", "sellerId": 1, "endTime": pt_str(end),
                        "watchlistId": 3, "notes": json.dumps({"max_bid": 5, "alerts": False})})
    fav["data"].append({"itemId": 3, "title": "Untracked", "sellerId": 1, "endTime": pt_str(end),
                        "watchlistId": 4, "notes": "gift idea"})
    responses.add(responses.POST, f"{API}/Favorite/GetAllFavoriteItemsByType", json=fav)
    responses.add(responses.GET, f"{API}/itemDetail/GetItemDetailModelByItemId/1",
                  json=item_payload(end, numberOfBids=0, bidHistory=dict(REAL_ITEM["bidHistory"], bidComplete=[]),
                                    currentPrice=7.99, minimumBid=8.99))
    async def run():
        for i in (1, 2, 3):
            await b.time_alert(i, end)
    asyncio.run(run())
    alerts = [r.getMessage() for r in caplog.records if "Time alert" in r.getMessage()]
    assert len(alerts) == 1
    assert "Uncontested: no bids yet" in alerts[0] and "your max $9.00 (89%)" in alerts[0]
