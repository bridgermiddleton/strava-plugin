"""Frame renderers for the Strava plugin. Pure: (size, state, time) -> PIL image.

Designed on a 128x32 panel. Panels at least twice as tall are rendered at
``(w // k, h // k)`` and upscaled ``k`` times with nearest-neighbour so the
pixel art stays crisp. Widths under 96 use compact layouts.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from pathlib import Path
from typing import List, Optional, Sequence, Tuple

from PIL import Image, ImageDraw, ImageFont

import strava_sprites as sprites
from strava_data import (
    Athlete, BoardRow, RecordRow, Run, fmt_distance, fmt_duration, fmt_pace, unit_suffix,
)

RGB = Tuple[int, int, int]
FONT_DIR = Path(__file__).resolve().parent / "assets" / "fonts"

WHITE: RGB = (255, 255, 255)
GREY: RGB = (110, 110, 120)
DIM: RGB = (55, 55, 65)
YELLOW: RGB = (255, 215, 0)
GREEN: RGB = (60, 220, 90)
RED: RGB = (240, 70, 70)
TRACK: RGB = (70, 70, 80)
ORANGE: RGB = (252, 76, 2)


# --------------------------------------------------------------------------- #
# Fonts
# --------------------------------------------------------------------------- #
class Fonts:
    """Bitmap fonts at their native pixel size (BDF only renders crisp there)."""

    _cache: dict = {}

    def __init__(self) -> None:
        self.tiny = self._load("tom-thumb.bdf", 6)      # 3x5 glyphs, 6 px line
        self.small = self._load("4x6.bdf", 6)           # 4x6
        self.medium = self._load("5x7.bdf", 7)          # 5x7
        self.large = self._load("6x10.bdf", 10)         # 6x10

    @classmethod
    def _load(cls, name: str, size: int):
        key = (name, size)
        if key not in cls._cache:
            try:
                cls._cache[key] = ImageFont.truetype(str(FONT_DIR / name), size)
            except Exception:
                cls._cache[key] = ImageFont.load_default()
        return cls._cache[key]

    @staticmethod
    def width(text: str, font) -> int:
        try:
            return int(math.ceil(font.getlength(text)))
        except Exception:
            return len(text) * 6

    @staticmethod
    def height(font) -> int:
        try:
            a, d = font.getmetrics()
            return int(a + d)
        except Exception:
            return 8


_FONTS: Optional[Fonts] = None


def fonts() -> Fonts:
    global _FONTS
    if _FONTS is None:
        _FONTS = Fonts()
    return _FONTS


def text(draw: ImageDraw.ImageDraw, xy: Tuple[int, int], s: str, font, fill: RGB = WHITE,
         align: str = "left", max_w: Optional[int] = None) -> int:
    """Draw crisp (non-antialiased) text; returns drawn width. ``align`` is
    relative to x: left, right or center. Truncates to ``max_w`` if given."""
    draw.fontmode = "1"
    if max_w is not None:
        while s and Fonts.width(s, font) > max_w:
            s = s[:-1]
    w = Fonts.width(s, font)
    x, y = xy
    if align == "right":
        x -= w
    elif align == "center":
        x -= w // 2
    draw.text((x, y), s, font=font, fill=fill)
    return w


def paste(img: Image.Image, sprite: Image.Image, xy: Tuple[int, int]) -> None:
    img.paste(sprite, (int(xy[0]), int(xy[1])), sprite)


def dim(color: RGB, f: float) -> RGB:
    return (int(color[0] * f), int(color[1] * f), int(color[2] * f))


# --------------------------------------------------------------------------- #
# Size handling
# --------------------------------------------------------------------------- #
def upscale_factor(w: int, h: int) -> int:
    """Integer factor so the logical canvas is >= 64 wide and 32 tall."""
    k = max(1, h // 32)
    while k > 1 and w // k < 64:
        k -= 1
    return k


def _canvas(w: int, h: int) -> Tuple[Image.Image, ImageDraw.ImageDraw]:
    img = Image.new("RGB", (w, h), (0, 0, 0))
    d = ImageDraw.Draw(img)
    d.fontmode = "1"
    return img, d


def finish(img: Image.Image, w: int, h: int, k: int) -> Image.Image:
    if k > 1:
        img = img.resize((img.width * k, img.height * k), Image.NEAREST)
    if img.size != (w, h):
        out = Image.new("RGB", (w, h), (0, 0, 0))
        out.paste(img, ((w - img.width) // 2, (h - img.height) // 2))
        img = out
    return img


# --------------------------------------------------------------------------- #
# Route projection
# --------------------------------------------------------------------------- #
def project_route(coords: Sequence[Tuple[float, float]], box: Tuple[int, int, int, int],
                  margin: int = 1) -> List[Tuple[int, int]]:
    """Fit lat/lon points into ``box=(x, y, w, h)`` preserving aspect ratio."""
    if not coords:
        return []
    x0, y0, bw, bh = box
    bw, bh = max(1, bw - 2 * margin), max(1, bh - 2 * margin)
    lats = [c[0] for c in coords]
    lons = [c[1] for c in coords]
    lat_min, lat_max = min(lats), max(lats)
    lon_min, lon_max = min(lons), max(lons)
    mid_lat = (lat_min + lat_max) / 2.0
    cos_lat = max(0.05, math.cos(math.radians(mid_lat)))
    span_x = (lon_max - lon_min) * cos_lat
    span_y = lat_max - lat_min
    scale = min(bw / span_x if span_x > 1e-9 else float("inf"),
                bh / span_y if span_y > 1e-9 else float("inf"))
    if math.isinf(scale):
        scale = 1.0
    used_w = span_x * scale
    used_h = span_y * scale
    off_x = x0 + margin + (bw - used_w) / 2.0
    off_y = y0 + margin + (bh - used_h) / 2.0
    pts = []
    for lat, lon in coords:
        px = off_x + (lon - lon_min) * cos_lat * scale
        py = off_y + (lat_max - lat) * scale
        pts.append((int(round(px)), int(round(py))))
    return pts


def draw_route(draw: ImageDraw.ImageDraw, pts: Sequence[Tuple[int, int]], color: RGB,
               progress: float = 1.0, trail: RGB = None) -> None:
    """Draw the route; ``progress`` in [0,1] traces it progressively with a
    dim 'ghost' of the remaining path. Start dot green, head/end dot."""
    if not pts:
        return
    n = len(pts)
    if n == 1:
        draw.point(pts[0], fill=color)
        return
    if trail is None:
        trail = dim(color, 0.18)
    if progress < 1.0:
        draw.line(list(pts), fill=trail, width=1)
    head = max(1, min(n - 1, int(round(progress * (n - 1)))))
    draw.line(list(pts[: head + 1]), fill=color, width=1)
    draw.point(pts[0], fill=GREEN)
    if progress >= 1.0:
        draw.point(pts[-1], fill=RED)
    else:
        draw.point(pts[head], fill=WHITE)


# --------------------------------------------------------------------------- #
# Shared bits
# --------------------------------------------------------------------------- #
def _header(draw, w: int, title: str, right: str = "", color: RGB = WHITE,
            right_color: RGB = GREY) -> int:
    f = fonts()
    text(draw, (1, 0), title, f.small, color, max_w=w - (Fonts.width(right, f.small) + 4 if right else 2))
    if right:
        text(draw, (w - 1, 0), right, f.small, right_color, align="right")
    draw.line([(0, 7), (w - 1, 7)], fill=DIM)
    return 8


def paginate(n_items: int, per_page: int, t: float, seconds_per_page: float) -> Tuple[int, int]:
    """(page_index, page_count) for time ``t``."""
    pages = max(1, math.ceil(n_items / max(1, per_page)))
    if pages == 1 or seconds_per_page <= 0:
        return 0, pages
    return int(t // seconds_per_page) % pages, pages


# --------------------------------------------------------------------------- #
# View 1: leaderboard
# --------------------------------------------------------------------------- #
def render_leaderboard(w: int, h: int, title: str, rows: Sequence[BoardRow], units: str,
                       t: float = 0.0, seconds_per_page: float = 5.0) -> Image.Image:
    k = upscale_factor(w, h)
    lw, lh = w // k, h // k
    img, d = _canvas(lw, lh)
    f = fonts()
    top = _header(d, lw, title, unit_suffix(units).upper())
    avail = lh - top
    n = len(rows)
    if n == 0:
        text(d, (lw // 2, top + (avail - 6) // 2), "NO ATHLETES", f.small, GREY, align="center")
        return finish(img, w, h, k)
    row_h = 12 if avail >= 24 and n <= avail // 12 else 8
    per_page = max(1, avail // row_h)
    page, pages = paginate(n, per_page, t, seconds_per_page)
    visible = rows[page * per_page:(page + 1) * per_page]
    leader = max((r.distance_m for r in rows), default=0.0)
    narrow = lw < 96
    for i, row in enumerate(visible):
        y = top + i * row_h
        a = row.athlete
        dist = fmt_distance(row.distance(units))
        # rank
        text(d, (1, y + (row_h - 6) // 2), str(row.rank), f.small, YELLOW if row.rank == 1 else GREY)
        # bust (+ crown for the leader on tall rows)
        bx = 7
        b = sprites.bust(a)
        if row.rank == 1 and row.distance_m > 0 and row_h >= 12:
            paste(img, sprites.crown(), (bx, y + 1))
            paste(img, b, (bx, y + 5))
        else:
            paste(img, b, (bx, y + (row_h - b.height) // 2))
        # name
        name_x = bx + 7
        name_font = f.small
        name_y = y + 1 if row_h >= 12 else y + (row_h - 6) // 2
        dist_font = f.medium if row_h >= 12 else f.small
        dist_w = Fonts.width(dist, dist_font)
        name_max = (lw - 2 - dist_w) - name_x - 2 if narrow else 40
        text(d, (name_x, name_y), a.label, name_font, a.color, max_w=max(8, name_max))
        # distance (right aligned)
        dy = y + (row_h - Fonts.height(dist_font)) // 2 if row_h < 12 else y + 2
        text(d, (lw - 1, dy), dist, dist_font, WHITE if row.distance_m > 0 else GREY, align="right")
        # proportional bar (wide panels only)
        if not narrow:
            bar_x0 = name_x
            bar_x1 = lw - 2 - dist_w - 3
            if row_h >= 12:
                by0, by1 = y + 8, y + 10
            else:
                bar_x0 = name_x + 42
                by0, by1 = y + row_h // 2 - 1, y + row_h // 2 + 1
            if bar_x1 - bar_x0 >= 8:
                d.rectangle([bar_x0, by0, bar_x1, by1], fill=DIM)
                if leader > 0 and row.distance_m > 0:
                    fill_w = int(round((bar_x1 - bar_x0) * row.distance_m / leader))
                    d.rectangle([bar_x0, by0, bar_x0 + fill_w, by1], fill=a.color)
    if pages > 1:
        # page dots bottom-right
        for p in range(pages):
            d.point((lw - 2 - (pages - 1 - p) * 2, lh - 1), fill=WHITE if p == page else DIM)
    return finish(img, w, h, k)


# --------------------------------------------------------------------------- #
# View 2: run card
# --------------------------------------------------------------------------- #
def render_run_card(w: int, h: int, athlete: Athlete, run: Run, coords: Sequence[Tuple[float, float]],
                    units: str, day_label: str, t: float, card_seconds: float = 6.0,
                    trace: bool = True, is_today: bool = False) -> Image.Image:
    k = upscale_factor(w, h)
    lw, lh = w // k, h // k
    img, d = _canvas(lw, lh)
    f = fonts()
    narrow = lw < 96

    # progress of the route trace: finish with ~1s to spare, hold at the end
    progress = 1.0
    if trace and card_seconds > 1.5:
        progress = min(1.0, max(0.0, t / (card_seconds - 1.2)))

    # route box on the left
    box_size = min(lh - 2, 30)
    box = (1, 1, box_size, box_size)
    if coords:
        pts = project_route(coords, box)
        draw_route(d, pts, athlete.color, progress)
    else:
        tm = sprites.treadmill()
        paste(img, tm, (box[0] + (box_size - tm.width) // 2, box[1] + (box_size - tm.height) // 2 - 3))
        text(d, (box[0] + box_size // 2, box[1] + box_size // 2 + 5), "INDOOR", f.tiny, GREY, align="center")

    x = box_size + 3
    dist = fmt_distance(run.distance(units))
    unit = unit_suffix(units)
    pace = fmt_pace(run.pace_s(units))
    dur = fmt_duration(run.moving_time_s)

    if narrow:
        text(d, (x, 1), athlete.label, f.small, athlete.color, max_w=lw - x - 1)
        text(d, (x, 9), dist, f.medium, WHITE)
        text(d, (x + Fonts.width(dist, f.medium) + 1, 10), unit, f.tiny, GREY)
        text(d, (x, 18), dur, f.small, WHITE, max_w=lw - x - 1)
        text(d, (x, 25), pace, f.small, GREY, max_w=lw - x - 1)
        return finish(img, w, h, k)

    # column A: day label over the animated runner
    frames = sprites.runner_frames(athlete)
    fr = frames[sprites.frame_index(t)] if run.distance_m > 0 else sprites.standing(athlete)
    text(d, (x, 1), day_label, f.tiny, WHITE if is_today else GREY, max_w=12)
    sy = lh - 2 - fr.height
    paste(img, fr, (x, sy))
    d.line([(x - 1, lh - 2), (x + 10, lh - 2)], fill=TRACK)

    # column B: name, big distance, duration + pace
    sx = x + 14
    elev_w = 0
    if lw >= 128 and run.elevation_m > 0:
        elev = f"{run.elevation_m * (1.0 if units == 'km' else 3.28084):.0f}{'m' if units == 'km' else 'ft'}"
        elev_w = text(d, (lw - 1, 1), elev, f.tiny, GREY, align="right") + 3
    text(d, (sx, 1), athlete.label, f.small, athlete.color, max_w=lw - sx - 1 - elev_w)
    dist_w = text(d, (sx, 8), dist, f.large, WHITE)
    text(d, (sx + dist_w + 2, 12), unit, f.small, GREY)
    text(d, (sx, 21), dur, f.medium, WHITE)
    text(d, (lw - 1, 21), f"{pace}/{unit}", f.medium, GREY, align="right")
    return finish(img, w, h, k)


# --------------------------------------------------------------------------- #
# View 3: race
# --------------------------------------------------------------------------- #
@dataclass
class RaceEntry:
    athlete: Athlete
    distance: float      # in configured units


def render_race(w: int, h: int, entries: Sequence[RaceEntry], goal: float, units: str,
                t: float, title: str = "RACE", seconds_per_page: float = 6.0) -> Image.Image:
    k = upscale_factor(w, h)
    lw, lh = w // k, h // k
    img, d = _canvas(lw, lh)
    f = fonts()
    unit = unit_suffix(units)
    goal = max(0.1, goal)
    ordered = sorted(entries, key=lambda e: -e.distance)
    # header: goal + gap between first and second
    right = ""
    right_color = GREY
    if len(ordered) >= 2 and ordered[0].distance > ordered[1].distance:
        right = f"+{fmt_distance(ordered[0].distance - ordered[1].distance)}"
        right_color = ordered[0].athlete.color
    elif len(ordered) >= 2:
        right = "TIED"
    head = f"GOAL {fmt_distance(goal, 0)}" if lw < 96 else f"{title} TO {fmt_distance(goal, 0)}{unit}"
    top = _header(d, lw, head, right, WHITE, right_color)

    avail = lh - top
    lane_h = 12
    per_page = max(1, avail // lane_h)
    page, pages = paginate(len(entries), per_page, t, seconds_per_page)
    visible = list(entries)[page * per_page:(page + 1) * per_page]
    narrow = lw < 96
    label_w = 20 if narrow else 34
    flag = sprites.flag()
    track_x0 = label_w + 2
    track_x1 = lw - 2 - flag.width - 1
    leader_dist = ordered[0].distance if ordered else 0.0
    for i, e in enumerate(visible):
        y = top + i * lane_h
        ground = y + lane_h - 1
        a = e.athlete
        # labels
        text(d, (1, y), a.label, f.small, a.color, max_w=label_w - 1)
        is_leader = leader_dist > 0 and e.distance >= leader_dist
        text(d, (1, y + 6), fmt_distance(e.distance), f.small, YELLOW if is_leader else GREY, max_w=label_w - 1)
        # track + finish flag
        d.line([(track_x0, ground), (track_x1 + 1, ground)], fill=TRACK)
        for tx in range(track_x0, track_x1, 8):
            d.point((tx, ground), fill=DIM)
        paste(img, flag, (track_x1 + 2, ground - flag.height + 1))
        # runner
        frac = min(1.0, e.distance / goal)
        run_len = track_x1 - track_x0 - 8
        rx = track_x0 + int(round(frac * run_len))
        frames = sprites.runner_frames(a)
        if e.distance <= 0:
            fr = sprites.standing(a)
        elif frac >= 1.0:
            fr = sprites.standing(a)
        else:
            # leader animates faster
            fps = 10.0 if is_leader else 7.0
            fr = frames[sprites.frame_index(t, fps)]
        paste(img, fr, (rx, ground - fr.height + 1))
        if frac >= 1.0:
            tr = sprites.trophy()
            paste(img, tr, (rx - tr.width - 1, ground - tr.height))
        elif is_leader and len(entries) > 1:
            paste(img, sprites.crown(), (rx + 1, ground - fr.height - 2 + (1 if fr.getbbox() and fr.getbbox()[1] else 0)))
    if pages > 1:
        for p in range(pages):
            d.point((lw - 2 - (pages - 1 - p) * 2, lh - 1), fill=WHITE if p == page else DIM)
    return finish(img, w, h, k)


# --------------------------------------------------------------------------- #
# View 4: records
# --------------------------------------------------------------------------- #
def render_records(w: int, h: int, rows: Sequence[RecordRow], units: str, t: float = 0.0,
                   seconds_per_page: float = 5.0) -> Image.Image:
    k = upscale_factor(w, h)
    lw, lh = w // k, h // k
    img, d = _canvas(lw, lh)
    f = fonts()
    narrow = lw < 96
    unit = unit_suffix(units)
    # column x positions
    if narrow:
        cols = {"name": None, "streak": 8, "long": 28, "pace": 46}
    else:
        cols = {"name": 8, "streak": 46, "long": 72, "pace": 104}
    # header row with column labels
    d.fontmode = "1"
    text(d, (1, 0), "RECORDS" if not narrow else "REC", f.small, WHITE)
    text(d, (cols["streak"], 1), "STRK", f.tiny, GREY)
    text(d, (cols["long"], 1), "LONG" if narrow else f"LONG {unit.upper()}", f.tiny, GREY)
    text(d, (cols["pace"], 1), "PACE", f.tiny, GREY)
    d.line([(0, 7), (lw - 1, 7)], fill=DIM)
    top = 8
    avail = lh - top
    n = len(rows)
    if n == 0:
        text(d, (lw // 2, top + (avail - 6) // 2), "NO DATA", f.small, GREY, align="center")
        return finish(img, w, h, k)
    row_h = 12 if n * 12 <= avail else 8
    per_page = max(1, avail // row_h)
    page, pages = paginate(n, per_page, t, seconds_per_page)
    visible = rows[page * per_page:(page + 1) * per_page]
    best_streak = max((r.streak for r in rows), default=0)
    best_long = max((r.longest_month.distance_m for r in rows if r.longest_month), default=0)
    best_pace = min((r.fastest_week.pace_s(units) for r in rows if r.fastest_week), default=None)
    fl = sprites.flame()
    for i, r in enumerate(visible):
        y = top + i * row_h
        cy = y + (row_h - 6) // 2
        a = r.athlete
        b = sprites.bust(a)
        paste(img, b, (1, y + (row_h - b.height) // 2))
        if cols["name"] is not None:
            name_max = cols["streak"] - cols["name"] - 2
            text(d, (cols["name"], cy), a.label, f.small, a.color, max_w=name_max)
        # streak with flame
        if r.streak > 0:
            paste(img, fl, (cols["streak"], cy))
            text(d, (cols["streak"] + fl.width + 1, cy), f"{r.streak}d", f.small,
                 YELLOW if r.streak == best_streak else WHITE)
        else:
            text(d, (cols["streak"], cy), "--", f.small, GREY)
        # longest run this month
        if r.longest_month:
            v = fmt_distance(r.longest_month.distance(units))
            text(d, (cols["long"], cy), v, f.small,
                 YELLOW if r.longest_month.distance_m >= best_long else WHITE)
        else:
            text(d, (cols["long"], cy), "--", f.small, GREY)
        # fastest pace this week
        if r.fastest_week:
            p = r.fastest_week.pace_s(units)
            text(d, (cols["pace"], cy), fmt_pace(p), f.small,
                 YELLOW if best_pace is not None and p <= best_pace else WHITE)
        else:
            text(d, (cols["pace"], cy), "--", f.small, GREY)
    if pages > 1:
        for p in range(pages):
            d.point((lw - 2 - (pages - 1 - p) * 2, lh - 1), fill=WHITE if p == page else DIM)
    return finish(img, w, h, k)


# --------------------------------------------------------------------------- #
# Message screen
# --------------------------------------------------------------------------- #
def render_message(w: int, h: int, lines: Sequence[str], color: RGB = WHITE,
                   icon: bool = True) -> Image.Image:
    k = upscale_factor(w, h)
    lw, lh = w // k, h // k
    img, d = _canvas(lw, lh)
    f = fonts()
    lines = list(lines)[:3]
    total = len(lines) * 7
    y = (lh - total) // 2
    if icon:
        text(d, (1, 0), "STRAVA", f.tiny, ORANGE)
    for line in lines:
        text(d, (lw // 2, y), line, f.small, color, align="center", max_w=lw - 2)
        y += 7
    return finish(img, w, h, k)
