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


@pytest.fixture(autouse=True)
def no_real_network(monkeypatch):
    """
    Tests must never reach the internet. HTTP mocks only cover the code under
    test, and a background thread (eg. the notifier) can outlive a test's mock,
    so block real connections at the socket: anything that isn't localhost fails.
    """

    import socket

    import requests.adapters

    # Behind a proxy every connection goes to the proxy (often localhost), which
    # would slip past the socket check, so drop proxy settings for tests.
    for var in ("HTTPS_PROXY", "HTTP_PROXY", "ALL_PROXY", "https_proxy", "http_proxy", "all_proxy"):
        monkeypatch.delenv(var, raising=False)

    # The `responses` mock replaces HTTPAdapter.send while active and restores
    # this one when it stops, so any request made outside a mock lands here.
    def no_send(self, request, *a, **k):
        raise RuntimeError(f"test tried a real network connection to {request.url!r}")

    monkeypatch.setattr(requests.adapters.HTTPAdapter, "send", no_send)

    real_connect = socket.socket.connect

    def guarded(self, address, *a, **k):
        host = address[0] if isinstance(address, tuple) else address
        if host not in ("127.0.0.1", "::1", "localhost"):
            raise RuntimeError(f"test tried a real network connection to {address!r}")
        return real_connect(self, address, *a, **k)

    monkeypatch.setattr(socket.socket, "connect", guarded)
    real_create = socket.create_connection

    def guarded_create(address, *a, **k):
        if address[0] not in ("127.0.0.1", "::1", "localhost"):
            raise RuntimeError(f"test tried a real network connection to {address!r}")
        return real_create(address, *a, **k)

    monkeypatch.setattr(socket, "create_connection", guarded_create)
    yield
