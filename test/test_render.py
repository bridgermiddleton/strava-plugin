import json
from datetime import datetime
from pathlib import Path

import pytest
from PIL import Image

import strava_data as D
import strava_render as R
import strava_sprites as S
from strava_api import decode_polyline

FIX = json.loads((Path(__file__).parent / "fixtures" / "activities.json").read_text())
NOW = datetime.fromisoformat(FIX["now"])
SIZES = [(128, 32), (64, 32), (128, 64), (96, 48), (256, 32), (64, 64), (192, 96)]


@pytest.fixture(scope="module")
def world():
    runs = [r for r in (D.parse_activity(a) for a in FIX["activities"]) if D.is_counted(r)]
    athletes = D.resolve_athletes([
        {"name": "Bridger", "strava_id": "1001", "accessory": "headband"},
        {"name": "Roommate", "strava_id": "2002", "accessory": "cap"},
        {"name": "Third", "strava_id": "3003", "accessory": "sunglasses"},
    ], FIX["athletes"])
    return runs, athletes


def _in_bounds(img, w, h):
    assert img.size == (w, h)
    assert img.mode == "RGB"
    assert img.getbbox() is not None, "blank frame"


@pytest.mark.parametrize("w,h", SIZES)
def test_leaderboard_all_sizes(world, w, h):
    runs, athletes = world
    s, e = D.period_bounds(D.Period(), NOW)
    rows = D.leaderboard(runs, athletes, s, e)
    for t in (0.0, 6.0):
        _in_bounds(R.render_leaderboard(w, h, "THIS WEEK", rows, "mi", t), w, h)
    _in_bounds(R.render_leaderboard(w, h, "THIS WEEK", [], "km"), w, h)


@pytest.mark.parametrize("w,h", SIZES)
def test_run_card_all_sizes(world, w, h):
    runs, athletes = world
    by_id = {a.id: a for a in athletes}
    for run in runs[:4]:
        a = by_id[run.athlete_id]
        for t in (0.0, 2.5, 6.0):
            _in_bounds(R.render_run_card(w, h, a, run, decode_polyline(run.polyline), "mi", "TUE", t), w, h)
    # a run with a single GPS point, zero distance, and km units must not crash
    weird = D.Run("x", "1001", "w", NOW, 0.0, 0, "Run", polyline=None)
    _in_bounds(R.render_run_card(w, h, athletes[0], weird, [(42.0, -71.0)], "km", "TODAY", 1.0), w, h)


@pytest.mark.parametrize("w,h", SIZES)
def test_race_and_records_all_sizes(world, w, h):
    runs, athletes = world
    entries = [R.RaceEntry(athletes[0], 21.7), R.RaceEntry(athletes[1], 9.2), R.RaceEntry(athletes[2], 0.0)]
    for t in (0.0, 0.35, 7.0):
        _in_bounds(R.render_race(w, h, entries, 20, "mi", t), w, h)
    _in_bounds(R.render_race(w, h, [], 20, "mi", 0.0), w, h)
    _in_bounds(R.render_records(w, h, D.records(runs, athletes, NOW), "mi"), w, h)
    _in_bounds(R.render_records(w, h, [], "mi"), w, h)
    _in_bounds(R.render_message(w, h, ["CONNECT STRAVA", "IN WEB UI"]), w, h)


def test_route_trace_progresses(world):
    runs, athletes = world
    run = next(r for r in runs if r.polyline)
    coords = decode_polyline(run.polyline)
    early = R.render_run_card(128, 32, athletes[0], run, coords, "mi", "TUE", 0.2)
    late = R.render_run_card(128, 32, athletes[0], run, coords, "mi", "TUE", 5.9)
    box = (0, 0, 32, 32)
    def lit(im):
        px = im.load()
        return sum(1 for x in range(box[0], box[2]) for y in range(box[1], box[3]) if px[x, y] == athletes[0].color)
    assert lit(late) > lit(early) * 2
    # finished route has a red end dot (on a loop it covers the green start dot)
    pts = R.project_route(coords, (1, 1, 30, 30))
    assert late.getpixel(pts[-1]) == R.RED
    assert early.getpixel(pts[0]) == R.GREEN


def test_project_route_fits_box():
    coords = [(42.0, -71.0), (42.01, -71.0), (42.01, -70.98), (42.0, -70.98)]
    pts = R.project_route(coords, (10, 5, 30, 30))
    xs = [p[0] for p in pts]
    ys = [p[1] for p in pts]
    assert 10 <= min(xs) and max(xs) <= 39 and 5 <= min(ys) and max(ys) <= 34
    assert R.project_route([], (0, 0, 10, 10)) == []
    assert R.project_route([(1.0, 1.0)], (0, 0, 10, 10)) == [(5, 5)]


def test_sprites_shapes_and_accessories():
    a = D.Athlete("1", "A", (255, 0, 0), (0, 0, 255), "cap")
    frames = S.runner_frames(a)
    assert len(frames) == 4 and all(f.size == (8, 12) for f in frames)
    assert all(f.getbbox() for f in frames)
    for acc in D.ACCESSORIES:
        b = D.Athlete("2", "B", (1, 2, 3), (4, 5, 6), acc)
        assert S.runner_frames(b, scale=2)[0].size == (16, 24)
    assert S.bust(a).size == (5, 6)
    # shirt color is actually used
    px = frames[0].load()
    assert any(px[x, y][:3] == (255, 0, 0) for x in range(8) for y in range(12))
    assert S.frame_index(0.0) == 0 and S.frame_index(0.5, fps=8) == 0 and S.frame_index(1.0 / 8, fps=8) == 1


def test_upscale_factor():
    assert R.upscale_factor(128, 32) == 1
    assert R.upscale_factor(128, 64) == 2
    assert R.upscale_factor(64, 64) == 1       # can't halve width below 64
    assert R.upscale_factor(256, 128) == 4
