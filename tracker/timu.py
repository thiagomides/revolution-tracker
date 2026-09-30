"""Scrape OVA tournaments from timu.ca (the OVA Tourney app: https://timu.ca/ova/index.php).

How the site is laid out (checked Sept 2026):
  • ova/index.php                    – <select id="tournament"> with one <optgroup label="——— Saturday October 03, 2026 ———">
                                        per date and one <option value="TID">15U Girls Bugarski Cup Select B</option> per division/tier.
  • scoreboards/schedule.php?tid=TID – hidden <input> fields (division, gender, tournament, tier, venue_name, address, tdate),
                                        a header with "(Start time: 9:00am)", pool seed lists ("1. Team Name ..."),
                                        and a time × court grid ("9:00am | 1 vs 7 | 2 vs 8"; "**" = follows previous match).
  • scoreboards/results.php?tid=TID  – one card per match: round label ("Gold Medal (W7 vs W8)"), court, teams, set scores.

Everything here fails soft: problems are logged and returned as warnings, never raised,
so a site change can't wipe out the published schedule.
"""
from __future__ import annotations

import html as htmllib
import logging
import re
import time
from datetime import datetime

import httpx
from bs4 import BeautifulSoup

log = logging.getLogger("timu")

BASE = "https://timu.ca"
INDEX = f"{BASE}/ova/index.php"
HEADERS = {"User-Agent": "RevolutionForceScheduleBot/1.0 (volunteer parent tool; runs a few times a day)"}

DIVISION_PREFIX = {"15UG": "15U Girls", "TLSG": "TLS Girls", "14UG": "14U Girls", "16UG": "16U Girls"}


# ------------------------------------------------------------------ http
def _get(client: httpx.Client, url: str, retries: int = 3) -> str | None:
    for attempt in range(1, retries + 1):
        try:
            r = client.get(url, headers=HEADERS, timeout=25)
            r.raise_for_status()
            return r.text
        except httpx.HTTPError as e:
            log.warning("GET %s attempt %d failed: %s", url, attempt, e)
            time.sleep(2 ** attempt)
    return None


# ------------------------------------------------------------------ index
def list_tournaments(client: httpx.Client, divisions: list[str]) -> list[dict]:
    """Every (date, tid, title) on the index page for the chosen divisions, e.g. ["15UG", "TLSG"]."""
    page = _get(client, INDEX)
    if not page:
        return []
    return parse_index(page, divisions)


_TOKEN = re.compile(r"<optgroup\s+label=['\"]([^'\"]*)['\"]|<option[^>]*value=['\"](\d+)['\"][^>]*>([^<]*)", re.I)


def parse_index(page: str, divisions: list[str]) -> list[dict]:
    """timu leaves every <optgroup> unclosed, so HTML parsers nest them all inside the first
    date. Read the tags in document order instead: each option belongs to the latest date label."""
    start = max(page.find('id="tournament"'), page.find("id='tournament'"))
    if start < 0:
        log.warning("timu index: tournament list not found (page layout changed?)")
        return []
    end = page.find("</select>", start)
    chunk = page[start:end if end > 0 else None]
    prefixes = tuple(DIVISION_PREFIX.get(d, d) for d in divisions)
    out, current = [], None
    for label, tid, title in _TOKEN.findall(chunk):
        if label:
            text = htmllib.unescape(label).strip("\u2014 \u00a0")
            try:
                current = datetime.strptime(text, "%A %B %d, %Y").date()
            except ValueError:
                current = None
            continue
        title = htmllib.unescape(title).strip()
        if current and tid and title.startswith(prefixes):
            out.append({"tid": tid, "title": title, "date": current.isoformat()})
    return out


# ------------------------------------------------------------------ schedule page
_VS = re.compile(r"(\d+)\s*vs\s*(\d+)(?:\s*\((\d+)\))?")
_START = re.compile(r"Start time:\s*([0-9]{1,2}:[0-9]{2}\s*[ap]m)", re.I)


def _to24(t: str) -> str:
    return datetime.strptime(t.replace(" ", "").lower(), "%I:%M%p").strftime("%H:%M")


