"""Pure data model and aggregation for the Strava plugin. No I/O, no PIL."""

from __future__ import annotations

import calendar
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

METERS_PER_MILE = 1609.344
DEFAULT_SPORT_TYPES = ("Run", "TrailRun", "VirtualRun")
MONTHS = ["JANUARY", "FEBRUARY", "MARCH", "APRIL", "MAY", "JUNE", "JULY",
          "AUGUST", "SEPTEMBER", "OCTOBER", "NOVEMBER", "DECEMBER"]
WEEKDAYS = ["MON", "TUE", "WED", "THU", "FRI", "SAT", "SUN"]

RGB = Tuple[int, int, int]

DEFAULT_SKIN: RGB = (236, 196, 150)
DEFAULT_HAIR: RGB = (196, 164, 88)   # dirty blonde
DEFAULT_SHOES: RGB = (240, 240, 240)
OUTFITS: List[Tuple[RGB, RGB]] = [    # (shirt, shorts) defaults by athlete order
    ((252, 76, 2), (30, 30, 40)),      # Strava orange / charcoal
    ((40, 140, 255), (245, 245, 245)), # blue / white
    ((60, 200, 90), (30, 30, 40)),     # green / charcoal
    ((230, 60, 200), (30, 30, 40)),    # magenta / charcoal
    ((250, 220, 40), (50, 50, 160)),   # yellow / navy
]
ACCESSORIES = ("none", "headband", "cap", "sunglasses")


# --------------------------------------------------------------------------- #
# Models
# --------------------------------------------------------------------------- #
@dataclass
class Athlete:
    id: str
    name: str
    shirt: RGB
    shorts: RGB
    accessory: str = "none"
    skin: RGB = DEFAULT_SKIN
    hair: RGB = DEFAULT_HAIR
    shoes: RGB = DEFAULT_SHOES
    connected: bool = True

    @property
    def color(self) -> RGB:
        return self.shirt

    @property
    def label(self) -> str:
        return (self.name or "?").upper()[:8]


@dataclass
class Run:
    id: str
    athlete_id: str
    name: str
    start_local: datetime           # naive, athlete's local time
    distance_m: float
    moving_time_s: int
    sport_type: str
    polyline: Optional[str] = None
    elevation_m: float = 0.0
    raw: Dict[str, Any] = field(default_factory=dict, repr=False)

    @property
    def miles(self) -> float:
        return self.distance_m / METERS_PER_MILE

    @property
    def km(self) -> float:
        return self.distance_m / 1000.0

    def distance(self, units: str) -> float:
        return self.km if units == "km" else self.miles

    def pace_s(self, units: str) -> Optional[float]:
        d = self.distance(units)
        if d <= 0.05 or self.moving_time_s <= 0:
            return None
        return self.moving_time_s / d

    @property
    def day(self) -> date:
        return self.start_local.date()


def parse_activity(a: Dict[str, Any], athlete_id: Optional[str] = None) -> Run:
    """Build a ``Run`` from a Strava SummaryActivity dict."""
    start = str(a.get("start_date_local") or a.get("start_date") or "1970-01-01T00:00:00Z")
    start = start.replace("Z", "")
    try:
        start_dt = datetime.fromisoformat(start)
    except ValueError:
        start_dt = datetime(1970, 1, 1)
    if start_dt.tzinfo is not None:
        start_dt = start_dt.replace(tzinfo=None)
    aid = athlete_id or str((a.get("athlete") or {}).get("id") or "")
    m = a.get("map") or {}
    return Run(
        id=str(a.get("id", "")),
        athlete_id=str(aid),
        name=str(a.get("name") or "Run"),
        start_local=start_dt,
        distance_m=float(a.get("distance") or 0.0),
        moving_time_s=int(a.get("moving_time") or 0),
        sport_type=str(a.get("sport_type") or a.get("type") or ""),
        polyline=m.get("summary_polyline") or m.get("polyline") or None,
        elevation_m=float(a.get("total_elevation_gain") or 0.0),
        raw=a,
    )


# --------------------------------------------------------------------------- #
# Periods
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class Period:
    kind: str = "week"      # week | month | year
    offset: int = 0         # 0 = current, 1 = previous, ...

    @staticmethod
    def from_config(item: Any) -> "Period":
        if isinstance(item, str):
            return Period(item if item in ("week", "month", "year") else "week", 0)
        if isinstance(item, dict):
            kind = str(item.get("period", item.get("kind", "week")))
            if kind not in ("week", "month", "year"):
                kind = "week"
            try:
                off = max(0, int(item.get("offset", 0)))
            except (TypeError, ValueError):
                off = 0
            return Period(kind, off)
        return Period()


def week_start_date(d: date, week_start: str = "monday") -> date:
    first = 6 if week_start == "sunday" else 0
    return d - timedelta(days=(d.weekday() - first) % 7)


