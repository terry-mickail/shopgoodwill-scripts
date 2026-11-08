import sys, json, copy, datetime
import os
HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))
import pytest, responses
from zoneinfo import ZoneInfo
import shopgoodwill as sgwmod
import bid_sniper
API = sgwmod.Shopgoodwill.API_ROOT
REAL_ITEM = json.load(open(os.path.join(HERE, "fixtures", "item_detail_sample.json")))

def pt_str(dt_utc):
    """UTC datetime -> SGW-style naive Pacific string with 2 fractional digits"""
    s = dt_utc.astimezone(ZoneInfo("America/Los_Angeles")).strftime("%Y-%m-%dT%H:%M:%S.%f")
    return s[:-4]

@pytest.fixture
def make_sniper():
    def _make(rsps, extra_cfg=None, dry_run=False):
        rsps.add(responses.GET, sgwmod.Shopgoodwill.LOGIN_PAGE_URL, body="ok")
        rsps.add(responses.POST, API + "/SignIn/Login", json={"message": "ok", "accessToken": "t"})
        cfg = {"auth_info": {"username": "u", "password": "p"},
               "bid_sniper": {"bid_snipe_time_delta": "1 seconds", "bid_preflight_seconds": 1},
               "logging": {"log_level": "DEBUG"}}
        if extra_cfg: cfg["bid_sniper"].update(extra_cfg)
        return bid_sniper.BidSniper(cfg, dry_run=dry_run)
    return _make

def item_payload(end_utc, server_offset_s=0.0, **overrides):
    d = copy.deepcopy(REAL_ITEM)
    now = datetime.datetime.now(datetime.timezone.utc)
    d["serverTime"] = pt_str(now + datetime.timedelta(seconds=server_offset_s))
    d["endTime"] = pt_str(end_utc)
    d.update(overrides)
    return d

def favorites_payload(item_id, end_utc, max_bid):
    return {"data": [{"itemId": item_id, "title": "Test Amp", "sellerId": 148,
                      "endTime": pt_str(end_utc), "watchlistId": 9,
                      "notes": json.dumps({"max_bid": max_bid})}]}
