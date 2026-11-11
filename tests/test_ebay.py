import copy, datetime, json, logging, os, time
import pytest, responses
import bidslammer as bs, ebay_desk as ed
from bid_sniper import BidSniper

FX = json.load(open(os.path.join(os.path.dirname(__file__), "fixtures", "bidslammer_responses.json")))
R = bs.API_ROOT
ITEM = 880087287709

def live_item(minutes=120, minimum=13, listing="Chinese"):
    d = copy.deepcopy(FX["fetch_item_live"])
    d.update(endTimeStamp=time.time() + minutes * 60, minimumToBid=minimum, listingType=listing)
    return d

def make_desk(tmp_path, dry=False, tie=0.0, lead=5, deltas=(3600, 600, 60)):
    lg = logging.getLogger("test_ebay"); lg.setLevel(logging.DEBUG)
    return ed.EbayDesk(
        client=bs.BidSlammer("k"), store=ed.EbayStore(str(tmp_path / "ebay_snipes.json")), logger=lg,
        notify_extra=BidSniper.notify_extra, tie_breaker=lambda: tie, lead_seconds=lambda: lead,
        is_dry_run=lambda: dry, contest_settings=lambda: {"window_minutes": 30, "min_bids": 3, "min_bidders": 2},
        alert_deltas=lambda: [datetime.timedelta(seconds=d) for d in deltas])

def test_parse_ebay_item():
    p = ed.parse_ebay_item
    assert p("https://www.ebay.com/itm/880087287709") == ITEM
    assert p("https://www.ebay.com/itm/Raspberry-Pi-5-8GB-RAM-/267829421546?hash=x") == 267829421546
    assert p("ebay.co.uk/itm/123456789012") == 123456789012
    assert p("880087287709") == ITEM
    assert p("283277658") is None and p("https://shopgoodwill.com/item/283277658") is None

@responses.activate
def test_preview_refuses_in_dry_run(tmp_path):
    with pytest.raises(ValueError, match="can't be dry-run"):
        make_desk(tmp_path, dry=True).preview(ITEM, 20)
    assert not responses.calls

@responses.activate
def test_preview_checks(tmp_path):
    d = make_desk(tmp_path, tie=0.37, lead=3)
    responses.add(responses.GET, R + "fetchItem", json=live_item())
    p = d.preview(ITEM, 20)
    assert p["bid"] == 20.37 and p["tie_breaker"] == 0.37 and p["delay"] == 3 and p["minimum"] == 13
    responses.replace(responses.GET, R + "fetchItem", json=live_item(minimum=25))
    with pytest.raises(ValueError, match=r"below eBay's current minimum \$25.00"):
        d.preview(ITEM, 20)
    responses.replace(responses.GET, R + "fetchItem", json=live_item(listing="FixedPriceItem"))
    with pytest.raises(ValueError, match="isn't an auction"):
        d.preview(ITEM, 20)
    responses.replace(responses.GET, R + "fetchItem", json=live_item(minutes=0.5))
    with pytest.raises(ValueError, match="ends within a minute"):
        d.preview(ITEM, 20)

def test_lead_time_clamped(tmp_path):
    assert make_desk(tmp_path, lead=0.2).delay() == 1 and make_desk(tmp_path, lead=30).delay() == 15

@responses.activate
def test_place_sends_verifies_and_stores(tmp_path, caplog):
    d = make_desk(tmp_path, tie=0.37)
    responses.add(responses.GET, R + "fetchItem", json=live_item())
    responses.add(responses.GET, R + "addBid", json=dict(FX["add_ok"], bid="20.37"))
    st = copy.deepcopy(FX["status_pending"]); st["item_info"]["bid"] = "20.37"
    responses.add(responses.GET, R + "checkSnipeStatus", json=st)
    caplog.set_level(logging.INFO)
    d.place(ITEM, 20, alerts=False)
    add = [c for c in responses.calls if "addBid" in c.request.url][0].request.url
    assert "bid=20.37" in add and "delay=5" in add and "enabled" not in add
    assert d.store.get(ITEM) == {"max_bid": 20.0, "alerts": False, "title": FX["fetch_item_live"]["title"]}
    assert "bids 20.37 (max 20.00 + tie-breaker 0.37)" in caplog.text

@responses.activate
def test_place_detects_wrong_stored_amount(tmp_path):
    d = make_desk(tmp_path)
    responses.add(responses.GET, R + "fetchItem", json=live_item())
    responses.add(responses.GET, R + "addBid", json=dict(FX["add_ok"], bid="20.00"))
    responses.add(responses.GET, R + "checkSnipeStatus", json=FX["status_pending"])  # shows 13.00
    with pytest.raises(bs.BidSlammerError, match="now shows 13.0, not 20.00"):
        d.place(ITEM, 20)
    assert d.store.get(ITEM) == {}

