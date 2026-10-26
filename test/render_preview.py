"""Render every view from the fixture to PNGs (scaled up) for eyeballing.

    .venv/bin/python test/render_preview.py [out_dir] [--sizes 128x32,64x32]
"""
from __future__ import annotations

import json
import sys
from datetime import datetime
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))

from PIL import Image  # noqa: E402

import strava_data as D  # noqa: E402
import strava_render as R  # noqa: E402
from strava_api import decode_polyline  # noqa: E402


def load_fixture():
    fx = json.loads((HERE / "fixtures" / "activities.json").read_text())
    now = datetime.fromisoformat(fx["now"])
    runs = [D.parse_activity(a) for a in fx["activities"]]
    runs = [r for r in runs if D.is_counted(r)]
    cfg_athletes = [
        {"name": "Bridger", "strava_id": "1001", "shirt_color": [252, 76, 2], "shorts_color": [30, 30, 40], "accessory": "headband"},
        {"name": "Roommate", "strava_id": "2002", "shirt_color": [40, 140, 255], "shorts_color": [245, 245, 245], "accessory": "cap"},
    ]
    athletes = D.resolve_athletes(cfg_athletes, fx["athletes"])
    return now, runs, athletes


def frames_for(w, h, now, runs, athletes, units="mi"):
    out = {}
    start, end = D.period_bounds(D.Period("week", 0), now)
    rows = D.leaderboard(runs, athletes, start, end)
    out["leaderboard"] = R.render_leaderboard(w, h, D.period_label(D.Period("week", 0), now), rows, units)
    m_start, m_end = D.period_bounds(D.Period("month", 0), now)
    out["leaderboard_month"] = R.render_leaderboard(w, h, D.period_label(D.Period("month", 0), now),
                                                    D.leaderboard(runs, athletes, m_start, m_end), units)
    week_runs = D.runs_between(runs, start, end)
    by_id = {a.id: a for a in athletes}
    for i, run in enumerate(week_runs[:3]):
        a = by_id[run.athlete_id]
        coords = decode_polyline(run.polyline)
        for tt in (0.8, 3.0, 5.5):
            out[f"run{i}_t{tt}"] = R.render_run_card(w, h, a, run, coords, units, D.fmt_day_short(run.day, now.date()), tt, is_today=run.day == now.date())
    entries = [R.RaceEntry(r.athlete, r.distance(units)) for r in rows]
    for tt in (0.0, 0.3):
        out[f"race_t{tt}"] = R.render_race(w, h, entries, 20, units, tt, title="WEEK")
    out["race_done"] = R.render_race(w, h, [R.RaceEntry(athletes[0], 21.0), R.RaceEntry(athletes[1], 9.2)], 20, units, 0.1, title="WEEK")
    out["records"] = R.render_records(w, h, D.records(runs, athletes, now), units)
    out["message"] = R.render_message(w, h, ["CONNECT STRAVA", "IN WEB UI"], R.GREY)
    return out


def main(argv):
    out_dir = Path(argv[1]) if len(argv) > 1 and not argv[1].startswith("--") else HERE.parent / "preview_out"
    sizes = [(128, 32), (64, 32), (128, 64)]
    if "--sizes" in argv:
        sizes = [tuple(int(v) for v in s.split("x")) for s in argv[argv.index("--sizes") + 1].split(",")]
    out_dir.mkdir(parents=True, exist_ok=True)
    now, runs, athletes = load_fixture()
    for (w, h) in sizes:
        frames = frames_for(w, h, now, runs, athletes)
        scale = max(1, 512 // w)
        for name, img in frames.items():
            assert img.size == (w, h), (name, img.size)
            img.resize((w * scale, h * scale), Image.NEAREST).save(out_dir / f"{w}x{h}_{name}.png")
        # contact sheet
        names = list(frames)
        sheet = Image.new("RGB", (w * scale, (h * scale + 4) * len(names)), (20, 20, 20))
        for i, name in enumerate(names):
            sheet.paste(frames[name].resize((w * scale, h * scale), Image.NEAREST), (0, i * (h * scale + 4)))
        sheet.save(out_dir / f"sheet_{w}x{h}.png")
        print(f"{w}x{h}: {len(frames)} frames -> {out_dir}")


if __name__ == "__main__":
    main(sys.argv)