def parse_schedule(page: str) -> dict:
    soup = BeautifulSoup(page, "html.parser")
    val = lambda name: (soup.find("input", attrs={"name": name}) or {}).get("value", "") if soup.find("input", attrs={"name": name}) else ""
    info = {
        "division": val("division"), "gender": val("gender"), "tournament": val("tournament"),
        "tier": val("tier"), "venue": val("venue_name"), "address": val("address"), "date": val("tdate"),
    }
    text = soup.get_text(" ", strip=True)
    m = _START.search(text)
    info["start_time"] = _to24(m.group(1)) if m else None

    # Seeds: "1. Durham Rebels Rogue 2. FCVC Aspen ..." inside the pools table
    seeds: dict[str, str] = {}
    pools = soup.find(attrs={"mame": "POOLS"}) or soup.find(attrs={"name": "POOLS"})
    seed_cells = soup.select("div.editSeed") or (pools.select("div.divTableCell") if pools else [])
    for cell in seed_cells:
        m2 = re.match(r"\s*(\d+)\.\s*(.+)", cell.get_text(" ", strip=True).replace("\xa0", " "))
        if m2:
            seeds[m2.group(1)] = m2.group(2).strip()
    info["seeds"] = seeds

    # Pool-play grid: first divTable after the pools table whose header row says "Court"
    grid = []
    tables = ([soup.find(id="poolplay")] if soup.find(id="poolplay") else []) + soup.select("div.divTable")
    for table in tables:
        if table is pools:
            continue
        head = table.select_one("div.divTableRow")
        if not head or "Court" not in head.get_text():
            continue
        courts = [c.get_text(" ", strip=True) for c in head.select("div.divTableCell")][1:]
        courts = [re.sub(r"\s*\(.*?\)", "", c).strip() for c in courts]
        last_time = info["start_time"]
        for row in table.select("div.divTableRow")[1:]:
            cells = [c.get_text(" ", strip=True).replace(" ", " ") for c in row.select("div.divTableCell")]
            if not cells:
                continue
            t = cells[0].strip()
            follows = t.startswith("**")
            if not follows and re.match(r"\d", t):
                try:
                    last_time = _to24(t)
                except ValueError:
                    pass
            for i, cell in enumerate(cells[1:]):
                mm = _VS.search(cell)
                if mm:
                    grid.append({
                        "time": None if follows else last_time, "after_previous": follows,
                        "court": courts[i] if i < len(courts) else "", "a": mm.group(1), "b": mm.group(2), "work": mm.group(3),
                    })
        break  # only the pool-play grid; playoffs depend on results
    info["grid"] = grid
    return info


# ------------------------------------------------------------------ results page
PLACEMENT_BY_ROUND = [
    (re.compile(r"gold", re.I), ("1st", "2nd")),
    (re.compile(r"bronze", re.I), ("3rd", "4th")),
    (re.compile(r"5th", re.I), ("5th", "6th/7th")),
    (re.compile(r"9th", re.I), ("9th", "10th+")),
]


def parse_results(page: str) -> list[dict]:
    soup = BeautifulSoup(page, "html.parser")
    matches = []
    for card in soup.select("div.wrapper[id]"):
        label_el = card.find("div", string=re.compile(r"\(.*vs.*\)|Pool|Final|Medal", re.I))
        label = label_el.get_text(" ", strip=True) if label_el else ""
        court_el = card.find("div", string=re.compile(r"Court:", re.I))
        court = ""
        if court_el:
            cm = re.search(r"Court:\s*(\S+)", court_el.get_text(" ", strip=True))
            court = f"Court {cm.group(1)}" if cm else ""
        teams = []
        for box in card.select("div.home-box, div.away-box"):
            name_el = box.select_one("div.away")
            blocks = [b.get_text(strip=True) for b in box.select("div.set-block")]
            if name_el:
                teams.append({"name": name_el.get_text(" ", strip=True), "sets_won": blocks[0] if blocks else "",
                              "scores": [b for b in blocks[1:] if b]})
        if len(teams) == 2:
            matches.append({"round": re.sub(r"\s*\(.*\)$", "", label), "court": court, "teams": teams})
    return matches


# ------------------------------------------------------------------ team view
def _norm(s: str) -> str:
    return re.sub(r"[^a-z0-9]", "", s.lower())


def find_team(seeds: dict[str, str], aliases: list[str]) -> str | None:
    wanted = [_norm(a) for a in aliases]
    for num, name in seeds.items():
        n = _norm(name)
        if any(w and (w == n or w in n) for w in wanted):
            return num
    return None


