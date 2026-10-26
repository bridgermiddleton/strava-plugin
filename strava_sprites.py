"""Pixel-art runner characters for the Strava plugin.

Sprites are hand-drawn as rows of palette letters, so an outfit is just a
color map. Every athlete shares the same body (light skin, dirty-blonde hair)
and differs by shirt, shorts and an optional accessory.

Palette letters::

    .  transparent      S  skin        H  hair       T  shirt
    P  shorts           K  shoes       A  accessory  W  white (eye/detail)
"""

from __future__ import annotations

from functools import lru_cache
from typing import Dict, List, Sequence, Tuple

from PIL import Image

RGB = Tuple[int, int, int]

SPRITE_W, SPRITE_H = 8, 12

# Four-frame run cycle, facing right. 8 x 12.
RUN_FRAMES: List[List[str]] = [
    [   # 0: full stride, front leg forward
        "..HHH...",
        ".HHSS...",
        ".HSSW...",
        "..SS....",
        "..TTT...",
        ".STTTS..",
        "..TTT...",
        "..PPP...",
        ".PP.PP..",
        ".P...P..",
        "K.....K.",
        "........",
    ],
    [   # 1: passing, body up, back leg tucked
        "........",
        "..HHH...",
        ".HHSS...",
        ".HSSW...",
        "..SS....",
        "..TTTS..",
        ".STTT...",
        "..PPP...",
        "..PPP...",
        "...P.P..",
        "...K.K..",
        "........",
    ],
    [   # 2: full stride, other leg forward
        "..HHH...",
        ".HHSS...",
        ".HSSW...",
        "..SS....",
        "..TTT...",
        "..TTTS..",
        ".STTT...",
        "..PPP...",
        ".PP.PP..",
        "P....P..",
        "K.....K.",
        "........",
    ],
    [   # 3: passing, body up, other leg tucked
        "........",
        "..HHH...",
        ".HHSS...",
        ".HSSW...",
        "..SS....",
        ".STTT...",
        "..TTTS..",
        "..PPP...",
        "..PPP...",
        "..P.P...",
        "..K.K...",
        "........",
    ],
]

# Standing pose (used when a run has no distance / the race hasn't started).
STAND_FRAME: List[str] = [
    "..HHH...",
    ".HHSS...",
    ".HSSW...",
    "..SS....",
    "..TTT...",
    ".STTTS..",
    "..TTT...",
    "..PPP...",
    "..P.P...",
    "..P.P...",
    "..K.K...",
    "........",
]

# Bust icon for leaderboard / records rows. 5 x 6.
BUST: List[str] = [
    ".HHH.",
    "HHHH.",
    ".SSW.",
    ".SS..",
    ".TTT.",
    "TTTTT",
]

CROWN: List[str] = [
    "A.A.A",
    "AAAAA",
    "AAAAA",
]

FLAME: List[str] = [
    "..A..",
    ".AA..",
    ".AAW.",
    "AAWWA",
    ".AWA.",
]

TROPHY: List[str] = [
    "AAAAA",
    "AAAAA",
    ".AAA.",
    "..A..",
    ".AAA.",
]

FLAG: List[str] = [   # checkered finish flag 5 x 7, pole = K
    "KAWAW",
    "KWAWA",
    "KAWAW",
    "KWAWA",
    "K....",
    "K....",
    "K....",
]

TREADMILL: List[str] = [   # for runs without GPS, 9 x 5
    ".........",
    "........K",
    "........K",
    "KKKKKKKKK",
    ".K.....K.",
]

WHITE: RGB = (255, 255, 255)
YELLOW: RGB = (255, 215, 0)
ORANGE: RGB = (255, 120, 20)
GREY: RGB = (120, 120, 130)


def _apply_accessory(rows: List[str], accessory: str) -> List[str]:
    """Overlay an accessory on a 8x12 runner frame (string grid)."""
    g = [list(r) for r in rows]
    # find the first hair row (top of head)
    top = next((i for i, r in enumerate(rows) if "H" in r), None)
    if top is None:
        return rows
    if accessory == "headband":
        y = top + 1
        for x in range(SPRITE_W):
            if g[y][x] in "HS":
                g[y][x] = "A"
    elif accessory == "cap":
        for x in range(SPRITE_W):
            if g[top][x] == "H":
                g[top][x] = "A"
        # brim in front of the face
        y = top + 1
        last = max((x for x in range(SPRITE_W) if g[y][x] in "HSW"), default=None)
        if last is not None and last + 1 < SPRITE_W:
            g[top][last] = "A"
            g[y][last + 1] = "A"
    elif accessory == "sunglasses":
        y = top + 2
        xs = [x for x in range(SPRITE_W) if g[y][x] in "SW"]
        for x in xs[-2:]:
            g[y][x] = "A"
    return ["".join(r) for r in g]


