import json, os, datetime
import pytest, responses
import bidslammer as bs

FX = json.load(open(os.path.join(os.path.dirname(__file__), "fixtures", "bidslammer_responses.json")))
R = bs.API_ROOT

def client():
    return bs.BidSlammer("k")

@responses.activate
def test_test_ok_and_bad_key():
    responses.add(responses.GET, R + "justTesting", json=FX["test_ok"])
    assert client().test()["status"] == "OK"
    assert responses.calls[0].request.url.startswith(R + "justTesting?key=k")
    responses.replace(responses.GET, R + "justTesting", json=FX["invalid_key"])
    with pytest.raises(bs.BidSlammerError, match="Invalid API key"):
        client().test()

@responses.activate
def test_fetch_item_live_and_unknown():
    responses.add(responses.GET, R + "fetchItem", json=FX["fetch_item_live"])
    it = client().fetch_item(880087287709)
    assert bs.is_auction(it) and it["minimumToBid"] == 13 and it["endTimeStamp"] > 0
    responses.replace(responses.GET, R + "fetchItem", json=FX["fetch_unknown"])
    with pytest.raises(bs.BidSlammerError):
        client().fetch_item(1)

@responses.activate
def test_add_snipe_ok_sends_exact_amount_no_enabled_flag():
    responses.add(responses.GET, R + "addBid", json=FX["add_ok"])
    out = client().add_snipe(880087287709, 13, delay=5)
    assert out["new_snipe"] is True
    q = responses.calls[0].request.url
    assert "bid=13.00" in q and "delay=5" in q and "force=0" in q and "enabled" not in q

@responses.activate
def test_add_snipe_below_minimum_raises_with_message():
    responses.add(responses.GET, R + "addBid", json=FX["add_below_min"])
    with pytest.raises(bs.BidSlammerError, match="Bid at least 148"):
        client().add_snipe(267829421546, 0.01, delay=10)

@responses.activate
def test_add_snipe_amount_mismatch_raises():
    bad = dict(FX["add_ok"], bid="14.00")
    responses.add(responses.GET, R + "addBid", json=bad)
    with pytest.raises(bs.BidSlammerError, match="recorded '14.00', not 13.00"):
        client().add_snipe(880087287709, 13, delay=5)

def test_delay_bounds():
    with pytest.raises(ValueError):
        client().add_snipe(1, 5, delay=0)
    with pytest.raises(ValueError):
        client().add_snipe(1, 5, delay=16)

@responses.activate
def test_list_snipes_pages_and_parses():
    one = FX["list_current_one"][0]
    page1 = [dict(one, item_no=str(i)) for i in range(20)]
    responses.add(responses.GET, R + "getUserItems", json=page1)
    responses.add(responses.GET, R + "getUserItems", json=[one])
    items = client().list_snipes("current")
    assert len(items) == 21 and "offset=20" in responses.calls[1].request.url
    assert bs.money(items[-1]["bid"]) == 13.0

@responses.activate
def test_list_empty_and_error_object():
    responses.add(responses.GET, R + "getUserItems", json=[])
    assert client().list_snipes() == []
    responses.replace(responses.GET, R + "getUserItems", json=FX["invalid_key"])
    with pytest.raises(bs.BidSlammerError, match="Invalid API key"):
        client().list_snipes()

@responses.activate
def test_status_pending_and_none():
    responses.add(responses.GET, R + "checkSnipeStatus", json=FX["status_pending"])
    st = client().snipe_status(880087287709)
    assert st["snipe"]["bid"] == "13.00" and bs.snipe_won(st) is None
    responses.replace(responses.GET, R + "checkSnipeStatus", json=FX["status_no_snipe"])
    assert client().snipe_status(1) is None

def test_snipe_won_verdicts():
    assert bs.snipe_won({"result": {"success": "1"}}) is True
    assert bs.snipe_won({"result": {"success": -7, "status": "Minimum Bid Increment Not Met."}}) is False
    assert bs.snipe_won({"result": []}) is None
    assert bs.snipe_won(None) is None

@responses.activate
def test_bidders_and_none():
    responses.add(responses.GET, R + "getAllBidders", json=FX["bidders"])
    b = client().bidders(880087287709)
    assert len(b) == 3 and b[0]["userID"] == "o***l" and b[0]["bidTime"] == "2026-11-10 08:39:41"
    responses.replace(responses.GET, R + "getAllBidders", json=FX["bidders_none"])
    assert client().bidders(1) == []

@responses.activate
def test_delete_ok_and_missing():
    responses.add(responses.GET, R + "deleteBid", json=FX["delete_ok"])
    assert client().delete_snipe(880087287709)["status"] == "OK"
    responses.replace(responses.GET, R + "deleteBid", json=FX["delete_missing"])
    with pytest.raises(bs.BidSlammerError):
        client().delete_snipe(880087287709)

def test_end_from_overtime():
    end = bs.end_utc_from_overtime("-3600", now=1_000_000)
    assert end == datetime.datetime.fromtimestamp(1_003_600, tz=datetime.timezone.utc)
    assert bs.end_utc_from_overtime(None) is None

def test_fixture_has_no_personal_ids():
    s = json.dumps(FX)
    assert "te_586154" not in s and "91611" not in s

@responses.activate
def test_round_trip_is_median_of_recent_calls():
    c = client()
    assert c.round_trip_ms() is None
    responses.add(responses.GET, R + "justTesting", json=FX["test_ok"])
    c.test(); c.test()
    assert isinstance(c.round_trip_ms(), (int, float))
    c._round_trips.clear(); c._round_trips.extend([100, 5000, 120])
    assert c.round_trip_ms() == 120  # one slow call doesn't skew it