@responses.activate
def test_rows_have_contest_label_and_end(tmp_path):
    d = make_desk(tmp_path)
    d.store.put(ITEM, max_bid=12.63, alerts=True)
    responses.add(responses.GET, R + "getUserItems", json=FX["list_current_one"])
    item = live_item(minutes=90)
    responses.add(responses.GET, R + "fetchItem", json=item)
    responses.add(responses.GET, R + "getAllBidders", json=FX["bidders"])
    row = d.rows()[0]
    assert row["site"] == "ebay" and row["bid_amount"] == 13.0 and row["max_bid"] == 12.63
    assert row["label"] in ("Quiet", "Active", "Contested") and row["total_bids"] == item["bidCount"]
    end = datetime.datetime.fromisoformat(row["end_utc"])
    assert abs(end.timestamp() - item["endTimeStamp"]) < 1

def test_contest_input_reads_bid_times_as_pacific():
    now = datetime.datetime(2026, 11, 10, 17, 0, tzinfo=datetime.timezone.utc)  # 09:00 PST
    bids = [{"bidTime": "2026-11-10 08:50:00", "userID": "a"}, {"bidTime": "2026-11-10 08:45:00", "userID": "b"},
            {"bidTime": "2026-11-10 08:40:00", "userID": "a"}]
    import watchlist
    s = watchlist.contest_status(ed.contest_input({"bidCount": 3, "currentPrice": 10, "minimumToBid": 11}, bids, now), max_bid=20)
    assert s["label"] == "Contested" and s["recent_bids"] == 3

@responses.activate
def test_time_alerts_fire_once_and_never_late(tmp_path, caplog):
    d = make_desk(tmp_path, deltas=(3600, 600))
    d.store.put(ITEM, max_bid=13, alerts=True)
    snipe = dict(FX["list_current_one"][0], overtime=str(-590))  # 9m50s left
    responses.add(responses.GET, R + "getUserItems", json=[snipe])
    responses.add(responses.GET, R + "fetchItem", json=live_item(minutes=10))
    responses.add(responses.GET, R + "getAllBidders", json=FX["bidders"])
    d._last_completed = time.time()  # skip results this pass
    caplog.set_level(logging.INFO)
    d.watch_once(); d.watch_once()
    alerts = [r for r in caplog.records if "Time alert (eBay)" in r.getMessage()]
    assert len(alerts) == 1 and "0:09:" in alerts[0].getMessage()  # the 10-minute alert, once
    assert alerts[0].notify_url == "https://www.ebay.com/itm/880087287709"
    assert not any("1:00:00" in r.getMessage() for r in caplog.records)  # 1-hour alert not sent late

@responses.activate
def test_alerts_off_means_no_alert(tmp_path, caplog):
    d = make_desk(tmp_path, deltas=(600,))
    d.store.put(ITEM, max_bid=13, alerts=False)
    responses.add(responses.GET, R + "getUserItems", json=[dict(FX["list_current_one"][0], overtime="-590")])
    d._last_completed = time.time()
    d.watch_once()
    assert "Time alert" not in caplog.text

@responses.activate
def test_results_first_run_silent_then_reports(tmp_path, caplog):
    d = make_desk(tmp_path)
    old = dict(FX["list_current_one"][0], item_no="111111111111", snipe_id="1", overtime="9000")
    responses.add(responses.GET, R + "getUserItems", json=[old])
    caplog.set_level(logging.INFO)
    d._results()
    assert "WON" not in caplog.text and "Lost" not in caplog.text and d.store.data["initialized"]

    won = dict(FX["list_current_one"][0], item_no=str(ITEM), snipe_id="2", overtime="400", cost="12.75")
    lost = dict(FX["list_current_one"][0], item_no="222222222222", snipe_id="3", overtime="400", cost="30.00")
    fresh = dict(FX["list_current_one"][0], item_no="333333333333", snipe_id="4", overtime="100")
    pending = dict(FX["list_current_one"][0], item_no="444444444444", snipe_id="5", overtime="400")
    responses.replace(responses.GET, R + "getUserItems", json=[old, won, lost, fresh, pending])
    def status(req):
        n = req.url.split("item_no=")[1]
        body = copy.deepcopy(FX["status_pending"])
        body["bidResult"] = {"880087287709": {"success": 1, "status": "Won"},
                             "222222222222": {"success": -7, "status": "Minimum Bid Increment Not Met."}}.get(n, [])
        return (200, {}, json.dumps(body))
    responses.add_callback(responses.GET, R + "checkSnipeStatus", callback=status)
    d._results()
    text = caplog.text
    assert "WON on eBay" in text and "for 12.75 (your bid 13.00)" in text
    assert "Lost on eBay" in text and "Minimum Bid Increment Not Met." in text
    assert "333333333333" not in [c.request.url.split("item_no=")[-1] for c in responses.calls if "checkSnipeStatus" in c.request.url]
    n_before = text.count("WON on eBay")
    d._results()
    assert caplog.text.count("WON on eBay") == n_before  # reported once

@responses.activate
def test_row_shows_its_lead_time(tmp_path):
    d = make_desk(tmp_path)
    responses.add(responses.GET, R + "getUserItems", json=[dict(FX["list_current_one"][0], delay="7")])
    responses.add(responses.GET, R + "fetchItem", json=live_item())
    responses.add(responses.GET, R + "getAllBidders", json=FX["bidders"])
    assert d.rows()[0]["lead_seconds"] == 7.0