def _shift_month(y: int, m: int, delta: int) -> Tuple[int, int]:
    idx = y * 12 + (m - 1) - delta
    return idx // 12, idx % 12 + 1


def period_bounds(period: Period, now: datetime, week_start: str = "monday") -> Tuple[datetime, datetime]:
    """Half-open ``[start, end)`` naive local datetimes for a period."""
    today = now.date()
    if period.kind == "week":
        start_d = week_start_date(today, week_start) - timedelta(weeks=period.offset)
        end_d = start_d + timedelta(days=7)
    elif period.kind == "month":
        y, m = _shift_month(today.year, today.month, period.offset)
        start_d = date(y, m, 1)
        end_d = date(*_shift_month(y, m, -1), 1)
    else:  # year
        y = today.year - period.offset
        start_d, end_d = date(y, 1, 1), date(y + 1, 1, 1)
    return (datetime.combine(start_d, datetime.min.time()),
            datetime.combine(end_d, datetime.min.time()))


def period_label(period: Period, now: datetime, week_start: str = "monday") -> str:
    start, _ = period_bounds(period, now, week_start)
    if period.kind == "week":
        if period.offset == 0:
            return "THIS WEEK"
        if period.offset == 1:
            return "LAST WEEK"
        return f"{period.offset} WKS AGO"
    if period.kind == "month":
        if period.offset == 0:
            return "THIS MONTH"
        if period.offset == 1:
            return "LAST MONTH"
        return MONTHS[start.month - 1]
    if period.offset == 0:
        return f"{start.year} YTD"
    return str(start.year)


def period_short_label(period: Period, now: datetime, week_start: str = "monday") -> str:
    """Compact label for narrow panels."""
    start, _ = period_bounds(period, now, week_start)
    if period.kind == "week":
        return "WEEK" if period.offset == 0 else f"WK-{period.offset}"
    if period.kind == "month":
        return MONTHS[start.month - 1][:3]
    return str(start.year)


# --------------------------------------------------------------------------- #
# Aggregation
# --------------------------------------------------------------------------- #
def is_counted(run: Run, sport_types: Iterable[str] = DEFAULT_SPORT_TYPES) -> bool:
    allowed = set(sport_types)
    return run.sport_type in allowed or (not run.sport_type and "Run" in allowed)


def runs_between(runs: Iterable[Run], start: datetime, end: datetime,
                 athlete_id: Optional[str] = None) -> List[Run]:
    out = [r for r in runs if start <= r.start_local < end
           and (athlete_id is None or r.athlete_id == athlete_id)]
    out.sort(key=lambda r: r.start_local)
    return out


@dataclass
class BoardRow:
    athlete: Athlete
    distance_m: float
    count: int
    rank: int = 0

    def distance(self, units: str) -> float:
        return self.distance_m / (1000.0 if units == "km" else METERS_PER_MILE)


def leaderboard(runs: Iterable[Run], athletes: Sequence[Athlete], start: datetime,
                end: datetime) -> List[BoardRow]:
    totals: Dict[str, List[float]] = {a.id: [0.0, 0] for a in athletes}
    for r in runs_between(runs, start, end):
        if r.athlete_id in totals:
            totals[r.athlete_id][0] += r.distance_m
            totals[r.athlete_id][1] += 1
    rows = [BoardRow(a, totals[a.id][0], int(totals[a.id][1])) for a in athletes]
    rows.sort(key=lambda row: (-row.distance_m, -row.count, row.athlete.name.lower()))
    rank = 0
    prev = None
    for i, row in enumerate(rows):
        if prev is None or abs(row.distance_m - prev) > 1.0:
            rank = i + 1
        row.rank = rank
        prev = row.distance_m
    return rows


def streak_days(runs: Iterable[Run], today: date) -> int:
    """Consecutive calendar days with at least one run, ending today or yesterday."""
    days = {r.day for r in runs}
    if not days:
        return 0
    cursor = today if today in days else today - timedelta(days=1)
    if cursor not in days:
        return 0
    n = 0
    while cursor in days:
        n += 1
        cursor -= timedelta(days=1)
    return n


@dataclass
class RecordRow:
    athlete: Athlete
    streak: int
    longest_month: Optional[Run]
    fastest_week: Optional[Run]
    week_distance_m: float
    week_count: int


def records(runs: Iterable[Run], athletes: Sequence[Athlete], now: datetime,
            week_start: str = "monday", units: str = "mi",
            min_fast_distance_m: float = METERS_PER_MILE) -> List[RecordRow]:
    runs = list(runs)
    w_start, w_end = period_bounds(Period("week", 0), now, week_start)
    m_start, m_end = period_bounds(Period("month", 0), now, week_start)
    out: List[RecordRow] = []
    for a in athletes:
        mine = [r for r in runs if r.athlete_id == a.id]
        week = runs_between(mine, w_start, w_end)
        month = runs_between(mine, m_start, m_end)
        longest = max(month, key=lambda r: r.distance_m, default=None)
        eligible = [r for r in week if r.distance_m >= min_fast_distance_m and r.pace_s(units)]
        fastest = min(eligible, key=lambda r: r.pace_s(units), default=None)
        out.append(RecordRow(
            athlete=a,
            streak=streak_days(mine, now.date()),
            longest_month=longest,
            fastest_week=fastest,
            week_distance_m=sum(r.distance_m for r in week),
            week_count=len(week),
        ))
    return out


