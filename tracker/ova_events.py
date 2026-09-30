"""The team's regular-season tournaments from the OVA events calendar (ontariovolleyball.org).

Why this source: OVA posts one calendar event per competition, e.g.
  "TLS Girls Division 1 Provincial Cup (Sat OR Sun)"  →  /event/show/588428285
and, about a month before, fills that page with one block per tier:
  <h1>Trillium I - Saturday November 15th</h1>
  <h3>Location:</h3><p>Huron Heights (40 Huron Heights DR, Newmarket ON L3Y 3J9)</p>
  <p>Participating Teams: (12)</p><table> 1 | Caspian Vortex … 10 | Markham Revolution Force … </table>
  <a href="https://www.timu.ca/scoreboards/results.php?tid=3319">Schedule/Results</a>
So we get only the tier we play in, its day and venue, and the exact timu page for match times.

Calendar months load through the site's own XHR endpoint:
  /event/change_month_list/<calendar_id>?month=M&year=YYYY   (needs X-Requested-With)
Everything fails soft; build.py falls back to the last saved data.
"""
from __future__ import annotations

import html as htmllib
import logging
import re
import time
from datetime import date, datetime

import httpx
from bs4 import BeautifulSoup

log = logging.getLogger("ova")

BASE = "https://www.ontariovolleyball.org"
HEADERS = {
    "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
                  "(KHTML, like Gecko) Chrome/128.0 Safari/537.36",
    "Accept-Language": "en-CA,en;q=0.9",
}
REGULAR_CUPS = ("Provincial Cup", "Challenge Cup", "McGregor Cup", "Bugarski Cup", "Furlani Cup")


def _get(client: httpx.Client, url: str, xhr: bool = False) -> str | None:
    headers = dict(HEADERS)
    if xhr:
        headers.update({"X-Requested-With": "XMLHttpRequest", "Accept": "text/javascript, application/javascript"})
    for attempt in range(1, 4):
        try:
            r = client.get(url, headers=headers, timeout=30)
            r.raise_for_status()
            return r.text
        except httpx.HTTPError as e:
            log.warning("GET %s attempt %d failed: %s", url, attempt, e)
            time.sleep(2 * attempt)
    return None


# ------------------------------------------------------------------ calendar
def parse_month(js: str) -> list[dict]:
    """The XHR reply is jQuery code: $j('#month_list').html('<escaped html>') – unescape and parse."""
    m = re.search(r"\$j\('#month_list'\)\.html\('(.*?)'\);\s*\$j\('#month_navigation'\)", js, re.S)
    body = m.group(1) if m else js
    body = (body.replace("\\n", " ").replace("\\/", "/").replace('\\"', '"').replace("\\'", "'"))
    soup = BeautifulSoup(body, "html.parser")
    out = []
    for ev in soup.select("div.vevent"):
        a = ev.select_one("h5.summary a[href]")
        when = ev.select_one("abbr.dtstart[title]")
        idm = re.search(r"/event/show/(\d+)", a["href"]) if a else None
        if not (a and when and idm):
            continue
        out.append({"id": idm.group(1), "title": a.get_text(" ", strip=True), "date": when["title"][:10]})
    return out


def season_months(season_start: str) -> list[tuple[int, int]]:
    y = int(season_start[:4])
    return [(m, y) for m in range(9, 13)] + [(m, y + 1) for m in range(1, 6)]


def list_events(client: httpx.Client, calendar_id: str, season_start: str, patterns: list[str]) -> list[dict]:
    rx = [re.compile(p, re.I) for p in patterns]
    found, seen = [], set()
    for m, y in season_months(season_start):
        js = _get(client, f"{BASE}/event/change_month_list/{calendar_id}?month={m}&year={y}", xhr=True)
        time.sleep(0.5)
        if not js:
            continue
        for e in parse_month(js):
            if e["id"] in seen:
                continue
            if any(r.search(e["title"]) for r in rx) and any(c.lower() in e["title"].lower() for c in REGULAR_CUPS):
                seen.add(e["id"])
                found.append(e)
    return found


# ------------------------------------------------------------------ event page
_DAY = re.compile(r"(Mon|Tues|Wednes|Thurs|Fri|Satur|Sun)day\s+([A-Z][a-z]+)\s+(\d{1,2})", re.I)


def _norm(s: str) -> str:
    return re.sub(r"[^a-z0-9]", "", s.lower())


def parse_event(page: str, aliases: list[str], event_date: str) -> dict:
    """Return {"splits_posted": bool, "tier": {...} | None} for our team."""
    soup = BeautifulSoup(page, "html.parser")
    wanted = [_norm(a) for a in aliases]
    splits_posted = False
    for h1 in soup.find_all("h1"):
        heading = htmllib.unescape(h1.get_text(" ", strip=True)).replace("\xa0", " ")
        if heading.lower() == "event" or not heading:
            continue
        box = h1.find_parent("div", class_="layoutContainer")
        if not box:
            continue
        teams = [c.get_text(" ", strip=True) for c in box.select("table td")] + \
                [li.get_text(" ", strip=True) for li in box.select("ol li")]
        names = [t for t in teams if t and not t.isdigit()]
        if any(n.lower() != "team name" for n in names):
            splits_posted = True
        if not any(_norm(n) in wanted for n in names):
            continue
        tier = re.split(r"\s+-\s+", heading)[0].strip()
        day = None
        dm = _DAY.search(heading)
        if dm:
            base = date.fromisoformat(event_date)
            try:
                d = datetime.strptime(f"{dm.group(2)} {dm.group(3)} {base.year}", "%B %d %Y").date()
                if abs((d - base).days) > 180:  # event spans new year
                    d = d.replace(year=base.year + 1)
                day = d.isoformat()
            except ValueError:
                pass
        loc_txt = ""
        for h3 in box.find_all("h3"):
            if "location" in h3.get_text().lower():
                p = h3.find_next("p")
                loc_txt = p.get_text(" ", strip=True) if p else ""
                break
        venue, address = loc_txt, ""
        lm = re.match(r"(.*?)\s*\((.*)\)\s*$", loc_txt)
        if lm:
            venue, address = lm.group(1).strip(), lm.group(2).strip()
        tid = None
        for a in box.find_all("a", href=True):
            tm = re.search(r"timu\.ca/.*tid=(\d+)", a["href"])
            if tm:
                tid = tm.group(1)
        return {"splits_posted": True, "tier": {"tier": tier, "day": day, "venue": venue,
                                                 "address": address, "timu_tid": tid}}
    return {"splits_posted": splits_posted, "tier": None}


def clean_title(title: str) -> str:
    return re.sub(r"\s*\((Sat|Sun)[^)]*\)\s*$", "", title).strip()
