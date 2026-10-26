"""Strava HTTP layer for the LEDMatrix Strava plugin.

Everything that touches the network or the token file lives here so the
renderer and the data layer stay pure and testable.

Strava specifics that shape this module:

* Access tokens live six hours. Every refresh returns a *new* refresh token
  and invalidates the old one, so the token file is rewritten after every
  refresh.
* New Strava apps are in "Single Player Mode" (one athlete). To let a second
  person in without lifting that cap, each athlete entry in the token file may
  carry its own ``client_id``/``client_secret`` (their own app). Entries
  without them fall back to the plugin-wide credentials.
* The token file is written by the web-UI user (auth script) and later by
  root (the display service refreshing tokens). Writes are atomic and keep the
  original owner/group so both sides can keep writing it.
"""

from __future__ import annotations

import json
import logging
import os
import stat
import tempfile
import time
from pathlib import Path
from typing import Any, Dict, List, Optional
from urllib.parse import urlencode

try:  # requests ships with LEDMatrix core; keep import soft for pure tests
    import requests
except Exception:  # pragma: no cover - exercised only without requests
    requests = None  # type: ignore

STRAVA_AUTHORIZE_URL = "https://www.strava.com/oauth/authorize"
STRAVA_TOKEN_URL = "https://www.strava.com/oauth/token"
STRAVA_API_BASE = "https://www.strava.com/api/v3"
DEFAULT_REDIRECT_URI = "http://localhost/exchange_token"
SCOPE = "read,activity:read_all"

#: Refresh when fewer than this many seconds remain on the access token.
REFRESH_MARGIN_S = 120
#: Largest page Strava allows on /athlete/activities.
PAGE_SIZE = 200
#: Hard stop on paging so a misconfigured period can't burn the daily budget.
MAX_PAGES = 10

TOKEN_FILE = Path(__file__).resolve().parent / "token.json"

log = logging.getLogger("plugin.strava.api")


# --------------------------------------------------------------------------- #
# Errors
# --------------------------------------------------------------------------- #
class StravaError(Exception):
    """Base error for anything Strava-related."""


class StravaAuthError(StravaError):
    """The athlete needs to (re)connect: no token, revoked, or refresh failed."""


class StravaRateLimited(StravaError):
    """Strava answered 429. Back off until the next 15-minute boundary."""


# --------------------------------------------------------------------------- #
# Polyline
# --------------------------------------------------------------------------- #
def decode_polyline(encoded: Optional[str], precision: int = 5) -> List[tuple]:
    """Decode a Google encoded polyline into ``[(lat, lon), ...]``.

    Pure Python; Strava's ``map.summary_polyline`` uses precision 5.
    """
    if not encoded:
        return []
    factor = 10 ** precision
    coords: List[tuple] = []
    index = lat = lon = 0
    length = len(encoded)
    while index < length:
        result = shift = 0
        while True:
            b = ord(encoded[index]) - 63
            index += 1
            result |= (b & 0x1F) << shift
            shift += 5
            if b < 0x20:
                break
        lat += ~(result >> 1) if result & 1 else result >> 1
        result = shift = 0
        while True:
            b = ord(encoded[index]) - 63
            index += 1
            result |= (b & 0x1F) << shift
            shift += 5
            if b < 0x20:
                break
        lon += ~(result >> 1) if result & 1 else result >> 1
        coords.append((lat / factor, lon / factor))
    return coords