def team_view(tid: str, sched: dict, results: list[dict], aliases: list[str]) -> dict | None:
    """Return a tournament dict (build.py format) if our team is in this division, else None."""
    seed = find_team(sched["seeds"], aliases)
    if not seed:
        return None
    team_name = sched["seeds"][seed]
    me = _norm(team_name)
    matches = []
    for g in sched["grid"]:
        if seed in (g["a"], g["b"]):
            opp = sched["seeds"].get(g["b"] if g["a"] == seed else g["a"], "TBD")
            matches.append({"time": g["time"] or "then", "court": g["court"], "opponent": opp,
                            "round": "Pool play", "role": "play"})
        elif g.get("work") == seed:
            matches.append({"time": g["time"] or "then", "court": g["court"], "round": "Work team (ref/score)",
                            "role": "work"})

    # Add results + playoff matches from the results page
    placement = None
    played = [r for r in results if any(_norm(t["name"]) == me for t in r["teams"])]
    for r in played:
        us = next(t for t in r["teams"] if _norm(t["name"]) == me)
        them = next(t for t in r["teams"] if t is not us)
        try:
            won = int(us["sets_won"]) > int(them["sets_won"])
        except ValueError:
            won = False
        tie = us["sets_won"] == them["sets_won"]
        score = (f"{'Split' if tie else 'W' if won else 'L'} {us['sets_won']}-{them['sets_won']}"
                 if us["sets_won"] != "" else "")
        existing = next((m for m in matches if m.get("opponent") == them["name"] and not m.get("result")), None)
        if existing and r["round"].lower().startswith("pool"):
            existing["result"] = score
        else:
            matches.append({"time": "", "court": r["court"], "opponent": them["name"], "round": r["round"],
                            "role": "play", "result": score})
    if played:
        last = played[-1]
        us = next(t for t in last["teams"] if _norm(t["name"]) == me)
        them = next(t for t in last["teams"] if t is not us)
        if us["sets_won"] != "" and them["sets_won"] != "":
            for rx, (win, lose) in PLACEMENT_BY_ROUND:
                if rx.search(last["round"]):
                    placement = win if int(us["sets_won"]) > int(them["sets_won"]) else lose
                    break

    age = sched["division"] or ""
    return {
        "id": f"timu-{tid}",
        "name": f"{age} {sched['gender']} {sched['tournament']}".strip(),
        "age_group": age,
        "division": sched["tier"],
        "start": sched["date"],
        "end": sched["date"],
        "first_match": next((m["time"] for m in matches if m["role"] == "play" and m["time"] and ":" in m["time"]), sched["start_time"]),
        "venue": sched["venue"],
        "address": sched["address"],
        "team_seed": team_name,
        "placement": f"{placement} ({sched['tier']})" if placement else None,
        "matches": matches,
        "links": {"schedule": f"{BASE}/scoreboards/schedule.php?tid={tid}",
                  "results": f"{BASE}/scoreboards/results.php?tid={tid}"},
        "source": "timu",
    }


# ------------------------------------------------------------------ entry point
def fetch_team_tournaments(divisions: list[str], aliases: list[str], since: str | None = None,
                           delay: float = 0.5) -> tuple[list[dict], list[str]]:
    """Scan every matching division page and keep the ones our team is in.

    `since` (YYYY-MM-DD) skips older events, keeping the scan small.
    Returns (tournaments, warnings).
    """
    warnings: list[str] = []
    found: list[dict] = []
    with httpx.Client(follow_redirects=True) as client:
        events = list_tournaments(client, divisions)
        if not events:
            return [], ["Could not read the tournament list on timu.ca – showing last saved data"]
        if since:
            events = [e for e in events if e["date"] >= since]
        log.info("timu: checking %d division pages", len(events))
        for e in events:
            page = _get(client, f"{BASE}/scoreboards/schedule.php?tid={e['tid']}")
            time.sleep(delay)
            if not page:
                warnings.append(f"Could not load {e['title']} ({e['date']})")
                continue
            try:
                sched = parse_schedule(page)
            except Exception as ex:  # layout change – skip this one, keep going
                log.warning("timu: could not parse schedule %s: %s", e["tid"], ex)
                continue
            if not find_team(sched["seeds"], aliases):
                continue
            results_page = _get(client, f"{BASE}/scoreboards/results.php?tid={e['tid']}") or ""
            time.sleep(delay)
            try:
                results = parse_results(results_page) if results_page else []
            except Exception as ex:
                log.warning("timu: could not parse results %s: %s", e["tid"], ex)
                results = []
            t = team_view(e["tid"], sched, results, aliases)
            if t:
                t["start"] = t["start"] or e["date"]
                t["end"] = t["end"] or e["date"]
                found.append(t)
    return found, warnings


def upcoming_divisions(divisions: list[str], since: str) -> list[dict]:
    """Tournaments listed for our age groups where seeding isn't posted yet (so the team can't be matched)."""
    with httpx.Client(follow_redirects=True) as client:
        return [e for e in list_tournaments(client, divisions) if e["date"] >= since]


# ------------------------------------------------------------------ game day
def page_links(tid: str) -> dict:
    """timu's own live pages for one tier – always the freshest source on game day."""
    return {name: f"{BASE}/scoreboards/{name}.php?tid={tid}" for name in ("schedule", "results", "playoffs", "pools")}


