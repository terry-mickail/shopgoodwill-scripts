import re
import requests, responses
from requests.exceptions import HTTPError
import shopgoodwill as sgwmod
from conftest import API

def _err(status=403, headers=None, body="<html>Access Denied</html>"):
    with responses.RequestsMock() as r:
        r.add(responses.GET, API + "/itemDetail/GetItemDetailModelByItemId/1", status=status, body=body,
              headers=headers or {})
        resp = requests.get(API + "/itemDetail/GetItemDetailModelByItemId/1?x=secret")
    try:
        resp.raise_for_status()
    except HTTPError as e:
        return e

def test_describe_403_has_evidence():
    d = sgwmod.describe_error(_err(headers={"Retry-After": "30", "Server": "AkamaiGHost", "X-Unrelated": "no"}))
    assert d.startswith("GET https://buyerapi.shopgoodwill.com/api/itemDetail/GetItemDetailModelByItemId/1 -> HTTP 403")
    assert "retry-after=30" in d and "server=AkamaiGHost" in d and "body: <html>Access Denied</html>" in d
    assert "secret" not in d and "X-Unrelated" not in d

def test_describe_non_http():
    assert sgwmod.describe_error(ValueError("boom")) == "ValueError: boom"

def test_body_is_truncated():
    d = sgwmod.describe_error(_err(body="x" * 5000))
    assert len(d.split("body: ")[1]) == 200
