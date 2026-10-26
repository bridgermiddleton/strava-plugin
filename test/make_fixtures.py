"""Generate test/fixtures/activities.json: a week of synthetic runs for two
athletes with encoded loop routes. Deterministic; re-run to regenerate."""
from __future__ import annotations

import json
import math
import random
from datetime import datetime, timedelta
from pathlib import Path

OUT = Path(__file__).parent / "fixtures" / "activities.json"
NOW = datetime(2026, 10, 25, 14, 0, 0)  # Sunday


def encode_polyline(coords, precision=5):
    factor = 10 ** precision
    out = []
    plat = plon = 0

    def enc(v):
        v = ~(v << 1) if v < 0 else v << 1
        s = ""
        while v >= 0x20:
            s += chr((0x20 | (v & 0x1F)) + 63)
            v >>= 5
        return s + chr(v + 63)

    for lat, lon in coords:
        ilat, ilon = int(round(lat * factor)), int(round(lon * factor))
        out.append(enc(ilat - plat))
        out.append(enc(ilon - plon))
        plat, plon = ilat, ilon
    return "".join(out)


def loop_route(seed, center=(42.36, -71.06), n=60, miles=5.0):
    rnd = random.Random(seed)
    # irregular closed loop with some wobble; ~0.0145 deg lat per mile
    radius_deg = miles / (2 * math.pi) * 0.0145
    pts = []
    for i in range(n + 1):
        a = 2 * math.pi * i / n
        r = radius_deg * (1 + 0.25 * math.sin(3 * a + seed) + 0.1 * rnd.uniform(-1, 1))
        lat = center[0] + r * math.sin(a)
        lon = center[1] + r * math.cos(a) / math.cos(math.radians(center[0]))
        pts.append((lat, lon))
    return pts


def out_and_back(seed, start=(42.36, -71.06), miles=4.0):
    rnd = random.Random(seed)
    half = miles / 2 * 0.0145
    pts = []
    n = 30
    for i in range(n + 1):
        f = i / n
        pts.append((start[0] + half * f, start[1] + 0.002 * math.sin(6 * f) + rnd.uniform(-0.0002, 0.0002)))
    return pts + pts[::-1][1:]


def activity(aid, athlete_id, name, day_offset, hour, miles, pace_s, polyline, sport="Run"):
    start = NOW.replace(hour=hour, minute=0, second=0) - timedelta(days=day_offset)
    dist = miles * 1609.344
    return {
        "id": aid,
        "name": name,
        "athlete": {"id": athlete_id},
        "distance": round(dist, 1),
        "moving_time": int(miles * pace_s),
        "elapsed_time": int(miles * pace_s * 1.05),
        "total_elevation_gain": round(miles * 12.3, 1),
        "sport_type": sport,
        "type": sport,
        "start_date_local": start.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "start_date": start.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "map": {"id": f"a{aid}", "summary_polyline": polyline},
        "average_speed": round(dist / (miles * pace_s), 3),
    }


def main():
    a1, a2 = 1001, 2002
    acts = [
        # athlete 1 (Bridger): 4 runs this week, streak 3 days (Fri, Sat, Sun)
        activity(1, a1, "Sunday long run", 0, 8, 9.1, 8 * 60 + 40, encode_polyline(loop_route(1, miles=9.1))),
        activity(2, a1, "Easy shakeout", 1, 7, 3.2, 9 * 60 + 5, encode_polyline(out_and_back(2, miles=3.2))),
        activity(3, a1, "Tempo", 2, 18, 5.0, 7 * 60 + 12, encode_polyline(loop_route(3, miles=5.0))),
        activity(4, a1, "Lunch jog", 5, 12, 4.4, 8 * 60 + 50, encode_polyline(loop_route(4, miles=4.4))),
        # athlete 2 (roommate): 3 runs this week incl. treadmill, streak 1 (yesterday)
        activity(5, a2, "Treadmill intervals", 1, 19, 4.0, 7 * 60 + 45, None),
        activity(6, a2, "Morning 10k", 3, 6, 6.2, 8 * 60 + 20, encode_polyline(loop_route(6, center=(42.37, -71.09), miles=6.2))),
        activity(7, a2, "River loop", 4, 17, 7.5, 8 * 60 + 2, encode_polyline(loop_route(7, center=(42.37, -71.09), miles=7.5))),
        # last week + earlier (for month/records)
        activity(8, a1, "Last week long", 8, 8, 11.0, 8 * 60 + 55, encode_polyline(loop_route(8, miles=11.0))),
        activity(9, a2, "Last week run", 9, 7, 5.5, 8 * 60 + 10, encode_polyline(loop_route(9, miles=5.5))),
        activity(10, a2, "Longest of month", 13, 8, 12.3, 8 * 60 + 30, encode_polyline(loop_route(10, miles=12.3))),
        activity(11, a1, "Bike commute", 1, 9, 6.0, 4 * 60, encode_polyline(loop_route(11, miles=6.0)), sport="Ride"),
        activity(12, a1, "September run", 40, 8, 6.0, 8 * 60 + 30, encode_polyline(loop_route(12, miles=6.0))),
    ]
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(json.dumps({"now": NOW.isoformat(), "athletes": {
        str(a1): {"athlete": {"id": a1, "firstname": "Bridger", "lastname": "M"}},
        str(a2): {"athlete": {"id": a2, "firstname": "Roommate", "lastname": "R"}},
    }, "activities": acts}, indent=1))
    print(f"wrote {OUT} ({len(acts)} activities)")


if __name__ == "__main__":
    main()