# --------------------------------------------------------------------------- #
# Athlete resolution (config + token file)
# --------------------------------------------------------------------------- #
def _rgb(value: Any, default: RGB) -> RGB:
    try:
        if isinstance(value, str) and value.startswith("#") and len(value) == 7:
            return (int(value[1:3], 16), int(value[3:5], 16), int(value[5:7], 16))
        r, g, b = (int(v) for v in value)  # type: ignore[misc]
        return (max(0, min(255, r)), max(0, min(255, g)), max(0, min(255, b)))
    except Exception:
        return default


def resolve_athletes(config_athletes: Any, token_entries: Dict[str, Dict[str, Any]]) -> List[Athlete]:
    """Merge the config's athlete table with whoever is connected in token.json.

    Config order wins; connected athletes missing from the config are appended
    with their Strava first name and a default outfit. Config rows without a
    ``strava_id`` are matched by (case-insensitive) first name, which lets you
    pre-fill the table before the person connects.
    """
    out: List[Athlete] = []
    used: set = set()
    rows = config_athletes if isinstance(config_athletes, list) else []
    by_name = {
        str((e.get("athlete") or {}).get("firstname", "")).strip().lower(): aid
        for aid, e in token_entries.items()
    }
    for i, row in enumerate(rows):
        if not isinstance(row, dict):
            continue
        sid = row.get("strava_id")
        sid = str(int(sid)) if isinstance(sid, (int, float)) and sid else (str(sid).strip() if sid else "")
        name = str(row.get("name") or "").strip()
        if not sid and name.lower() in by_name:
            sid = by_name[name.lower()]
        entry = token_entries.get(sid, {}) if sid else {}
        if not name:
            name = str((entry.get("athlete") or {}).get("firstname") or f"Runner {i + 1}")
        shirt_d, shorts_d = OUTFITS[i % len(OUTFITS)]
        acc = str(row.get("accessory") or "none")
        out.append(Athlete(
            id=sid or f"pending-{i}",
            name=name,
            shirt=_rgb(row.get("shirt_color"), shirt_d),
            shorts=_rgb(row.get("shorts_color"), shorts_d),
            accessory=acc if acc in ACCESSORIES else "none",
            skin=_rgb(row.get("skin_color"), DEFAULT_SKIN),
            hair=_rgb(row.get("hair_color"), DEFAULT_HAIR),
            connected=bool(entry),
        ))
        if sid:
            used.add(sid)
    for aid, entry in token_entries.items():
        if aid in used:
            continue
        i = len(out)
        shirt_d, shorts_d = OUTFITS[i % len(OUTFITS)]
        first = str((entry.get("athlete") or {}).get("firstname") or f"Runner {i + 1}")
        out.append(Athlete(id=aid, name=first, shirt=shirt_d, shorts=shorts_d, connected=True))
    return out


# --------------------------------------------------------------------------- #
# Formatting
# --------------------------------------------------------------------------- #
def fmt_distance(value: float, decimals: int = 1) -> str:
    if value >= 100:
        return f"{value:.0f}"
    return f"{value:.{decimals}f}"


def fmt_duration(seconds: float) -> str:
    s = int(round(seconds))
    h, rem = divmod(s, 3600)
    m, sec = divmod(rem, 60)
    if h:
        return f"{h}:{m:02d}:{sec:02d}"
    return f"{m}:{sec:02d}"


def fmt_pace(seconds_per_unit: Optional[float]) -> str:
    if not seconds_per_unit or seconds_per_unit <= 0 or seconds_per_unit > 59 * 60:
        return "--:--"
    m, s = divmod(int(round(seconds_per_unit)), 60)
    return f"{m}:{s:02d}"


def fmt_day(d: date, today: date) -> str:
    if d == today:
        return "TODAY"
    if d == today - timedelta(days=1):
        return "YDAY"
    if 0 <= (today - d).days < 7:
        return WEEKDAYS[d.weekday()]
    return f"{d.month}/{d.day}"


def fmt_day_short(d: date, today: date) -> str:
    """Weekday for the last 7 days, else M/D. Fits a 12 px column in Tom Thumb."""
    if 0 <= (today - d).days < 7:
        return WEEKDAYS[d.weekday()]
    return f"{d.month}/{d.day}"


def unit_suffix(units: str) -> str:
    return "km" if units == "km" else "mi"


def days_in_month(d: date) -> int:
    return calendar.monthrange(d.year, d.month)[1]
