import requests, pytest
def test_guard_blocks():
    with pytest.raises(Exception, match="real network connection"):
        requests.get("https://ntfy.sh/", timeout=5)