# --------------------------------------------------------------------------- #
# Token store
# --------------------------------------------------------------------------- #
class TokenStore:
    """``token.json``: ``{"<athlete_id>": {...token entry...}, ...}``.

    An entry holds ``access_token``, ``refresh_token``, ``expires_at``,
    ``athlete`` (``id``, ``firstname``, ``lastname``) and optionally
    ``client_id`` / ``client_secret`` when the athlete uses their own app.
    """

    def __init__(self, path: Path = TOKEN_FILE) -> None:
        self.path = Path(path)

    def load(self) -> Dict[str, Dict[str, Any]]:
        try:
            with open(self.path, "r", encoding="utf-8") as fh:
                data = json.load(fh)
        except FileNotFoundError:
            return {}
        except (OSError, json.JSONDecodeError) as exc:
            log.warning("Could not read %s: %s", self.path, exc)
            return {}
        if not isinstance(data, dict):
            return {}
        # Tolerate a single-entry legacy layout ({"access_token": ...}).
        if "refresh_token" in data and "athlete" in data:
            aid = str(data["athlete"].get("id", "me"))
            return {aid: data}
        return {str(k): v for k, v in data.items() if isinstance(v, dict)}

    def save(self, data: Dict[str, Dict[str, Any]]) -> None:
        """Atomic, group-writable write that preserves the existing owner."""
        self.path.parent.mkdir(parents=True, exist_ok=True)
        prev_uid = prev_gid = None
        try:
            st = os.stat(self.path)
            prev_uid, prev_gid = st.st_uid, st.st_gid
        except FileNotFoundError:
            pass
        fd, tmp_name = tempfile.mkstemp(prefix=".token-", suffix=".json",
                                        dir=str(self.path.parent))
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                json.dump(data, fh, indent=2, sort_keys=True)
            os.chmod(tmp_name, stat.S_IRUSR | stat.S_IWUSR | stat.S_IRGRP | stat.S_IWGRP)
            if prev_uid is not None and hasattr(os, "geteuid") and os.geteuid() == 0:
                try:
                    os.chown(tmp_name, prev_uid, prev_gid)
                except OSError as exc:  # pragma: no cover
                    log.debug("chown of token file failed: %s", exc)
            os.replace(tmp_name, self.path)
        except Exception:
            try:
                os.unlink(tmp_name)
            except OSError:
                pass
            raise

    def upsert(self, entry: Dict[str, Any]) -> str:
        """Insert/replace an entry keyed by its athlete id; returns the key."""
        athlete = entry.get("athlete") or {}
        aid = str(athlete.get("id") or entry.get("athlete_id") or "me")
        data = self.load()
        merged = dict(data.get(aid, {}))
        merged.update(entry)
        data[aid] = merged
        self.save(data)
        return aid

    def athlete_ids(self) -> List[str]:
        return list(self.load().keys())


# --------------------------------------------------------------------------- #
# OAuth helpers (used by the auth script and the client)
# --------------------------------------------------------------------------- #
def build_authorize_url(client_id: str, redirect_uri: str = DEFAULT_REDIRECT_URI,
                        scope: str = SCOPE, state: str = "") -> str:
    params = {
        "client_id": str(client_id),
        "redirect_uri": redirect_uri,
        "response_type": "code",
        "approval_prompt": "force",
        "scope": scope,
    }
    if state:
        params["state"] = state
    return f"{STRAVA_AUTHORIZE_URL}?{urlencode(params)}"


def _post_token(payload: Dict[str, Any], session=None) -> Dict[str, Any]:
    sess = session or requests
    if sess is None:
        raise StravaError("The 'requests' package is required")
    resp = sess.post(STRAVA_TOKEN_URL, data=payload, timeout=20)
    if resp.status_code == 429:
        raise StravaRateLimited("Strava rate limit hit during token request")
    if resp.status_code >= 400:
        snippet = ""
        try:
            snippet = json.dumps(resp.json())[:300]
        except Exception:
            snippet = (getattr(resp, "text", "") or "")[:300]
        if resp.status_code in (400, 401, 403):
            raise StravaAuthError(f"Strava token request rejected ({resp.status_code}): {snippet}")
        raise StravaError(f"Strava token request failed ({resp.status_code}): {snippet}")
    return resp.json()


def exchange_code(client_id: str, client_secret: str, code: str, session=None) -> Dict[str, Any]:
    """Authorization-code exchange. Returns Strava's token response (has ``athlete``)."""
    return _post_token({
        "client_id": client_id,
        "client_secret": client_secret,
        "code": code,
        "grant_type": "authorization_code",
    }, session)


def refresh_access_token(client_id: str, client_secret: str, refresh_token: str,
                         session=None) -> Dict[str, Any]:
    return _post_token({
        "client_id": client_id,
        "client_secret": client_secret,
        "refresh_token": refresh_token,
        "grant_type": "refresh_token",
    }, session)


