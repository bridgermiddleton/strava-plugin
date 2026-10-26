"""Strava Running plugin for LEDMatrix.

Four display modes served by one instance: ``strava_leaderboard``,
``strava_runs``, ``strava_race`` and ``strava_records``. Network work happens
in ``update()`` (worker thread); ``display()`` only renders. The plugin asks
for the high-FPS loop and throttles itself to ~15 fps for the animations.
"""

from __future__ import annotations

import os
import sys
import threading
import time
from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional, Tuple

from PIL import ImageDraw

_HERE = os.path.dirname(os.path.abspath(__file__))
if _HERE not in sys.path:
    sys.path.insert(0, _HERE)

try:
    from src.plugin_system.base_plugin import BasePlugin
except Exception:  # pragma: no cover - only when running outside LEDMatrix
    class BasePlugin:  # type: ignore[no-redef]
        """Minimal stand-in so the module imports in tests without the core."""

        def __init__(self, plugin_id, config, display_manager, cache_manager, plugin_manager):
            import logging
            self.plugin_id = plugin_id
            self.config = config or {}
            self.display_manager = display_manager
            self.cache_manager = cache_manager
            self.plugin_manager = plugin_manager
            self.logger = logging.getLogger(f"plugin.{plugin_id}")
            self.enabled = self.config.get("enabled", True)

        def on_config_change(self, new_config):
            self.config = new_config or {}
            self.enabled = self.config.get("enabled", True)

        def validate_config(self):
            return True

        def cleanup(self):
            pass

import strava_data as D  # noqa: E402
import strava_render as R  # noqa: E402
from strava_api import (  # noqa: E402
    StravaAuthError, StravaClient, StravaError, StravaRateLimited, TokenStore, decode_polyline,
)

MODE_LEADERBOARD = "strava_leaderboard"
MODE_RUNS = "strava_runs"
MODE_RACE = "strava_race"
MODE_RECORDS = "strava_records"
MODE_VIEW_KEY = {
    MODE_LEADERBOARD: "leaderboard",
    MODE_RUNS: "runs",
    MODE_RACE: "race",
    MODE_RECORDS: "records",
}
ALL_MODES = list(MODE_VIEW_KEY)

#: A gap this long between display() calls means a new rotation slot started.
NEW_SLOT_GAP_S = 2.5
CACHE_TTL_S = 7 * 24 * 3600          # Strava's terms: cache no longer than 7 days
RENDER_FPS = 15.0
#: Poll cadence while nobody is connected yet (token.json just reads, no network).
UNCONNECTED_POLL_S = 15.0
#: Retry cadence while connected but no fetch has succeeded yet.
FIRST_FETCH_RETRY_S = 60.0
#: How often display() re-reads token.json while waiting for a connection.
TOKEN_RECHECK_S = 5.0


def _num(value: Any, default: float, lo: Optional[float] = None, hi: Optional[float] = None) -> float:
    try:
        v = float(value)
    except (TypeError, ValueError):
        return default
    if v != v:  # NaN
        return default
    if lo is not None:
        v = max(lo, v)
    if hi is not None:
        v = min(hi, v)
    return v


