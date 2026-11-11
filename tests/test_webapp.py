import datetime, json
import pytest, responses
from conftest import API, item_payload, pt_str
import shopgoodwill as sgwmod
import webapp, watchlist

ACTIVE = {}


@pytest.fixture
def client(tmp_path):
    rsps = responses.RequestsMock(assert_all_requests_are_fired=False)
    rsps.start()
    ACTIVE["rsps"] = rsps
    rsps.add(responses.GET, sgwmod.Shopgoodwill.LOGIN_PAGE_URL, body="ok")
    rsps.add(responses.POST, API + "/SignIn/Login", json={"message": "ok", "accessToken": "t"})
    end = datetime.datetime.now(datetime.timezone.utc) + datetime.timedelta(hours=1)
    notes = {"7": json.dumps({"max_bid": 9.0})}
    def favs(req):
        data = [{"itemId": int(k), "title": f"Item {k}", "sellerId": 148, "endTime": pt_str(end),
                 "watchlistId": int(k) * 10, "notes": v} for k, v in notes.items()]
        return (200, {}, json.dumps({"data": data}))
    rsps.add_callback(responses.POST, API + "/Favorite/GetAllFavoriteItemsByType", callback=favs)
    def add_fav(req):
        iid = req.url.split("itemId=")[1]; notes.setdefault(iid, ""); return (200, {}, "{}")
    rsps.add_callback(responses.GET, API + "/Favorite/AddToFavorite", callback=add_fav)
    def save_note(req):
        body = json.loads(req.body)
        notes[str(body["watchlistId"] // 10)] = body["notes"]; return (200, {}, "{}")
    rsps.add_callback(responses.POST, API + "/Favorite/Save", callback=save_note)
    def item(req):
        iid = int(req.url.rstrip("/").split("/")[-1])
        d = item_payload(end, minimumBid=8.0, currentPrice=7.0, itemId=iid, isAuction=(iid != 555555))
        return (200, {}, json.dumps(d))
    import re
    rsps.add_callback(responses.GET, re.compile(API + r"/itemDetail/GetItemDetailModelByItemId/\d+"), callback=item)
    cfg = {"auth_info": {"username": "me@example.com", "password": "secret"},
           "bid_sniper": {"bid_snipe_time_delta": "5 seconds", "alert_time_deltas": ["1 hour"]}}
    path = tmp_path / "config.json"; path.write_text(json.dumps(cfg))
    sa = webapp.SniperApp(str(path))
    app = webapp.create_app(sa)
    yield app.test_client(), sa, notes, path
    sa.notify_listener.stop()  # drain notifications while the mock is still active
    rsps.stop(); rsps.reset()

def test_parse_item_id():
    assert webapp.parse_item_id("https://shopgoodwill.com/item/282679478") == 282679478
    assert webapp.parse_item_id("https://shopgoodwill.com/item/282679478?ref=x") == 282679478
    assert webapp.parse_item_id(" 282679478 ") == 282679478
    assert webapp.parse_item_id("hello") is None

def test_index_served(client):
    c, *_ = client
    r = c.get("/"); assert r.status_code == 200 and b"Goodwill Sniper" in r.data

def test_state_has_tracked_item_with_status(client):
    c, sa, notes, _ = client
    s = c.get("/api/state").get_json()
    assert s["dry_run"] is True
    it = s["items"][0]
    assert it["id"] == 7 and it["max_bid"] == 9.0 and it["label"] == "Quiet" and it["pct_of_max"] == 78
    assert s["clock_offset_seconds"] is not None

def test_add_edit_cancel(client):
    c, sa, notes, _ = client
    r = c.post("/api/items", json={"item": "https://shopgoodwill.com/item/123456789", "max_bid": "12.50", "alerts": True})
    assert r.get_json()["ok"], r.get_json()
    assert watchlist.parse_note(notes["123456789"]) == {"max_bid": 12.5, "alerts": True}
    r = c.post("/api/items/123456789", json={"max_bid": 15, "alerts": False})
    assert watchlist.parse_note(notes["123456789"]) == {"max_bid": 15.0, "alerts": False}
    r = c.post("/api/items/123456789", json={"max_bid": None, "alerts": True})
    assert watchlist.parse_note(notes["123456789"]) == {"max_bid": None, "alerts": True}

def test_add_rejects_bad_input(client):
    c, *_ = client
    assert c.post("/api/items", json={"item": "nope"}).status_code == 400
    assert c.post("/api/items", json={"item": "123456", "max_bid": -1}).status_code == 400
    r = c.post("/api/items", json={"item": "555555", "max_bid": 5})
    assert r.status_code == 400 and "isn't an auction" in r.get_json()["error"]

def test_settings_validate_apply_and_keep_credentials(client):
    c, sa, notes, path = client
    good = {"snipe_seconds": 6, "alert_times": ["1 hour", " 5 minutes", ""], "contest_window_minutes": 20,
            "contest_min_bids": 4, "contest_min_bidders": 2, "ntfy_topic": "sgw-abc123", "ntfy_server": "https://ntfy.sh", "windows_toast": False}
    r = c.post("/api/settings", json=good); assert r.get_json()["ok"], r.get_json()
    assert sa.sniper.bid_time_delta.total_seconds() == 6
    assert [d.total_seconds() for d in sa.sniper.alert_time_deltas] == [3600, 300]
    assert sa.sniper.contest_settings()["min_bids"] == 4
    saved = json.loads(path.read_text())
    assert saved["auth_info"] == {"username": "me@example.com", "password": "secret"}
    assert saved["notifications"]["ntfy_topic"] == "sgw-abc123"
    assert c.post("/api/settings", json=dict(good, alert_times=["banana"])).status_code == 400
    assert c.post("/api/settings", json=dict(good, ntfy_topic="bad topic!")).status_code == 400
    assert c.post("/api/settings", json=dict(good, snipe_seconds=0)).status_code == 400

def test_dry_run_toggle(client):
    c, sa, *_ = client
    c.post("/api/dry-run", json={"enabled": False}); assert sa.sniper.dry_run is False
    c.post("/api/dry-run", json={"enabled": True}); assert sa.sniper.dry_run is True and sa.sniper.dry_run_msg == "DRY-RUN: "

def test_test_notify_reports(client):
    c, *_ = client
    r = c.post("/api/test-notify", json={}).get_json()
    assert r["result"]["ntfy"] == "off"

def test_http_errors_logged_once_with_evidence(client):
    c, sa, notes, _ = client
    import requests
    from requests.exceptions import HTTPError
    resp = requests.models.Response(); resp.status_code = 403; resp.reason = "Forbidden"
    resp._content = b"blocked"; resp.headers["Retry-After"] = "60"; resp.url = "https://x/api/y"
    resp.request = requests.Request("GET", "https://x/api/y").prepare()
    err = HTTPError(response=resp)
    for _ in range(5):
        sa.log_http_error("load item 9", err)
    lines = [e["message"] for e in sa.ring.recent() if "Couldn't load item 9" in e["message"]]
    assert len(lines) == 1 and "HTTP 403 Forbidden" in lines[0] and "retry-after=60" in lines[0]

def test_add_retries_once_on_403(client, monkeypatch):
    c, sa, notes, _ = client
    import webapp as wa
    monkeypatch.setattr(wa.time, "sleep", lambda s: None)
    calls = {"n": 0}
    real = sa.client.get_item_info
    def flaky(item_id):
        calls["n"] += 1
        if calls["n"] == 1:
            import requests
            from requests.exceptions import HTTPError
            r = requests.models.Response(); r.status_code = 403; r.url = "https://x"; r._content = b""
            raise HTTPError(response=r)
        return real(item_id)
    monkeypatch.setattr(sa.client, "get_item_info", flaky)
    r = c.post("/api/items", json={"item": "222333444", "max_bid": 5})
    assert r.get_json()["ok"] and calls["n"] == 2

def test_tie_breaker_setting(client):
    c, sa, notes, path = client
    base = {"snipe_seconds": 5, "alert_times": ["1 hour"], "contest_window_minutes": 30,
            "contest_min_bids": 3, "contest_min_bidders": 2, "ntfy_topic": "", "ntfy_server": "https://ntfy.sh", "windows_toast": False}
    assert c.post("/api/settings", json=dict(base, tie_breaker=37)).status_code == 400
    assert c.post("/api/settings", json=dict(base, tie_breaker=-1)).status_code == 400
    r = c.post("/api/settings", json=dict(base, tie_breaker=0.37)).get_json()
    assert r["ok"] and r["settings"]["tie_breaker"] == 0.37 and json.loads(path.read_text())["bid_sniper"]["tie_breaker"] == 0.37
    item = c.get("/api/state").get_json()["items"][0]
    assert item["max_bid"] == 9.0 and item["bid_amount"] == 9.37

def _ebay_client(tmp_path, live=True):
    import webapp as wa, bidslammer as bs
    cfg = {"auth_info": {"username": "u", "password": "p"}, "bid_sniper": {"tie_breaker": 0.37},
           "bidslammer": {"api_key": "k"}}
    path = tmp_path / "config.json"; path.write_text(json.dumps(cfg))
    sa = wa.SniperApp(str(path), live=live)
    CREATED.append(sa)
    return wa.create_app(sa).test_client(), sa, bs


CREATED = []


@pytest.fixture(autouse=True)
def _stop_created_listeners():
    yield
    while CREATED:
        CREATED.pop().notify_listener.stop()

def test_ebay_add_needs_confirm_then_places(client, tmp_path):
    import os, copy, time as _t, bidslammer as bs
    fx = json.load(open(os.path.join(os.path.dirname(__file__), "fixtures", "bidslammer_responses.json")))
    item = copy.deepcopy(fx["fetch_item_live"]); item.update(endTimeStamp=_t.time() + 7200)
    m = ACTIVE["rsps"]
    m.add(responses.GET, bs.API_ROOT + "fetchItem", json=item)
    m.add(responses.GET, bs.API_ROOT + "addBid", json=dict(fx["add_ok"], bid="20.37"))
    st = copy.deepcopy(fx["status_pending"]); st["item_info"]["bid"] = "20.37"
    m.add(responses.GET, bs.API_ROOT + "checkSnipeStatus", json=st)
    c, sa, _ = _ebay_client(tmp_path, live=True)
    r = c.post("/api/items", json={"item": "https://www.ebay.com/itm/880087287709", "max_bid": 20}).get_json()
    assert r["needs_confirm"] and "Bid: $20.37 (max $20.00 + tie-breaker $0.37)" in r["confirm_text"]
    assert not [x for x in m.calls if "addBid" in x.request.url]
    r = c.post("/api/items", json={"item": "https://www.ebay.com/itm/880087287709", "max_bid": 20, "confirm": True}).get_json()
    assert r["ok"] and r["placed"]["bid"] == 20.37
    assert sa.ebay.store.get(880087287709)["max_bid"] == 20.0

def test_ebay_refused_in_dry_run(client, tmp_path):
    c, sa, _ = _ebay_client(tmp_path, live=False)
    r = c.post("/api/items", json={"item": "880087287709", "max_bid": 20, "confirm": True})
    assert r.status_code == 400 and "can't be dry-run" in r.get_json()["error"]

def test_ebay_not_configured(client):
    c, sa, notes, _ = client
    r = c.post("/api/items", json={"item": "880087287709", "max_bid": 20})
    assert r.status_code == 400 and "BidSlammer API key" in r.get_json()["error"]

def test_separate_lead_times(client, tmp_path):
    import os, copy, time as _t, bidslammer as bs
    c, sa, _ = _ebay_client(tmp_path, live=True)
    base = {"snipe_seconds": 3, "alert_times": ["1 hour"], "contest_window_minutes": 30, "contest_min_bids": 3,
            "contest_min_bidders": 2, "ntfy_topic": "", "ntfy_server": "https://ntfy.sh", "windows_toast": False, "tie_breaker": 0}
    for bad in (0, 16, 4.5):
        r = c.post("/api/settings", json=dict(base, ebay_snipe_seconds=bad))
        assert r.status_code == 400 and "eBay snipe time" in r.get_json()["error"]
    r = c.post("/api/settings", json=dict(base, ebay_snipe_seconds=5)).get_json()
    assert r["ok"] and r["settings"]["snipe_seconds"] == 3 and r["settings"]["ebay_snipe_seconds"] == 5
    assert sa.sniper.bid_time_delta.total_seconds() == 3 and sa.ebay.delay() == 5
    fx = json.load(open(os.path.join(os.path.dirname(__file__), "fixtures", "bidslammer_responses.json")))
    item = copy.deepcopy(fx["fetch_item_live"]); item.update(endTimeStamp=_t.time() + 7200)
    ACTIVE["rsps"].add(responses.GET, bs.API_ROOT + "fetchItem", json=item)
    pre = c.post("/api/items", json={"item": "880087287709", "max_bid": 20}).get_json()
    assert pre["preview"]["delay"] == 5 and "Fires 5 seconds before the end" in pre["confirm_text"]

def test_ebay_lead_time_defaults_to_5(client, tmp_path):
    c, sa, _ = _ebay_client(tmp_path, live=True)
    assert sa.ebay.delay() == 5 and c.get("/api/state").get_json()["settings"]["ebay_snipe_seconds"] == 5

def _add_remove_mock(notes, works=True, status=200):
    def remove(req):
        iid = req.url.split("itemId=")[1]
        if works and status == 200:
            notes.pop(iid, None)
        return (status, {}, "")
    ACTIVE["rsps"].add_callback(responses.GET, API + "/Favorite/RemoveItemFromFavoriteList", callback=remove)

def test_delete_removes_favorite_and_confirms(client):
    c, sa, notes, _ = client
    _add_remove_mock(notes)
    assert c.get("/api/state").get_json()["items"][0]["id"] == 7
    r = c.post("/api/items/7/delete", json={})
    assert r.get_json()["ok"] and "7" not in notes
    calls = [x.request.url for x in ACTIVE["rsps"].calls if "RemoveItemFromFavoriteList" in x.request.url]
    assert calls == [API + "/Favorite/RemoveItemFromFavoriteList?itemId=7"]
    assert c.get("/api/state").get_json()["items"] == []
    assert any("Stopped tracking 'Item 7'" in e["message"] for e in sa.ring.recent())

def test_delete_reports_when_item_survives(client):
    c, sa, notes, _ = client
    _add_remove_mock(notes, works=False)
    r = c.post("/api/items/7/delete", json={})
    assert r.status_code == 400 and "still in your favorites" in r.get_json()["error"] and "7" in notes

def test_delete_reports_http_failure(client, monkeypatch):
    c, sa, notes, _ = client
    import webapp as wa
    monkeypatch.setattr(wa.time, "sleep", lambda s: None)
    _add_remove_mock(notes, status=500)
    r = c.post("/api/items/7/delete", json={})
    assert r.status_code == 400 and "HTTP 500" in r.get_json()["error"]