def parse_playoffs(page: str) -> list[dict]:
    """Bracket boxes come as team-top / team-space-border (score) / team-bot, in that order."""
    soup = BeautifulSoup(page, "html.parser")
    seq = soup.find_all("div", class_=["team-top", "team-bot", "team-space-border"])
    out, cur = [], {}
    for d in seq:
        cls = d.get("class") or []
        txt = d.get_text(" ", strip=True).replace("\xa0", " ").strip()
        if "team-top" in cls:
            cur = {"top": txt, "score": "", "bot": ""}
        elif "team-space-border" in cls and cur:
            cur["score"] = txt
        elif "team-bot" in cls and cur:
            cur["bot"] = txt
            out.append(cur)
            cur = {}
    return out


def pool_standings(results: list[dict], pool: str) -> list[dict]:
    """Unofficial standings from finished pool matches: wins, then set ratio, then point ratio."""
    table: dict[str, dict] = {}
    for r in results:
        if not r["round"].lower().startswith(pool.lower()):
            continue
        a, b = r["teams"]
        try:
            sa, sb = int(a["sets_won"]), int(b["sets_won"])
        except ValueError:
            continue
        pa = sum(int(x) for x in a["scores"] if x.isdigit())
        pb = sum(int(x) for x in b["scores"] if x.isdigit())
        for me, them, s_me, s_them, p_me, p_them in ((a, b, sa, sb, pa, pb), (b, a, sb, sa, pb, pa)):
            row = table.setdefault(me["name"], {"team": me["name"], "w": 0, "l": 0, "sw": 0, "sl": 0, "pf": 0, "pa": 0})
            row["w"] += s_me > s_them
            row["l"] += s_me < s_them
            row["sw"] += s_me
            row["sl"] += s_them
            row["pf"] += p_me
            row["pa"] += p_them
    rows = list(table.values())
    rows.sort(key=lambda r: (-r["w"], -(r["sw"] / max(r["sl"], 1)), -(r["pf"] / max(r["pa"], 1))))
    return rows


def game_day(tid: str, sched: dict, results: list[dict], playoffs: list[dict], aliases: list[str]) -> dict | None:
    """What a parent at the gym wants: our pool table, the last result, and who/where we play next."""
    seed = find_team(sched["seeds"], aliases)
    if not seed:
        return None
    me_name = sched["seeds"][seed]
    me = _norm(me_name)
    mine = [r for r in results if any(_norm(t["name"]) == me for t in r["teams"])]
    pool = next((r["round"] for r in mine if r["round"].lower().startswith("pool")), None)

    last = None
    if mine:
        r = mine[-1]
        us = next(t for t in r["teams"] if _norm(t["name"]) == me)
        them = next(t for t in r["teams"] if t is not us)
        last = {"opponent": them["name"], "round": r["round"], "court": r["court"],
                "score": f"{us['sets_won']}-{them['sets_won']}",
                "sets": ", ".join(f"{a}-{b}" for a, b in zip(us["scores"], them["scores"])),
                "won": str(us["sets_won"]).isdigit() and str(them["sets_won"]).isdigit()
                       and int(us["sets_won"]) > int(them["sets_won"])}
        last["label"] = "W" if last["won"] else ("Split" if us["sets_won"] == them["sets_won"] else "L")

    # Next match: first pool-grid game of ours without a result, else an unscored bracket game of ours
    played = {_norm(t["name"]) for r in mine if r["round"].lower().startswith("pool") for t in r["teams"]} - {me}
    nxt = None
    for i, g in enumerate(sched["grid"]):
        if seed not in (g["a"], g["b"]):
            continue
        opp = sched["seeds"].get(g["b"] if g["a"] == seed else g["a"], "TBD")
        if _norm(opp) in played:
            continue
        pos = sum(1 for h in sched["grid"][:i + 1] if h["court"] == g["court"])
        nxt = {"opponent": opp, "court": g["court"], "round": "Pool play",
               "time": g["time"] or f"Match {pos} on {g['court']}"}
        break
    if not nxt:
        for m in playoffs:
            names = (_norm(m["top"]), _norm(m["bot"]))
            if me in names and not re.search(r"\d", m["score"]):
                opp = m["bot"] if names[0] == me else m["top"]
                nxt = {"opponent": opp or "winner of another match", "court": "", "round": "Playoffs", "time": ""}
                break

    return {"team": me_name, "pool": pool,
            "standings": pool_standings(results, pool) if pool else [],
            "last": last, "next": nxt, "links": page_links(tid)}
