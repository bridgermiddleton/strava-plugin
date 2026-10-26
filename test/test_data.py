import json
from datetime import date, datetime
from pathlib import Path

import pytest

import strava_data as D
from strava_api import decode_polyline

FIX = json.loads((Path(__file__).parent / "fixtures" / "activities.json").read_text())
NOW = datetime.fromisoformat(FIX["now"])  # Sunday 2026-10-25 14:00


@pytest.fixture
def runs():
    return [r for r in (D.parse_activity(a) for a in FIX["activities"]) if D.is_counted(r)]


@pytest.fixture
def athletes():
    return D.resolve_athletes([
        {"name": "Bridger", "strava_id": "1001"},
        {"name": "Roommate", "strava_id": "2002", "accessory": "cap"},
    ], FIX["athletes"])


def test_polyline_google_reference():
    assert decode_polyline("_p~iF~ps|U_ulLnnqC_mqNvxq`@") == [(38.5, -120.2), (40.7, -120.95), (43.252, -126.453)]
    assert decode_polyline(None) == []
    assert decode_polyline("") == []


def test_week_bounds_monday_and_sunday():
    s, e = D.period_bounds(D.Period("week", 0), NOW)
    assert (s.date(), e.date()) == (date(2026, 10, 19), date(2026, 10, 26))
    s, e = D.period_bounds(D.Period("week", 0), NOW, "sunday")
    assert (s.date(), e.date()) == (date(2026, 10, 25), date(2026, 11, 1))
    s, e = D.period_bounds(D.Period("week", 2), NOW)
    assert s.date() == date(2026, 10, 5)


def test_month_and_year_bounds():
    s, e = D.period_bounds(D.Period("month", 0), NOW)
    assert (s.date(), e.date()) == (date(2026, 10, 1), date(2026, 11, 1))
    s, e = D.period_bounds(D.Period("month", 10), NOW)   # wraps into previous year
    assert (s.date(), e.date()) == (date(2025, 12, 1), date(2026, 1, 1))
    s, e = D.period_bounds(D.Period("year", 1), NOW)
    assert (s.date(), e.date()) == (date(2025, 1, 1), date(2026, 1, 1))


def test_labels():
    assert D.period_label(D.Period("week", 0), NOW) == "THIS WEEK"
    assert D.period_label(D.Period("week", 1), NOW) == "LAST WEEK"
    assert D.period_label(D.Period("month", 2), NOW) == "AUGUST"
    assert D.period_label(D.Period("year", 0), NOW) == "2026 YTD"
    assert D.Period.from_config({"period": "month", "offset": "x"}) == D.Period("month", 0)
    assert D.Period.from_config("bogus") == D.Period("week", 0)


def test_sport_filter_drops_ride(runs):
    assert all(r.sport_type != "Ride" for r in runs)
    assert len(runs) == 11


def test_leaderboard_this_week(runs, athletes):
    s, e = D.period_bounds(D.Period("week", 0), NOW)
    rows = D.leaderboard(runs, athletes, s, e)
    assert [r.athlete.name for r in rows] == ["Bridger", "Roommate"]
    assert rows[0].rank == 1 and rows[1].rank == 2
    assert round(rows[0].distance("mi"), 1) == 21.7
    assert rows[0].count == 4
    assert round(rows[1].distance("mi"), 1) == 17.7


def test_leaderboard_ties_share_rank(athletes):
    s, e = D.period_bounds(D.Period("week", 0), NOW)
    mk = lambda aid, i, miles: D.Run(str(i), aid, "r", datetime(2026, 10, 20, 8), miles * D.METERS_PER_MILE, 1800, "Run")
    rows = D.leaderboard([mk("1001", 1, 5.0), mk("2002", 2, 5.0)], athletes, s, e)
    assert [r.rank for r in rows] == [1, 1]


def test_streaks(runs):
    mine = [r for r in runs if r.athlete_id == "1001"]
    assert D.streak_days(mine, NOW.date()) == 3          # Fri, Sat, Sun
    theirs = [r for r in runs if r.athlete_id == "2002"]
    assert D.streak_days(theirs, NOW.date()) == 1        # yesterday only
    assert D.streak_days([], NOW.date()) == 0


def test_records(runs, athletes):
    recs = D.records(runs, athletes, NOW)
    by = {r.athlete.name: r for r in recs}
    assert by["Bridger"].streak == 3
    assert by["Bridger"].longest_month.name == "Last week long"     # 11.0 mi, in October
    assert by["Bridger"].fastest_week.name == "Tempo"
    assert by["Roommate"].longest_month.name == "Longest of month"
    assert by["Roommate"].fastest_week.name == "Treadmill intervals"
    assert round(by["Roommate"].week_distance_m / D.METERS_PER_MILE, 1) == 17.7


def test_formatting():
    assert D.fmt_pace(486) == "8:06"
    assert D.fmt_pace(None) == "--:--"
    assert D.fmt_duration(3730) == "1:02:10"
    assert D.fmt_duration(610) == "10:10"
    assert D.fmt_distance(123.4) == "123"
    assert D.fmt_day(NOW.date(), NOW.date()) == "TODAY"
    assert D.fmt_day(date(2026, 10, 24), NOW.date()) == "YDAY"
    assert D.fmt_day(date(2026, 10, 20), NOW.date()) == "TUE"
    assert D.fmt_day(date(2026, 9, 1), NOW.date()) == "9/1"
    assert D.fmt_day_short(NOW.date(), NOW.date()) == "SUN"
    assert D.fmt_day_short(date(2026, 10, 1), NOW.date()) == "10/1"


def test_resolve_athletes_merges_config_and_tokens():
    tokens = {
        "1001": {"athlete": {"id": 1001, "firstname": "Bridger"}},
        "2002": {"athlete": {"id": 2002, "firstname": "Sam"}},
        "3003": {"athlete": {"id": 3003, "firstname": "Extra"}},
    }
    cfg = [
        {"name": "Bridger", "strava_id": None, "shirt_color": "#ff0000", "accessory": "headband"},
        {"name": "Sammy", "strava_id": 2002, "shirt_color": [1, 2, 3], "accessory": "bogus"},
    ]
    out = D.resolve_athletes(cfg, tokens)
    assert [a.id for a in out] == ["1001", "2002", "3003"]
    assert out[0].shirt == (255, 0, 0) and out[0].accessory == "headband" and out[0].connected
    assert out[1].name == "Sammy" and out[1].shirt == (1, 2, 3) and out[1].accessory == "none"
    assert out[2].name == "Extra" and out[2].connected
    pending = D.resolve_athletes([{"name": "Nobody"}], {})
    assert pending[0].id.startswith("pending-") and not pending[0].connected


def test_parse_activity_handles_missing_fields():
    r = D.parse_activity({"id": 5, "distance": None, "start_date_local": "garbage"}, athlete_id="9")
    assert r.athlete_id == "9" and r.distance_m == 0.0 and r.pace_s("mi") is None
    assert r.start_local.year == 1970
