"""OVA girls team rankings (https://www.ontariovolleyball.org/girls-team-rankings).

Page layout (checked Sept 2026, SportsEngine site):
  one accordion per age group –
    <div class="custom-accordion">
      <h3><span>15U Girls - Post Ontario Championships Rankings 2025-26</span></h3>
      <table class="dataTable"> <thead> Rank | Team Name | Provincial Cup | … | Top 2 Average | … </thead> … </table>
  Column sets differ by age group and by point in the season (after each OVA
  event OVA replaces the table), so columns are read by header name.
  The page also links the new season's "Division Split" PDFs.

The site sometimes blocks automated requests (HTTP 403). This module fails
soft; build.py then shows the last saved ranking with its date.
"""
from __future__ import annotations

import logging
import re
import time

import httpx
from bs4 import BeautifulSoup

log = logging.getLogger("rankings")

URL = "https://www.ontariovolleyball.org/girls-team-rankings"
HEADERS = {
    # A normal browser UA; the site rejects obvious bots. Requests stay a few per day.
    "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
                  "(KHTML, like Gecko) Chrome/128.0 Safari/537.36",
    "Accept": "text/html,application/xhtml+xml",
    "Accept-Language": "en-CA,en;q=0.9",
}
HEADING = re.compile(r"((?:4v4|6v6|TLS|1\dU)\s+Girls)\s*[-–]\s*(.*Rankings.*)", re.I)


def fetch_page(retries: int = 3) -> str | None:
    for attempt in range(1, retries + 1):
        try:
            r = httpx.get(URL, headers=HEADERS, timeout=30, follow_redirects=True)
            r.raise_for_status()
            return r.text
        except httpx.HTTPError as e:
            log.warning("rankings attempt %d failed: %s", attempt, e)
            time.sleep(3 * attempt)
    return None


def _norm(s: str) -> str:
    return re.sub(r"[^a-z0-9]", "", s.lower())


def parse(page: str, aliases: list[str]) -> dict:
    """Return {"entries": [...], "splits": [...]} for our team."""
    soup = BeautifulSoup(page, "html.parser")
    wanted = [_norm(a) for a in aliases]
    entries = []
    for table in soup.select("table.dataTable"):
        head_txt = table.find_previous(string=HEADING)
        if not head_txt:
            continue
        m = HEADING.search(head_txt.strip())
        age, title = m.group(1), m.group(2).strip()
        headers = [th.get_text(" ", strip=True) for th in table.select("thead th")]
        rows = table.select("tbody tr")
        ranked = 0
        found = None
        for tr in rows:
            cells = [td.get_text(" ", strip=True) for td in tr.find_all("td")]
            if not cells:
                continue
            row = dict(zip(headers, cells))
            rank_val = row.get("Rank") or row.get("Final Rank") or ""
            if rank_val.strip():
                ranked += 1
            name = row.get("Team Name", "")
            if any(w and w == _norm(name) for w in wanted):
                found = row
        if not found:
            continue
        rank = (found.get("Rank") or found.get("Final Rank") or "").strip()
        detail = {k: v for k, v in found.items() if k not in ("Rank", "Team Name", "Final Rank") and v not in ("", None)}
        entries.append({
            "age_group": age,
            "title": title,
            "rank": rank or None,              # empty = listed but not officially ranked (e.g. underage)
            "of": ranked,
            "detail": detail,                   # e.g. {"Pre OC Rank": "39"} or cup points / Top 2 Average
        })
    splits = []
    for a in soup.find_all("a", href=True):
        text = a.get_text(" ", strip=True)
        if re.search(r"Division Split", text, re.I):
            splits.append({"label": text, "url": a["href"]})
    return {"entries": entries, "splits": splits}


def fetch(aliases: list[str]) -> tuple[dict | None, str | None]:
    page = fetch_page()
    if not page:
        return None, "Couldn't reach the OVA rankings page – showing last saved ranking"
    try:
        return parse(page, aliases), None
    except Exception as e:  # layout change
        log.warning("rankings parse failed: %s", e)
        return None, "OVA rankings page changed layout – showing last saved ranking"
