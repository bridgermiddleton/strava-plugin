"""Plugin-level tests with a fake display manager. Runs without the LEDMatrix
core (manager.py falls back to a stub BasePlugin)."""
import json
import time
from datetime import datetime, timedelta
from pathlib import Path

import pytest
from PIL import Image

import manager as M
import strava_api as A

FIX = json.loads((Path(__file__).parent / "fixtures" / "activities.json").read_text())


class FakeDisplay:
    def __init__(self, width=128, height=32):
        self.width, self.height = width, height
        self.image = Image.new("RGB", (width, height))
        self.draw = None
        self.updates = 0

    def update_display(self):
        self.updates += 1


class FakeCache:
    def __init__(self):
        self.store = {}

    def get(self, key, max_age=300, memory_ttl=None):
        return self.store.get(key)

    def set(self, key, data, ttl=None):
        self.store[key] = data


class FakeSession:
    """Serves the fixture activities for any athlete, re-dated to this week."""

    def __init__(self):
        self.calls = 0
        shift = datetime.now() - datetime.fromisoformat(FIX["now"])
        self.acts = {}
        for a in FIX["activities"]:
            a = dict(a)
            dt = datetime.fromisoformat(a["start_date_local"].rstrip("Z")) + shift
            a["start_date_local"] = dt.strftime("%Y-%m-%dT%H:%M:%SZ")
            self.acts.setdefault(str(a["athlete"]["id"]), []).append(a)

    def get(self, url, params=None, headers=None, timeout=None):
        self.calls += 1
        token = headers["Authorization"].split()[1]
        aid = token.split("-")[1]
        class Resp:
            status_code = 200
            headers = {"X-ReadRateLimit-Usage": "2,10", "X-ReadRateLimit-Limit": "100,1000"}
            def json(self_inner):
                return self.acts.get(aid, [])
        return Resp()

    def post(self, *a, **k):
        raise AssertionError("no token refresh expected")


def make_plugin(tmp_path, config=None, connected=True, size=(128, 32)):
    cfg = {
        "enabled": True, "client_id": "cid", "client_secret": "sec",
        "athletes": [
            {"name": "Bridger", "strava_id": "1001", "accessory": "headband"},
            {"name": "Roommate", "strava_id": "2002", "accessory": "cap"},
        ],
        "views": {"runs": {"seconds_per_run": 4, "max_seconds": 40}},
    }
    if config:
        cfg.update(config)
    store = A.TokenStore(tmp_path / "token.json")
    if connected:
        for aid in ("1001", "2002"):
            store.upsert({"refresh_token": "r", "access_token": f"tok-{aid}", "expires_at": time.time() + 9999,
                          "athlete": {"id": int(aid), "firstname": "X"}})
    M.TokenStore = lambda *a, **k: store  # plugin constructs its own store
    disp = FakeDisplay(*size)
    plugin = M.StravaPlugin("strava", cfg, disp, FakeCache(), None)
    plugin.client.session = FakeSession()
    return plugin, disp


def test_modes_follow_enabled_views(tmp_path):
    plugin, _ = make_plugin(tmp_path, {"views": {"race": {"enabled": False}, "records": {"enabled": False}}})
    assert plugin.modes == [M.MODE_LEADERBOARD, M.MODE_RUNS]
    plugin, _ = make_plugin(tmp_path)
    assert plugin.modes == M.ALL_MODES


def test_not_connected_shows_message(tmp_path):
    plugin, disp = make_plugin(tmp_path, connected=False)
    plugin.update()
    assert plugin._status == "CONNECT STRAVA"
    assert plugin.display(force_clear=True, display_mode=M.MODE_LEADERBOARD) is True
    assert disp.updates == 1 and disp.image.getbbox()


def test_update_fetches_and_display_renders_every_mode(tmp_path):
    plugin, disp = make_plugin(tmp_path)
    plugin.update()
    assert plugin.client.session.calls == 2
    runs, athletes = plugin._snapshot()
    assert len(runs) == 11                      # the Ride is filtered out
    assert all(a.connected for a in athletes)
    assert plugin.cache_manager.store              # raw activities cached
    for mode in M.ALL_MODES:
        plugin.display(force_clear=True, display_mode=mode)
        assert disp.image.size == (128, 32) and disp.image.getbbox(), mode
    assert disp.updates == 4


