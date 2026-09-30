"""Pull a team's schedule from AES (Advanced Event Systems), which OVA uses.

AES has no official public API. Its results site (results.advancedeventsystems.com)
is a web app that loads JSON from its own endpoints; this module calls those.
They can change without notice, so every function here fails soft: on any
problem it logs a warning and returns None, and the site keeps the last good data.

To verify/repair endpoints: open the team's schedule page in Chrome,
DevTools → Network → Fetch/XHR, reload, and look at the request URLs.
"""
from __future__ import annotations

import logging
import re
import time
from datetime import datetime

import httpx

log = logging.getLogger("aes")

BASE = "https://results.advancedeventsystems.com"
HEADERS = {
    "User-Agent": "RevolutionForceScheduleBot/1.0 (volunteer parent tool; low-frequency)",
    "Accept": "application/json",
}
URL_RE = re.compile(r"/event/(?P<key>[^/]+)/divisions/(?P<div>\d+)/teams/(?P<team>\d+)")


def parse_team_url(url: str) -> dict | None:
    m = URL_RE.search(url)
    return m.groupdict() if m else None


def _get_json(client: httpx.Client, path: str, retries: int = 3):
    for attempt in range(1, retries + 1):
        try:
            r = client.get(BASE + path, headers=HEADERS, timeout=20)
            if r.status_code == 404:
                return None
            r.raise_for_status()
            return r.json()
        except (httpx.HTTPError, ValueError) as e:
            log.warning("AES %s attempt %d failed: %s", path, attempt, e)
            time.sleep(2 ** attempt)
    return None


def _pick(d: dict, *keys, default=None):
    """Return the first present key (AES field names vary between versions)."""
    for k in keys:
        v = d.get(k) if isinstance(d, dict) else None
        if v not in (None, ""):
            return v
    return default


def _text(v):
    if isinstance(v, dict):
        return _pick(v, "Name", "Text", "TeamName", "Description")
    return v


def _normalize_match(m: dict, team_names: list[str], role: str = "play") -> dict | None:
    start = _pick(m, "ScheduledStartDateTime", "StartTime", "MatchDateTime", "Date")
    if not start:
        return None
    try:
        dt = datetime.fromisoformat(str(start).replace("Z", ""))
    except ValueError:
        return None
    t1 = _text(_pick(m, "FirstTeamText", "FirstTeam", "HomeTeam")) or ""
    t2 = _text(_pick(m, "SecondTeamText", "SecondTeam", "AwayTeam")) or ""
    lowered = [n.lower() for n in team_names]
    opponent = t2 if any(n in t1.lower() for n in lowered) else t1
    return {
        "date": dt.date().isoformat(),
        "time": dt.strftime("%H:%M"),
        "court": _text(_pick(m, "Court", "CourtName")) or "",
        "opponent": opponent if role == "play" else "",
        "round": _text(_pick(m, "PlayName", "Play", "RoundName")) or ("Work team" if role == "work" else ""),
        "role": role,
        "result": _pick(m, "ScoreText", "Result", default=""),
    }


def fetch_team(url: str, team_names: list[str]) -> dict | None:
    """Return {'matches': [...], 'placement': str|None, 'aes_url': url} or None."""
    ids = parse_team_url(url)
    if not ids:
        log.warning("Not a recognizable AES team URL: %s", url)
        return None
    key, div, team = ids["key"], ids["div"], ids["team"]
    base = f"/api/event/{key}/division/{div}/team/{team}"

    with httpx.Client(follow_redirects=True) as client:
        current = _get_json(client, f"{base}/schedule/current") or []
        past = _get_json(client, f"{base}/schedule/past") or []
        work = _get_json(client, f"{base}/schedule/work") or []
        standing = _get_json(client, f"{base}")  # team summary incl. finish

    if not (current or past or work):
        log.warning("AES returned no schedule for %s (not posted yet, or endpoint changed)", url)
        return None

    def flatten(block):
        # Responses are either a list of matches or a list of "plays" holding matches.
        out = []
        for item in block if isinstance(block, list) else [block]:
            if isinstance(item, dict) and isinstance(item.get("Matches"), list):
                for mm in item["Matches"]:
                    mm.setdefault("PlayName", _text(item.get("Play")) or item.get("PlayName"))
                    out.append(mm)
            elif isinstance(item, dict):
                out.append(item)
        return out

    matches = []
    for m in flatten(past) + flatten(current):
        n = _normalize_match(m, team_names, "play")
        if n:
            matches.append(n)
    for m in flatten(work):
        n = _normalize_match(m, team_names, "work")
        if n:
            matches.append(n)
    matches.sort(key=lambda x: (x["date"], x["time"]))

    placement = None
    if isinstance(standing, dict):
        fin = _pick(standing, "FinishRankText", "FinishRank", "Finish", "OverallRankText")
        if fin:
            placement = str(fin)

    return {"matches": matches, "placement": placement, "aes_url": url}