class StravaPlugin(BasePlugin):
    """See module docstring."""

    def __init__(self, plugin_id: str, config: Dict[str, Any], display_manager: Any,
                 cache_manager: Any, plugin_manager: Any) -> None:
        super().__init__(plugin_id, config, display_manager, cache_manager, plugin_manager)
        self.needs_high_fps = True
        self.enable_scrolling = False
        self._data_lock = threading.Lock()
        self._runs: List[D.Run] = []
        self._athletes: List[D.Athlete] = []
        self._coords_cache: Dict[str, List[Tuple[float, float]]] = {}
        self._status: Optional[str] = None       # short error/status for the panel
        self._last_update_ok: Optional[float] = None
        self._fetched_window: Optional[Tuple[datetime, datetime]] = None
        self._last_token_check: float = 0.0
        # per-slot animation state
        self._slot_mode: Optional[str] = None
        self._slot_start: float = 0.0
        self._last_display_call: float = 0.0
        self._last_render: float = 0.0
        self._runs_cursor: int = 0
        self._slot_runs_base: int = 0
        self._slot_runs_count: int = 0
        self._cycle_done: bool = False
        self._min_frame_interval = 1.0 / RENDER_FPS

        self._load_config(config or {})
        self.token_store = TokenStore()
        self.client = StravaClient(self.token_store, self.client_id, self.client_secret,
                                   logger=self.logger)
        # Rotation slots: only enabled views. The core reads ``self.modes`` at
        # registration time, so toggling a view needs a display restart.
        self.modes = [m for m in ALL_MODES if self.views[MODE_VIEW_KEY[m]].get("enabled", True)]
        if not self.modes:
            self.modes = [MODE_LEADERBOARD]
        self._athletes = D.resolve_athletes(self.config_athletes, self.token_store.load())
        self._load_from_cache()

    # ------------------------------------------------------------------ config
    def _load_config(self, config: Dict[str, Any]) -> None:
        self.config = config
        self.enabled = bool(config.get("enabled", True))
        self.client_id = str(config.get("client_id") or "").strip()
        self.client_secret = str(config.get("client_secret") or "").strip()
        self.units = "km" if str(config.get("units", "mi")).lower() == "km" else "mi"
        self.week_start = "sunday" if str(config.get("week_start", "monday")).lower() == "sunday" else "monday"
        self.display_duration = _num(config.get("display_duration"), 10.0, 1.0, 600.0)
        self.update_interval_s = _num(config.get("update_interval"), 600.0, 300.0, 86400.0)
        self.history_days = int(_num(config.get("history_days"), 90, 14, 400))
        sports = config.get("sport_types")
        self.sport_types = tuple(str(s) for s in sports) if isinstance(sports, list) and sports else D.DEFAULT_SPORT_TYPES
        self.config_athletes = config.get("athletes") if isinstance(config.get("athletes"), list) else []

        views = config.get("views") if isinstance(config.get("views"), dict) else {}
        self.views: Dict[str, Dict[str, Any]] = {}
        for key in ("leaderboard", "runs", "race", "records"):
            v = views.get(key)
            self.views[key] = dict(v) if isinstance(v, dict) else {}
        lb = self.views["leaderboard"]
        periods = lb.get("periods")
        self.lb_periods = [D.Period.from_config(p) for p in periods] if isinstance(periods, list) and periods else [D.Period()]
        self.lb_seconds_per_page = _num(lb.get("seconds_per_page"), 5.0, 2.0, 60.0)
        runs = self.views["runs"]
        self.runs_period = D.Period("month" if runs.get("period") == "month" else "week", 0)
        self.runs_seconds = _num(runs.get("seconds_per_run"), 6.0, 3.0, 30.0)
        self.runs_max_seconds = _num(runs.get("max_seconds"), 60.0, 10.0, 300.0)
        self.runs_trace = bool(runs.get("trace_route", True))
        race = self.views["race"]
        self.race_goal = _num(race.get("goal"), 20.0, 0.5, 100000.0)
        self.race_period = D.Period("month" if race.get("period") == "month" else "week", 0)
        dyn = config.get("dynamic_duration") if isinstance(config.get("dynamic_duration"), dict) else {}
        self.dynamic_enabled = bool(dyn.get("enabled", True))
        self.dynamic_cap = _num(dyn.get("max_duration_seconds"), self.runs_max_seconds, 10.0, 600.0)

    def on_config_change(self, new_config: Dict[str, Any]) -> None:
        try:
            super().on_config_change(new_config)
        except Exception:  # pragma: no cover
            pass
        self._load_config(new_config or {})
        self.client.client_id = self.client_id
        self.client.client_secret = self.client_secret
        with self._data_lock:
            self._athletes = D.resolve_athletes(self.config_athletes, self.token_store.load())
        self._fetched_window = None   # force a full refetch with new periods

    def validate_config(self) -> bool:
        if not self.client_id or not self.client_secret:
            self.logger.warning("Strava client_id/client_secret not set; the panel will ask you to configure them")
        return True

    def get_update_interval(self) -> float:
        """Configured cadence once data has loaded; quick retries before that so
        a freshly connected athlete shows up within seconds, not 10 minutes."""
        if self._last_update_ok is not None:
            return self.update_interval_s
        if not self._any_connected():
            return UNCONNECTED_POLL_S
        return FIRST_FETCH_RETRY_S

    def _any_connected(self) -> bool:
        with self._data_lock:
            return any(a.connected for a in self._athletes)

    def _recheck_tokens(self, now_mono: float) -> None:
        """Cheap re-read of token.json so the 'connect' screen clears as soon
        as the web-UI action has written it (update() may be minutes away)."""
        if now_mono - self._last_token_check < TOKEN_RECHECK_S:
            return
        self._last_token_check = now_mono
        try:
            athletes = D.resolve_athletes(self.config_athletes, self.token_store.load())
        except Exception as exc:  # pragma: no cover
            self.logger.debug("token recheck failed: %s", exc)
            return
        with self._data_lock:
            self._athletes = athletes

    def get_display_duration(self) -> float:
        return float(self.display_duration)

    # --------------------------------------------------------- dynamic duration
    def supports_dynamic_duration(self) -> bool:
        return self.dynamic_enabled and MODE_RUNS in self.modes

    def get_dynamic_duration_cap(self) -> Optional[float]:
        return float(min(self.dynamic_cap, self.runs_max_seconds))

    def get_cycle_duration(self, display_mode: Optional[str] = None) -> Optional[float]:
        if display_mode == MODE_RUNS:
            n = len(self._current_runs())
            if n == 0:
                return self.display_duration
            return min(self.runs_max_seconds, max(self.display_duration, n * self.runs_seconds))
        return self.display_duration

    def is_cycle_complete(self) -> bool:
        if self._slot_mode != MODE_RUNS:
            return True
        return self._cycle_done

    def reset_cycle_state(self) -> None:
        self._cycle_done = False

    # ------------------------------------------------------------------ update
    def _needed_window(self, now: datetime) -> Tuple[datetime, datetime]:
        starts = [now - timedelta(days=self.history_days)]
        for p in self.lb_periods + [self.runs_period, self.race_period, D.Period("month", 0)]:
            starts.append(D.period_bounds(p, now, self.week_start)[0])
        return min(starts), now + timedelta(days=1)

    def _cache_key(self, athlete_id: str) -> str:
        return f"strava_activities_{athlete_id}"

    def _load_from_cache(self) -> None:
        if self.cache_manager is None:
            return
        runs: List[D.Run] = []
        for a in self._athletes:
            try:
                raw = self.cache_manager.get(self._cache_key(a.id), max_age=CACHE_TTL_S)
            except TypeError:
                try:
                    raw = self.cache_manager.get(self._cache_key(a.id))
                except Exception:
                    raw = None
            except Exception:
                raw = None
            if isinstance(raw, list):
                runs.extend(self._parse(raw, a.id))
        if runs:
            with self._data_lock:
                self._runs = runs
            self.logger.info("Loaded %d cached Strava activities", len(runs))

    def _parse(self, raw: List[Dict[str, Any]], athlete_id: str) -> List[D.Run]:
        out = []
        for item in raw:
            if not isinstance(item, dict):
                continue
            try:
                r = D.parse_activity(item, athlete_id)
            except Exception:
                continue
            if D.is_counted(r, self.sport_types):
                out.append(r)
        return out

    def update(self) -> None:
        now = datetime.now()
        tokens = self.token_store.load()
        athletes = D.resolve_athletes(self.config_athletes, tokens)
        with self._data_lock:
            self._athletes = athletes
        connected = [a for a in athletes if a.connected]
        if not self.client_id or not self.client_secret:
            # per-athlete apps may still work; only complain if nobody has creds
            if not any(tokens.get(a.id, {}).get("client_id") for a in connected):
                self._status = "SET CLIENT ID"
                return
        if not connected:
            self._status = "CONNECT STRAVA"
            return

        start, end = self._needed_window(now)
        after = int(time.mktime(start.timetuple()))
        before = int(time.mktime(end.timetuple()))
        new_runs: Dict[str, List[D.Run]] = {}
        errors: List[str] = []
        for a in connected:
            try:
                raw = self.client.fetch_activities(a.id, after, before)
            except StravaRateLimited as exc:
                self.logger.warning("Strava rate limited: %s", exc)
                errors.append("RATE LIMITED")
                break
            except StravaAuthError as exc:
                self.logger.warning("Strava auth problem for %s: %s", a.name, exc)
                errors.append(f"RECONNECT {a.label}")
                continue
            except (StravaError, Exception) as exc:  # network etc.
                self.logger.warning("Strava fetch failed for %s: %s", a.name, exc)
                errors.append("STRAVA OFFLINE")
                continue
            new_runs[a.id] = self._parse(raw, a.id)
            if self.cache_manager is not None:
                try:
                    self.cache_manager.set(self._cache_key(a.id), raw, ttl=CACHE_TTL_S)
                except TypeError:
                    try:
                        self.cache_manager.set(self._cache_key(a.id), raw)
                    except Exception:
                        pass
                except Exception:
                    pass

        if new_runs:
            with self._data_lock:
                keep = [r for r in self._runs if r.athlete_id not in new_runs]
                merged = keep + [r for rs in new_runs.values() for r in rs]
                self._runs = merged
                self._fetched_window = (start, end)
            self._last_update_ok = time.time()
            self.logger.info("Strava: %d activities for %d athletes (%s)",
                             sum(len(v) for v in new_runs.values()), len(new_runs),
                             self.client.last_usage or "usage n/a")
        self._status = errors[0] if errors and not self._runs else None
        if errors and self._runs:
            self.logger.info("Strava update had issues (%s); showing cached data", ", ".join(errors))

    # ----------------------------------------------------------------- helpers
    def _snapshot(self) -> Tuple[List[D.Run], List[D.Athlete]]:
        with self._data_lock:
            return list(self._runs), list(self._athletes)

    def _current_runs(self) -> List[D.Run]:
        runs, _ = self._snapshot()
        start, end = D.period_bounds(self.runs_period, datetime.now(), self.week_start)
        out = D.runs_between(runs, start, end)
        out.sort(key=lambda r: r.start_local, reverse=True)
        return out

    def _coords(self, run: D.Run) -> List[Tuple[float, float]]:
        if not run.polyline:
            return []
        c = self._coords_cache.get(run.id)
        if c is None:
            c = decode_polyline(run.polyline)
            if len(self._coords_cache) > 300:
                self._coords_cache.clear()
            self._coords_cache[run.id] = c
        return c

    def _size(self) -> Tuple[int, int]:
        dm = self.display_manager
        w = getattr(dm, "width", None) or getattr(getattr(dm, "matrix", None), "width", None) or 128
        h = getattr(dm, "height", None) or getattr(getattr(dm, "matrix", None), "height", None) or 32
        return int(w), int(h)

    def _push(self, img) -> None:
        dm = self.display_manager
        dm.image = img
        try:
            dm.draw = ImageDraw.Draw(img)
        except Exception:
            pass
        dm.update_display()

    def _begin_slot(self, mode: str, now: float) -> None:
        self._slot_mode = mode
        self._slot_start = now
        self._last_render = 0.0
        self._cycle_done = False
        if mode == MODE_RUNS:
            n = len(self._current_runs())
            if self.dynamic_enabled or n == 0:
                self._slot_runs_base = 0
            else:
                self._slot_runs_base = self._runs_cursor % n
            self._slot_runs_count = 0

    # ----------------------------------------------------------------- display
    def display(self, force_clear: bool = False, display_mode: Optional[str] = None) -> bool:
        now = time.monotonic()
        mode = display_mode or self._slot_mode or self.modes[0]
        gap = now - self._last_display_call if self._last_display_call else None
        if force_clear or mode != self._slot_mode or (gap is not None and gap > NEW_SLOT_GAP_S):
            if self._slot_mode == MODE_RUNS and not self.dynamic_enabled and self._slot_runs_count:
                self._runs_cursor = self._slot_runs_base + self._slot_runs_count
            self._begin_slot(mode, now)
        self._last_display_call = now
        if self._last_render and now - self._last_render < self._min_frame_interval:
            return True
        self._last_render = now
        if self._last_update_ok is None and not self._any_connected():
            self._recheck_tokens(now)
        t = now - self._slot_start
        w, h = self._size()
        try:
            img = self._render(mode, w, h, t)
        except Exception as exc:  # never take the display down
            self.logger.error("Strava render failed: %s", exc, exc_info=True)
            img = R.render_message(w, h, ["STRAVA", "RENDER ERROR"], R.RED)
        self._push(img)
        return True

    def _render(self, mode: str, w: int, h: int, t: float):
        runs, athletes = self._snapshot()
        now = datetime.now()
        if not runs:
            if not athletes or not any(a.connected for a in athletes):
                if not self.client_id or not self.client_secret:
                    return R.render_message(w, h, ["SET CLIENT ID", "IN WEB UI"], R.GREY)
                return R.render_message(w, h, ["CONNECT STRAVA", "IN WEB UI"], R.GREY)
            if self._status:
                return R.render_message(w, h, ["STRAVA", self._status], R.GREY)
            if self._last_update_ok is None:
                return R.render_message(w, h, ["LOADING RUNS", "FROM STRAVA..."], R.GREY)
        if mode == MODE_RUNS:
            return self._render_runs(w, h, t, runs, athletes, now)
        if mode == MODE_RACE:
            start, end = D.period_bounds(self.race_period, now, self.week_start)
            rows = D.leaderboard(runs, athletes, start, end)
            entries = [R.RaceEntry(r.athlete, r.distance(self.units)) for r in rows]
            title = "WEEK" if self.race_period.kind == "week" else "MONTH"
            return R.render_race(w, h, entries, self.race_goal, self.units, t, title=title)
        if mode == MODE_RECORDS:
            rows = D.records(runs, athletes, now, self.week_start, self.units)
            return R.render_records(w, h, rows, self.units, t)
        # leaderboard (default)
        periods = self.lb_periods
        idx = int(t // self.lb_seconds_per_page) % len(periods) if len(periods) > 1 else 0
        p = periods[idx]
        start, end = D.period_bounds(p, now, self.week_start)
        rows = D.leaderboard(runs, athletes, start, end)
        title = D.period_label(p, now, self.week_start) if w >= 96 else D.period_short_label(p, now, self.week_start)
        return R.render_leaderboard(w, h, title, rows, self.units, t % self.lb_seconds_per_page
                                    if len(periods) > 1 else t, self.lb_seconds_per_page)

    def _render_runs(self, w: int, h: int, t: float, runs, athletes, now: datetime):
        start, end = D.period_bounds(self.runs_period, now, self.week_start)
        period_runs = D.runs_between(runs, start, end)
        period_runs.sort(key=lambda r: r.start_local, reverse=True)
        n = len(period_runs)
        if n == 0:
            self._cycle_done = True
            label = "THIS WEEK" if self.runs_period.kind == "week" else "THIS MONTH"
            return R.render_message(w, h, ["NO RUNS YET", label], R.GREY)
        idx_in_slot = int(t // self.runs_seconds)
        if self.dynamic_enabled:
            if idx_in_slot >= n:
                self._cycle_done = True
                idx_in_slot = n - 1
            idx = idx_in_slot
        else:
            idx = (self._slot_runs_base + idx_in_slot) % n
        self._slot_runs_count = max(self._slot_runs_count, idx_in_slot + 1)
        run = period_runs[idx]
        by_id = {a.id: a for a in athletes}
        athlete = by_id.get(run.athlete_id) or athletes[0]
        t_card = t - min(idx_in_slot, n - 1) * self.runs_seconds if self.dynamic_enabled else t - idx_in_slot * self.runs_seconds
        return R.render_run_card(w, h, athlete, run, self._coords(run), self.units,
                                 D.fmt_day_short(run.day, now.date()), t_card, self.runs_seconds,
                                 self.runs_trace, is_today=run.day == now.date())

    # ------------------------------------------------------------------- misc
    def get_info(self) -> Dict[str, Any]:
        runs, athletes = self._snapshot()
        return {
            "plugin_id": self.plugin_id,
            "modes": list(self.modes),
            "athletes": [{"id": a.id, "name": a.name, "connected": a.connected} for a in athletes],
            "activities_loaded": len(runs),
            "status": self._status,
            "last_update_ok": self._last_update_ok,
            "strava_usage": self.client.last_usage,
        }

    def cleanup(self) -> None:
        try:
            super().cleanup()
        except Exception:
            pass