# --------------------------------------------------------------------------- #
# Client
# --------------------------------------------------------------------------- #
class StravaClient:
    """Per-athlete token handling plus the one endpoint the plugin needs."""

    def __init__(self, token_store: TokenStore, client_id: str = "", client_secret: str = "",
                 session=None, logger: Optional[logging.Logger] = None) -> None:
        self.store = token_store
        self.client_id = str(client_id or "")
        self.client_secret = str(client_secret or "")
        self.session = session or requests
        self.log = logger or log
        #: epoch seconds until which we refuse to call Strava (after a 429)
        self.rate_limited_until: float = 0.0
        self.last_usage: Optional[str] = None

    # -- credentials --------------------------------------------------------
    def _creds_for(self, entry: Dict[str, Any]) -> tuple:
        cid = str(entry.get("client_id") or self.client_id)
        csec = str(entry.get("client_secret") or self.client_secret)
        if not cid or not csec:
            raise StravaAuthError("Strava client_id/client_secret not configured")
        return cid, csec

    def access_token_for(self, athlete_id: str) -> str:
        data = self.store.load()
        entry = data.get(str(athlete_id))
        if not entry or not entry.get("refresh_token"):
            raise StravaAuthError(f"Athlete {athlete_id} is not connected")
        expires_at = float(entry.get("expires_at") or 0)
        if entry.get("access_token") and expires_at - time.time() > REFRESH_MARGIN_S:
            return entry["access_token"]
        cid, csec = self._creds_for(entry)
        self.log.info("Refreshing Strava token for athlete %s", athlete_id)
        fresh = refresh_access_token(cid, csec, entry["refresh_token"], self.session)
        entry.update({
            "access_token": fresh["access_token"],
            "refresh_token": fresh.get("refresh_token", entry["refresh_token"]),
            "expires_at": fresh.get("expires_at", time.time() + fresh.get("expires_in", 21600)),
        })
        data[str(athlete_id)] = entry
        self.store.save(data)
        return entry["access_token"]

    # -- requests -----------------------------------------------------------
    def _check_rate_limit(self) -> None:
        if time.time() < self.rate_limited_until:
            raise StravaRateLimited("Backing off after a Strava 429")

    def _note_rate_limit_headers(self, resp) -> None:
        usage = resp.headers.get("X-ReadRateLimit-Usage") or resp.headers.get("X-RateLimit-Usage")
        limit = resp.headers.get("X-ReadRateLimit-Limit") or resp.headers.get("X-RateLimit-Limit")
        if usage:
            self.last_usage = f"{usage} of {limit}"
            self.log.debug("Strava read usage %s", self.last_usage)

    def _get(self, path: str, athlete_id: str, params: Dict[str, Any]) -> Any:
        if self.session is None:
            raise StravaError("The 'requests' package is required")
        self._check_rate_limit()
        token = self.access_token_for(athlete_id)
        resp = self.session.get(f"{STRAVA_API_BASE}{path}", params=params,
                                headers={"Authorization": f"Bearer {token}"}, timeout=25)
        self._note_rate_limit_headers(resp)
        if resp.status_code == 429:
            now = time.time()
            # 15-minute windows reset on the quarter hour
            self.rate_limited_until = (int(now // 900) + 1) * 900 + 5
            raise StravaRateLimited("Strava read rate limit exceeded")
        if resp.status_code == 401:
            raise StravaAuthError(f"Strava rejected the token for athlete {athlete_id}")
        if resp.status_code >= 400:
            raise StravaError(f"Strava GET {path} failed ({resp.status_code})")
        return resp.json()

    def fetch_activities(self, athlete_id: str, after_epoch: int, before_epoch: int) -> List[Dict[str, Any]]:
        """All activities for one athlete in ``[after, before]`` (summary objects)."""
        out: List[Dict[str, Any]] = []
        for page in range(1, MAX_PAGES + 1):
            batch = self._get("/athlete/activities", athlete_id, {
                "after": int(after_epoch),
                "before": int(before_epoch),
                "per_page": PAGE_SIZE,
                "page": page,
            })
            if not isinstance(batch, list):
                break
            out.extend(batch)
            if len(batch) < PAGE_SIZE:
                break
        return out
