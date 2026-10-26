#!/usr/bin/env python3
"""Connect a Strava account to the LEDMatrix Strava plugin.

Two ways to run it:

1. **Web UI button** ("Connect Strava" on the plugin's config page).
   LEDMatrix calls ``get_auth_url()`` first and shows the link. After the
   athlete approves, the browser lands on ``http://localhost/exchange_token?...``
   (a page that does not exist - that is expected). Paste that full URL into
   the step-2 box; LEDMatrix runs this file with the URL on stdin and it saves
   the tokens into ``token.json`` next to this script.

2. **Command line** (e.g. for a roommate using their *own* Strava app, which
   sidesteps Strava's one-athlete "Single Player Mode" on your app)::

       python3 authenticate_strava.py --client-id 12345 --client-secret abc...

   It prints the link, waits for the pasted redirect URL, and stores the
   tokens together with those app credentials so refreshes keep working.

Other handy flags: ``--list`` shows connected athletes, ``--remove ID`` drops one.

Importing this module must stay side-effect free: the web process imports it
to call ``get_auth_url()``.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path
from urllib.parse import parse_qs, urlparse

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

from strava_api import (  # noqa: E402
    DEFAULT_REDIRECT_URI, TokenStore, build_authorize_url, exchange_code,
)

PLUGIN_ID = "strava"


def _ledmatrix_root() -> Path:
    root = os.environ.get("LEDMATRIX_ROOT")
    if root:
        return Path(root)
    # plugin-repos/strava/../.. = LEDMatrix root (same layout as official plugins)
    return HERE.parent.parent


def _load_json(path: Path) -> dict:
    try:
        with open(path, "r", encoding="utf-8") as fh:
            data = json.load(fh)
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def load_credentials() -> tuple:
    """(client_id, client_secret) from config_secrets.json, then config.json."""
    root = _ledmatrix_root()
    cid = csec = ""
    for name in ("config_secrets.json", "config.json"):
        section = _load_json(root / "config" / name).get(PLUGIN_ID, {})
        if not isinstance(section, dict):
            continue
        cid = cid or str(section.get("client_id") or "").strip()
        csec = csec or str(section.get("client_secret") or "").strip()
    # env overrides are handy for the CLI path
    cid = os.environ.get("STRAVA_CLIENT_ID", cid)
    csec = os.environ.get("STRAVA_CLIENT_SECRET", csec)
    return cid, csec


def get_auth_url() -> str:
    """Step 1 for the web UI. Raises if the client ID is not configured yet."""
    cid, _ = load_credentials()
    if not cid:
        raise RuntimeError("Set the Strava Client ID and Client Secret in the plugin settings and save first.")
    return build_authorize_url(cid, DEFAULT_REDIRECT_URI)


def extract_code(text: str) -> str:
    """Accept a full redirect URL, a query string, or a bare code."""
    text = (text or "").strip().strip('"').strip("'")
    if not text:
        return ""
    if text.startswith("{"):           # web UI may wrap it as JSON
        try:
            text = json.loads(text).get("redirect_url", text)
        except Exception:
            pass
    if "/" in text or "?" in text or "=" in text:
        query = urlparse(text).query if "://" in text else text.lstrip("?")
        qs = parse_qs(query)
        if "error" in qs:
            raise RuntimeError(f"Strava returned an error: {qs['error'][0]}")
        if qs.get("code"):
            return qs["code"][0]
        raise RuntimeError("Could not find 'code=' in the pasted URL.")
    return text


def complete(code: str, client_id: str, client_secret: str, own_app: bool = False) -> dict:
    token = exchange_code(client_id, client_secret, code)
    athlete = token.get("athlete") or {}
    entry = {
        "access_token": token.get("access_token"),
        "refresh_token": token.get("refresh_token"),
        "expires_at": token.get("expires_at"),
        "scope": token.get("scope"),
        "athlete": {
            "id": athlete.get("id"),
            "firstname": athlete.get("firstname"),
            "lastname": athlete.get("lastname"),
        },
    }
    if own_app:
        entry["client_id"] = client_id
        entry["client_secret"] = client_secret
    TokenStore().upsert(entry)
    return entry


def _print_connected(entry: dict) -> None:
    a = entry.get("athlete") or {}
    print(json.dumps({
        "status": "success",
        "athlete_id": a.get("id"),
        "name": f"{a.get('firstname', '')} {a.get('lastname', '')}".strip(),
        "message": (f"Connected {a.get('firstname', 'athlete')} (Strava ID {a.get('id')}). "
                    f"Add this ID to the Athletes table in the plugin settings, or leave the row's "
                    f"ID blank if the Name matches their Strava first name."),
    }, indent=2))


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--client-id", help="Use this Strava app instead of the plugin-wide one")
    ap.add_argument("--client-secret")
    ap.add_argument("--list", action="store_true", help="List connected athletes and exit")
    ap.add_argument("--remove", metavar="ATHLETE_ID", help="Disconnect an athlete and exit")
    args = ap.parse_args(argv)

    store = TokenStore()
    if args.list:
        data = store.load()
        if not data:
            print("No athletes connected.")
        for aid, e in data.items():
            a = e.get("athlete") or {}
            own = " (own app)" if e.get("client_id") else ""
            print(f"{aid}: {a.get('firstname', '')} {a.get('lastname', '')}{own}")
        return 0
    if args.remove:
        data = store.load()
        if data.pop(str(args.remove), None) is None:
            print(f"No athlete {args.remove} in {store.path}")
            return 1
        store.save(data)
        print(f"Removed athlete {args.remove}")
        return 0

    own_app = bool(args.client_id)
    if own_app:
        cid, csec = args.client_id, args.client_secret or os.environ.get("STRAVA_CLIENT_SECRET", "")
        if not csec:
            print("--client-secret is required with --client-id", file=sys.stderr)
            return 2
    else:
        cid, csec = load_credentials()
        if not cid or not csec:
            print(json.dumps({"status": "error",
                              "message": "Strava client_id/client_secret not set. Save them in the plugin settings first."}))
            return 1

    interactive = sys.stdin.isatty()
    if interactive:
        print("\n1. Open this link (any device), log in as the athlete, click Authorize:\n")
        print("   " + build_authorize_url(cid, DEFAULT_REDIRECT_URI))
        print("\n2. The browser will land on a page that fails to load at http://localhost/exchange_token?...")
        print("   Copy that FULL URL from the address bar and paste it here.\n")
        pasted = input("Redirect URL: ")
    else:
        pasted = sys.stdin.read()

    try:
        code = extract_code(pasted)
        if not code:
            raise RuntimeError("No authorization code received.")
        entry = complete(code, cid, csec, own_app=own_app)
    except Exception as exc:  # noqa: BLE001 - surfaced to the UI
        print(json.dumps({"status": "error", "message": str(exc)}))
        return 1
    _print_connected(entry)
    return 0


if __name__ == "__main__":
    sys.exit(main())
