import asyncio, datetime, threading, time
import responses
from conftest import API, favorites_payload

@responses.activate
def test_request_refresh_wakes_main_loop(make_sniper):
    b = make_sniper(responses, extra_cfg={"refresh_seconds": 3600, "favorites_max_cache_seconds": 3600})
    calls = []
    def fav_cb(req):
        calls.append(time.time()); return (200, {}, '{"data": []}')
    responses.add_callback(responses.POST, f"{API}/Favorite/GetAllFavoriteItemsByType", callback=fav_cb)
    t = threading.Thread(target=b.start, daemon=True); t.start()
    time.sleep(0.5)
    assert len(calls) == 1  # startup read (proves the 1970 cache is treated as stale)
    b.request_refresh(); time.sleep(0.5)
    assert len(calls) == 2  # woke up despite refresh_seconds=3600
    b.event_loop.call_soon_threadsafe(b.event_loop.stop)
