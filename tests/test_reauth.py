
import responses, pytest
from requests.exceptions import HTTPError
import shopgoodwill as sgwmod
API = sgwmod.Shopgoodwill.API_ROOT

def _login_mocks(rsps, tokens):
    rsps.add(responses.GET, sgwmod.Shopgoodwill.LOGIN_PAGE_URL, body="ok")
    for t in tokens:
        rsps.add(responses.POST, API + "/SignIn/Login", json={"message": "ok", "accessToken": t})

@responses.activate
def test_401_triggers_relogin_and_retry():
    _login_mocks(responses, ["tok1", "tok2"])
    responses.add(responses.GET, sgwmod.Shopgoodwill.LOGIN_PAGE_URL, body="ok")
    responses.add(responses.POST, API + "/ItemBid/PlaceBid", status=401)
    responses.add(responses.POST, API + "/ItemBid/PlaceBid", json={"message": "Bid Received!"})
    c = sgwmod.Shopgoodwill({"username": "u", "password": "p"})
    custom_hook_calls = []
    def hook(r, *a, **k):
        custom_hook_calls.append(r.status_code); r.raise_for_status()
    c.shopgoodwill_session.hooks["response"] = hook
    c.place_bid(1, 5.0, 2)
    bids = [x for x in responses.calls if x.request.url.endswith("PlaceBid")]
    assert len(bids) == 2
    assert bids[0].request.headers["Authorization"] == "Bearer tok1"
    assert bids[1].request.headers["Authorization"] == "Bearer tok2"
    # the bid_sniper-style hook must survive the re-login
    assert c.shopgoodwill_session.hooks["response"] is hook
    assert 401 in custom_hook_calls

@responses.activate
def test_token_only_does_not_retry():
    responses.add(responses.POST, API + "/SaveSearches/GetSaveSearches", json={"data": []})
    responses.add(responses.POST, API + "/ItemBid/PlaceBid", status=401)
    c = sgwmod.Shopgoodwill({"access_token": "abc"})
    with pytest.raises(HTTPError):
        c.place_bid(1, 5.0, 2)

@responses.activate
def test_second_401_is_raised_not_looped():
    _login_mocks(responses, ["tok1", "tok2"])
    responses.add(responses.GET, sgwmod.Shopgoodwill.LOGIN_PAGE_URL, body="ok")
    responses.add(responses.POST, API + "/ItemBid/PlaceBid", status=401)
    c = sgwmod.Shopgoodwill({"username": "u", "password": "p"})
    with pytest.raises(HTTPError):
        c.place_bid(1, 5.0, 2)
    assert len([x for x in responses.calls if x.request.url.endswith("PlaceBid")]) == 2