def palette(shirt: RGB, shorts: RGB, skin: RGB, hair: RGB, shoes: RGB,
            accessory_color: RGB = WHITE) -> Dict[str, RGB]:
    return {"S": skin, "H": hair, "T": shirt, "P": shorts, "K": shoes,
            "A": accessory_color, "W": WHITE}


def grid_to_image(rows: Sequence[str], colors: Dict[str, RGB], scale: int = 1) -> Image.Image:
    """Render a letter grid to an RGBA image (``.`` is transparent)."""
    h = len(rows)
    w = max(len(r) for r in rows)
    img = Image.new("RGBA", (w, h), (0, 0, 0, 0))
    px = img.load()
    for y, row in enumerate(rows):
        for x, ch in enumerate(row):
            if ch == ".":
                continue
            c = colors.get(ch)
            if c is None:
                continue
            px[x, y] = (c[0], c[1], c[2], 255)
    if scale > 1:
        img = img.resize((w * scale, h * scale), Image.NEAREST)
    return img


def accessory_color(accessory: str, shirt: RGB) -> RGB:
    if accessory == "sunglasses":
        return (20, 20, 30)
    if accessory == "cap":
        return shirt
    if accessory == "headband":
        return WHITE
    return WHITE


@lru_cache(maxsize=256)
def _runner_frames_cached(shirt: RGB, shorts: RGB, skin: RGB, hair: RGB, shoes: RGB,
                          accessory: str, scale: int) -> Tuple[Image.Image, ...]:
    colors = palette(shirt, shorts, skin, hair, shoes, accessory_color(accessory, shirt))
    frames = [grid_to_image(_apply_accessory(f, accessory), colors, scale) for f in RUN_FRAMES]
    return tuple(frames)


def runner_frames(athlete, scale: int = 1) -> Tuple[Image.Image, ...]:
    """Four RGBA frames (8x12 * scale) for an ``Athlete``-like object."""
    return _runner_frames_cached(tuple(athlete.shirt), tuple(athlete.shorts), tuple(athlete.skin),
                                 tuple(athlete.hair), tuple(athlete.shoes), athlete.accessory, scale)


@lru_cache(maxsize=64)
def _stand_cached(shirt, shorts, skin, hair, shoes, accessory, scale) -> Image.Image:
    colors = palette(shirt, shorts, skin, hair, shoes, accessory_color(accessory, shirt))
    return grid_to_image(_apply_accessory(STAND_FRAME, accessory), colors, scale)


def standing(athlete, scale: int = 1) -> Image.Image:
    return _stand_cached(tuple(athlete.shirt), tuple(athlete.shorts), tuple(athlete.skin),
                         tuple(athlete.hair), tuple(athlete.shoes), athlete.accessory, scale)


@lru_cache(maxsize=64)
def _bust_cached(shirt, skin, hair, scale) -> Image.Image:
    colors = palette(shirt, shirt, skin, hair, WHITE)
    return grid_to_image(BUST, colors, scale)


def bust(athlete, scale: int = 1) -> Image.Image:
    """5x6 head-and-shoulders icon."""
    return _bust_cached(tuple(athlete.shirt), tuple(athlete.skin), tuple(athlete.hair), scale)


def crown(scale: int = 1) -> Image.Image:
    return grid_to_image(CROWN, {"A": YELLOW}, scale)


def flame(scale: int = 1) -> Image.Image:
    return grid_to_image(FLAME, {"A": ORANGE, "W": YELLOW}, scale)


def trophy(scale: int = 1) -> Image.Image:
    return grid_to_image(TROPHY, {"A": YELLOW}, scale)


def flag(scale: int = 1) -> Image.Image:
    return grid_to_image(FLAG, {"A": (235, 235, 235), "W": (40, 40, 40), "K": GREY}, scale)


def treadmill(scale: int = 1) -> Image.Image:
    return grid_to_image(TREADMILL, {"K": GREY}, scale)


def frame_index(t: float, fps: float = 8.0, n: int = 4) -> int:
    """Which run-cycle frame to show at time ``t`` seconds."""
    return int(t * fps) % n