def test_frame_throttle_and_slot_restart(tmp_path):
    plugin, disp = make_plugin(tmp_path)
    plugin.update()
    plugin.display(force_clear=True, display_mode=M.MODE_RACE)
    plugin.display(display_mode=M.MODE_RACE)   # within 1/15 s: held frame
    assert disp.updates == 1
    start = plugin._slot_start
    plugin._last_display_call -= 10            # simulate a long gap -> new slot
    plugin.display(display_mode=M.MODE_RACE)
    assert plugin._slot_start > start


def test_runs_dynamic_cycle(tmp_path):
    plugin, disp = make_plugin(tmp_path)
    plugin.update()
    n = len(plugin._current_runs())
    assert n >= 3
    assert plugin.supports_dynamic_duration()
    assert plugin.get_cycle_duration(display_mode=M.MODE_RUNS) == min(40, max(10, n * 4))
    assert plugin.get_cycle_duration(display_mode=M.MODE_RACE) == plugin.display_duration
    plugin.display(force_clear=True, display_mode=M.MODE_RUNS)
    assert plugin.is_cycle_complete() is False
    plugin._slot_start -= n * 4 + 1              # jump past the last card
    plugin._last_render = 0
    plugin.display(display_mode=M.MODE_RUNS)
    assert plugin.is_cycle_complete() is True
    plugin.reset_cycle_state()
    assert plugin.is_cycle_complete() is False
    # other modes always report complete so they end at display_duration
    plugin.display(force_clear=True, display_mode=M.MODE_RECORDS)
    assert plugin.is_cycle_complete() is True


def test_runs_cursor_without_dynamic(tmp_path):
    plugin, disp = make_plugin(tmp_path, {"dynamic_duration": {"enabled": False}})
    plugin.update()
    n = len(plugin._current_runs())
    plugin.display(force_clear=True, display_mode=M.MODE_RUNS)
    plugin._slot_start -= 4.5; plugin._last_render = 0
    plugin.display(display_mode=M.MODE_RUNS)   # second card of the slot
    assert plugin._slot_runs_count == 2
    plugin.display(force_clear=True, display_mode=M.MODE_RUNS)  # new slot picks up after card 2
    assert plugin._slot_runs_base == 2 % n


def test_rate_limit_keeps_cached_data(tmp_path):
    plugin, disp = make_plugin(tmp_path)
    plugin.update()
    before = len(plugin._snapshot()[0])
    plugin.client.rate_limited_until = time.time() + 600
    plugin.update()
    assert len(plugin._snapshot()[0]) == before
    assert plugin._status is None            # cached data still shown


def test_config_change_reloads(tmp_path):
    plugin, disp = make_plugin(tmp_path)
    plugin.on_config_change({**plugin.config, "units": "km", "views": {"race": {"goal": 50}}})
    assert plugin.units == "km" and plugin.race_goal == 50
    plugin.update()
    plugin.display(force_clear=True, display_mode=M.MODE_RACE)
    assert disp.image.getbbox()


def test_other_panel_sizes(tmp_path):
    for size in [(64, 32), (128, 64), (256, 128)]:
        plugin, disp = make_plugin(tmp_path, size=size)
        plugin.update()
        for mode in M.ALL_MODES:
            plugin.display(force_clear=True, display_mode=mode)
            assert disp.image.size == size


def test_fast_polling_until_first_fetch_and_token_recheck(tmp_path):
    plugin, disp = make_plugin(tmp_path, connected=False)
    assert plugin.get_update_interval() == M.UNCONNECTED_POLL_S
    plugin.display(force_clear=True, display_mode=M.MODE_LEADERBOARD)
    assert not plugin._any_connected()
    # the web-UI action writes token.json while the display is running
    store = plugin.token_store
    for aid in ("1001", "2002"):
        store.upsert({"refresh_token": "r", "access_token": f"tok-{aid}", "expires_at": time.time() + 9999,
                      "athlete": {"id": int(aid), "firstname": "X"}})
    plugin._last_token_check -= M.TOKEN_RECHECK_S + 1
    plugin._last_render = 0
    plugin.display(display_mode=M.MODE_LEADERBOARD)
    assert plugin._any_connected()
    assert plugin.get_update_interval() == M.FIRST_FETCH_RETRY_S
    plugin.update()
    assert plugin.get_update_interval() == plugin.update_interval_s


def test_loading_screen_before_first_fetch(tmp_path):
    plugin, disp = make_plugin(tmp_path)
    img = plugin._render(M.MODE_LEADERBOARD, 128, 32, 0.0)
    loading = img.tobytes()
    plugin.update()
    assert plugin._render(M.MODE_LEADERBOARD, 128, 32, 0.0).tobytes() != loading
