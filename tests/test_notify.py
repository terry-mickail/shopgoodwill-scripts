import logging
import responses
import notify

def test_ntfy_headers_and_body():
    with responses.RequestsMock() as r:
        r.add(responses.POST, "https://ntfy.sh/sgw-abc", status=200)
        notify.send_ntfy("https://ntfy.sh/", "sgw-abc", "Ending in 0:10:00 — café", "Fossil café ™ watch", 4, "https://shopgoodwill.com/item/1")
        req = r.calls[0].request
        assert req.headers["X-Title"].isascii() and req.headers["X-Priority"] == "4"
        assert req.headers["X-Click"] == "https://shopgoodwill.com/item/1"
        assert req.body.decode("utf-8") == "Fossil café ™ watch"

def test_handler_filters_and_never_raises():
    with responses.RequestsMock(assert_all_requests_are_fired=False) as r:
        r.add(responses.POST, "https://ntfy.sh/t", status=500)
        log = logging.getLogger("test_notify_logger"); log.setLevel(logging.DEBUG); log.propagate = False
        cfg = {"notifications": {"ntfy_topic": "t", "windows_toast": False}}
        listener = notify.attach_notifiers(log, cfg)
        log.warning("plain warning, not for push")
        log.warning("bid placed", extra={"notify": True, "notify_title": "Bid", "notify_priority": 4})
        log.error("an error")
        listener.stop()
        assert len(r.calls) == 2  # the flagged warning and the error, both 500s, no exception
        assert r.calls[0].request.headers["X-Title"] == "Bid"

def test_deliver_reports_off_channels():
    res = notify.deliver({"ntfy_topic": "", "windows_toast": False}, "t", "m")
    assert res == {"ntfy": "off", "windows": "off"}
    res = notify.deliver({"ntfy_topic": "", "windows_toast": True}, "t", "m")
    assert res["windows"] == "not on Windows"
