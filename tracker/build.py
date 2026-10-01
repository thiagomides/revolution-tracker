"""Build the schedule site: merge data → site/index.html, schedule.ics, data.json, digest.txt.

Run locally:  python -m tracker.build
"""
from __future__ import annotations

import hashlib
import json
import logging
import os
import smtplib
import re
import sys
import time
from datetime import date, datetime, timedelta
from email.message import EmailMessage
from pathlib import Path
from urllib.parse import quote, urlencode
from zoneinfo import ZoneInfo

import yaml
from icalendar import Alarm, Calendar, Event
from jinja2 import Environment, FileSystemLoader, select_autoescape

from tracker import aes, ova_events, rankings, timu

ROOT = Path(__file__).resolve().parent.parent
SITE = ROOT / "site"
DATA = ROOT / "data"
CACHE = DATA / "aes_cache.json"     # last good AES result per tournament
STATE = DATA / "last_build.json"    # previous build, for change detection
RANK_CACHE = DATA / "rankings_cache.json"  # last good OVA ranking for our team
OVA_CACHE = DATA / "ova_cache.json"  # {event_id: {title, date, tier, team}} from the OVA calendar
TIMU_CACHE = DATA / "timu_cache.json"  # {tid: tournament|null}; null = our team not in that division

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
log = logging.getLogger("build")


# ---------------------------------------------------------------- loading
def load_yaml(p: Path) -> dict:
    with p.open(encoding="utf-8") as f:
        return yaml.safe_load(f) or {}


def validate(t: dict) -> list[str]:
    errs = []
    for k in ("id", "name", "start"):
        if not t.get(k):
            errs.append(f"missing '{k}'")
    for k in ("start", "end"):
        if t.get(k):
            try:
                date.fromisoformat(str(t[k]))
            except ValueError:
                errs.append(f"bad date in '{k}': {t[k]}")
    return errs


# ---------------------------------------------------------------- merging
def merge_aes(tournaments: list[dict], cfg: dict) -> list[str]:
    """Overlay AES data onto tournaments. Returns warnings for the status bar."""
    warnings = []
    cache = json.loads(CACHE.read_text()) if CACHE.exists() else {}
    names = cfg["team"].get("aliases", []) + [cfg["team"]["name"]]
    by_id = {t["id"]: t for t in tournaments}

    for link in cfg.get("aes_team_links") or []:
        tid, url = link.get("tournament_id"), link.get("url")
        if tid not in by_id:
            warnings.append(f"AES link points to unknown tournament id '{tid}'")
            continue
        fresh = aes.fetch_team(url, names)
        if fresh:
            cache[tid] = {**fresh, "fetched": datetime.utcnow().isoformat(timespec="seconds")}
        elif tid in cache:
            warnings.append(f"AES unreachable for {by_id[tid]['name']} – showing last saved data")
        else:
            warnings.append(f"AES schedule not available yet for {by_id[tid]['name']}")
            continue
        data = cache[tid]
        t = by_id[tid]
        if data.get("matches"):
            t["matches"] = data["matches"]          # AES wins over manual entries
            plays = [m for m in data["matches"] if m.get("role") == "play"]
            if plays:
                t["first_match"] = min(plays, key=lambda m: (m["date"], m["time"]))["time"]
        if data.get("placement") and not t.get("placement_manual"):
            t["placement"] = data["placement"]
        t.setdefault("links", {})["aes"] = data.get("aes_url")

    CACHE.write_text(json.dumps(cache, indent=2))
    return warnings


def collect_timu(cfg: dict, today: date) -> tuple[list[dict], list[str], list[dict]]:
    """Team tournaments from timu.ca, using a cache so finished events aren't re-downloaded.

    Returns (team_tournaments, warnings, upcoming_division_events).
    """
    tc = cfg.get("timu") or {}
    if not tc.get("enabled"):
        return [], [], []
    cache: dict = json.loads(TIMU_CACHE.read_text()) if TIMU_CACHE.exists() else {}
    aliases = cfg["team"].get("aliases") or [cfg["team"]["name"]]
    warnings: list[str] = []
    upcoming: list[dict] = []
    offline = skip_full_refresh()

    if not offline:
        import httpx
        with httpx.Client(follow_redirects=True) as client:
            events = timu.list_tournaments(client, tc.get("divisions") or [])
            if not events:
                warnings.append("Couldn't read the tournament list on timu.ca – showing last saved data")
            recheck_from = (today - timedelta(days=3)).isoformat()
            for e in events:
                if e["date"] < tc.get("season_start", "1900-01-01"):
                    continue
                if not is_regular(e["title"]):
                    continue  # skip Non-OVA / exhibition events – the team only plays regular-season cups
                cached = cache.get(e["tid"], "missing")
                # Re-check: anything upcoming or within 3 days (results, reseeding); fetch older ones once.
                if cached != "missing" and e["date"] < recheck_from:
                    continue
                if e["date"] > today.isoformat():
                    upcoming.append(e)
                page = timu._get(client, f"{timu.BASE}/scoreboards/schedule.php?tid={e['tid']}")
                time.sleep(float(tc.get("delay_seconds", 0.5)))
                if not page:
                    warnings.append(f"Couldn't load {e['title']} ({e['date']}) – will retry next run")
                    continue
                try:
                    sched = timu.parse_schedule(page)
                except Exception as ex:
                    log.warning("timu parse failed for %s: %s", e["tid"], ex)
                    continue
                if not timu.find_team(sched["seeds"], aliases):
                    # Seeding not posted yet → keep checking; posted without us → remember
                    cache[e["tid"]] = None if sched["seeds"] else cache.get(e["tid"])
                    continue
                res_page = timu._get(client, f"{timu.BASE}/scoreboards/results.php?tid={e['tid']}") or ""
                time.sleep(float(tc.get("delay_seconds", 0.5)))
                try:
                    results = timu.parse_results(res_page) if res_page else []
                except Exception as ex:
                    log.warning("timu results parse failed for %s: %s", e["tid"], ex)
                    results = []
                t = timu.team_view(e["tid"], sched, results, aliases)
                if t:
                    t["start"] = t["start"] or e["date"]
                    t["end"] = t["end"] or e["date"]
                    cache[e["tid"]] = t
        if events:
            cache["_upcoming"] = upcoming
        TIMU_CACHE.write_text(json.dumps(cache, indent=2))
    else:
        upcoming = cache.get("_upcoming") or []
    upcoming = [e for e in upcoming if e["date"] >= today.isoformat()]

    keep_from = tc.get("show_results_from") or tc.get("season_start") or "1900-01-01"
    team = [t for k, t in cache.items() if not k.startswith("_") and isinstance(t, dict) and str(t.get("start", "")) >= keep_from]
    team_dates = {t["start"] for t in team}
    # Upcoming events in our divisions where the team isn't seeded yet – one row per event/date
    seen, div_events = set(), []
    for e in sorted(upcoming, key=lambda x: x["date"]):
        base = e["title"]
        for tier in ("Premier", "Select", "Championship", "Trillium", "Exhibition"):
            if f" {tier}" in base:
                base = base.split(f" {tier}")[0]
                break
        key = (e["date"], base)
        if key in seen or e["date"] in team_dates:
            continue
        seen.add(key)
        div_events.append({"date": e["date"], "name": base,
                           "label": date.fromisoformat(e["date"]).strftime("%a %b %-d")})
    team = [t for t in team if is_regular(t.get("name", "") + " " + str(t.get("division", "")))]
    return team, warnings, []  # the "other events in our age group" list is no longer shown


def skip_full_refresh() -> bool:
    """OFFLINE=1: use saved data only. LIVE_ONLY=1 (game-day runs every ~10 min): refresh only today's tournament."""
    return os.environ.get("OFFLINE") == "1" or os.environ.get("LIVE_ONLY") == "1"


def add_live(ts: list[dict], cfg: dict, today: date) -> int:
    """Game day: pull timu's live pages for today's tournament. Returns how many were refreshed."""
    aliases = cfg["team"].get("aliases") or [cfg["team"]["name"]]
    refreshed = 0
    for t in ts:
        tid = t.get("timu_tid") or (t["id"][5:] if str(t["id"]).startswith("timu-") else None)
        if not tid:
            continue
        t["links"].update({k: v for k, v in timu.page_links(tid).items() if k not in t["links"]})
        if not (t["_start"] <= today <= t["_end"]) or os.environ.get("OFFLINE") == "1":
            continue
        import httpx
        with httpx.Client(follow_redirects=True) as client:
            sp = timu._get(client, f"{timu.BASE}/scoreboards/schedule.php?tid={tid}")
            rp = timu._get(client, f"{timu.BASE}/scoreboards/results.php?tid={tid}") or ""
            pp = timu._get(client, f"{timu.BASE}/scoreboards/playoffs.php?tid={tid}") or ""
        if not sp:
            continue
        try:
            sched, results = timu.parse_schedule(sp), (timu.parse_results(rp) if rp else [])
            bracket = timu.parse_playoffs(pp) if pp else []
            view = timu.team_view(tid, sched, results, aliases)
            if view:
                for k in ("matches", "placement", "first_match"):
                    if view.get(k):
                        t[k] = view[k]
            t["live"] = timu.game_day(tid, sched, results, bracket, aliases)
            t["live_checked"] = datetime.now(ZoneInfo(cfg["site"]["timezone"])).strftime("%-I:%M %p")
            refreshed += 1
        except Exception as ex:
            log.warning("game-day parse failed for tid %s: %s", tid, ex)
    return refreshed


def is_regular(title: str) -> bool:
    t = title.lower()
    return any(c.lower() in t for c in ova_events.REGULAR_CUPS) and "non-ova" not in t and "exhibition" not in t


def collect_ova(cfg: dict, today: date) -> tuple[list[dict], list[str]]:
    """Our regular-season tournaments from the OVA calendar, showing only the tier we play in."""
    oc = cfg.get("ova_events") or {}
    if not oc.get("enabled"):
        return [], []
    cache: dict = json.loads(OVA_CACHE.read_text()) if OVA_CACHE.exists() else {}
    aliases = cfg["team"].get("aliases") or [cfg["team"]["name"]]
    season_start = str((cfg.get("timu") or {}).get("season_start", f"{today.year}-09-01"))
    warnings: list[str] = []
    if not skip_full_refresh():
        import httpx
        with httpx.Client(follow_redirects=True) as client:
            events = ova_events.list_events(client, str(oc.get("calendar_id")), season_start, oc.get("titles") or [])
            if not events:
                warnings.append("Couldn't read the OVA events calendar – showing last saved tournaments")
            for e in events:
                old = cache.get(e["id"]) or {}
                done = old.get("team") and old["team"].get("placement")
                if done and e["date"] < (today - timedelta(days=3)).isoformat():
                    continue  # finished and recorded – no need to fetch again
                entry = {**old, "title": e["title"], "date": e["date"]}
                page = ova_events._get(client, f"{ova_events.BASE}/event/show/{e['id']}")
                time.sleep(0.5)
                if page:
                    try:
                        parsed = ova_events.parse_event(page, aliases, e["date"])
                        entry["splits_posted"] = parsed["splits_posted"]
                        if parsed["tier"]:
                            entry["tier"] = parsed["tier"]
                    except Exception as ex:
                        log.warning("OVA event parse failed for %s: %s", e["id"], ex)
                tid = (entry.get("tier") or {}).get("timu_tid")
                if tid:
                    sched_page = timu._get(client, f"{timu.BASE}/scoreboards/schedule.php?tid={tid}")
                    res_page = timu._get(client, f"{timu.BASE}/scoreboards/results.php?tid={tid}") or ""
                    if sched_page:
                        try:
                            view = timu.team_view(tid, timu.parse_schedule(sched_page),
                                                  timu.parse_results(res_page) if res_page else [], aliases)
                            if view:
                                entry["team"] = view
                        except Exception as ex:
                            log.warning("timu parse failed for tid %s: %s", tid, ex)
                cache[e["id"]] = entry
            if events:
                cache["_listed"] = [e["id"] for e in events]
        OVA_CACHE.write_text(json.dumps(cache, indent=2))

    listed = set(cache.get("_listed") or [k for k in cache if not k.startswith("_")])
    out = []
    for eid, entry in cache.items():
        if eid.startswith("_") or eid not in listed or entry.get("date", "") < season_start:
            continue
        title = ova_events.clean_title(entry["title"])
        age = title.split()[0]
        name = title.replace(" Division 1 ", " D1 ").replace(" Division 2 ", " D2 ")
        t = {"id": f"ova-{eid}", "name": name, "age_group": age, "start": entry["date"],
             "end": (date.fromisoformat(entry["date"]) + timedelta(days=1)).isoformat(),
             "links": {"ova": f"{ova_events.BASE}/event/show/{eid}"}}
        tier = entry.get("tier")
        if not tier and entry.get("splits_posted"):
            continue  # splits are out and we're not in any tier – not our tournament
        if not tier and (cfg.get("ova_events") or {}).get("hide_until_splits", {}).get(age, False):
            continue
        if tier:
            t.update({"division": tier["tier"], "venue": tier.get("venue"), "address": tier.get("address")})
            if tier.get("day"):
                t["start"] = t["end"] = tier["day"]
            if tier.get("timu_tid"):
                t["links"]["schedule"] = f"{timu.BASE}/scoreboards/schedule.php?tid={tier['timu_tid']}"
                t["timu_tid"] = tier["timu_tid"]
        else:
            t["tier_pending"] = True
            t["notes"] = ("Tier, day (Sat or Sun) and venue will appear here once OVA posts the team splits, "
                          "usually about a month before.")
        team = entry.get("team")
        if team:
            for k in ("matches", "placement", "first_match"):
                if team.get(k):
                    t[k] = team[k]
            t["links"]["results"] = team.get("links", {}).get("results")
        out.append(t)
    return out, warnings


def collect_rankings(cfg: dict, tz: ZoneInfo) -> tuple[dict, list[str], list[str]]:
    """Our team's OVA ranking(s). Returns (data, warnings, changes)."""
    rc = cfg.get("rankings") or {}
    cached = json.loads(RANK_CACHE.read_text()) if RANK_CACHE.exists() else {}
    if not rc.get("enabled", True):
        return {}, [], []
    warnings, changes = [], []
    if not skip_full_refresh():
        aliases = cfg["team"].get("aliases") or [cfg["team"]["name"]]
        fresh, err = rankings.fetch(aliases)
        if err:
            warnings.append(err)
        elif fresh is not None:
            old = {(e["age_group"], e["title"]): e.get("rank") for e in cached.get("entries", [])}
            for e in fresh["entries"]:
                key = (e["age_group"], e["title"])
                if key in old and old[key] != e["rank"]:
                    changes.append(f"OVA ranking ({e['age_group']}): {old[key] or '—'} → {e['rank'] or '—'}")
                elif key not in old and cached:
                    changes.append(f"New OVA ranking posted ({e['age_group']}): {e['rank'] or 'listed'}")
            cached = {**fresh, "fetched": datetime.now(tz).strftime("%b %-d, %Y")}
            RANK_CACHE.write_text(json.dumps(cached, indent=2))
    # Put our current competitions first (TLS / 15U), then anything else the team is listed in
    mine = [DIVISION_NAMES.get(d, d) for d in (cfg.get("timu") or {}).get("divisions", [])]
    entries = sorted(cached.get("entries", []), key=lambda e: (e["age_group"] not in mine, e["age_group"]))
    y = int(str((cfg.get("timu") or {}).get("season_start", "2026"))[:4])
    season_rx = re.compile(rf"{y}\s*[-/]\s*(20)?{(y + 1) % 100:02d}")
    prev_rx = re.compile(rf"{y - 1}\s*[-/]\s*(20)?{y % 100:02d}")
    for e in entries:
        # Current if it names this season, or it's one of our divisions and doesn't name last season
        e["current"] = bool(season_rx.search(e["title"])) or (e["age_group"] in mine and not prev_rx.search(e["title"]))
    has_current = any(e["current"] and e["age_group"] in mine for e in entries)
    season = f"{y}-{(y + 1) % 100:02d}"
    return {**cached, "entries": entries, "mine": mine, "has_current": has_current, "season": season}, warnings, changes


DIVISION_NAMES = {"15UG": "15U Girls", "TLSG": "TLS Girls", "6v6G": "6v6 Girls", "16UG": "16U Girls",
                  "17UG": "17U Girls", "18UG": "18U Girls", "4v4G": "4v4 Girls"}


def ordinal(n: str | None) -> str:
    if not n or not str(n).isdigit():
        return "—"
    n = int(n)
    return f"{n}{'th' if 10 <= n % 100 <= 20 else {1: 'st', 2: 'nd', 3: 'rd'}.get(n % 10, 'th')}"


# ---------------------------------------------------------------- helpers
def gmaps(addr: str) -> str:
    return "https://www.google.com/maps/search/?api=1&" + urlencode({"query": addr})


def event_window(t: dict, tz: ZoneInfo):
    """(start, end, all_day) for a tournament."""
    s = date.fromisoformat(str(t["start"]))
    e = date.fromisoformat(str(t.get("end") or t["start"]))
    if t.get("first_match") and s == e:
        hh, mm = map(int, str(t["first_match"]).split(":"))
        start = datetime(s.year, s.month, s.day, hh, mm, tzinfo=tz)
        times = [m["time"] for m in t.get("matches", []) if re.fullmatch(r"\d{1,2}:\d{2}", str(m.get("time", "")))]
        # OVA one-day events usually finish mid/late afternoon; use that unless a later time is listed.
        end = start.replace(hour=17, minute=0)
        if times:
            lh, lm = map(int, max(times).split(":"))
            end = max(end, datetime(s.year, s.month, s.day, lh, lm, tzinfo=tz) + timedelta(hours=1, minutes=15))
        return start, max(end, start + timedelta(hours=1)), False
    return s, e + timedelta(days=1), True


def describe(t: dict, cfg: dict) -> str:
    lines = [f"{t.get('age_group', '')} {t.get('division', '')}".strip()]
    for m in t.get("matches", []):
        who = f"vs {m['opponent']}" if m.get("opponent") else m.get("round", "")
        if m.get("opponent") and m.get("round") and m["round"] != "Pool play":
            who = f"{m['round']} {who}"
        when = m.get("time") or ""
        res = f" ({m['result']})" if m.get("result") else ""
        lines.append(" ".join(x for x in (f"• {when}", m.get("court", ""), f"– {who}{res}") if x.strip()))
    if t.get("placement"):
        lines.append(f"Result: {t['placement']}")
    if t.get("notes"):
        lines.append(t["notes"])
    for label, url in (t.get("links") or {}).items():
        if url:
            lines.append(f"{label.upper()}: {url}")
    lines.append(f"Full schedule: {cfg['site']['base_url']}")
    return "\n".join(l for l in lines if l)


def gcal_link(t: dict, cfg: dict, tz: ZoneInfo) -> str:
    start, end, all_day = event_window(t, tz)
    fmt = (lambda d: d.strftime("%Y%m%d")) if all_day else (lambda d: d.astimezone(ZoneInfo("UTC")).strftime("%Y%m%dT%H%M%SZ"))
    q = {
        "action": "TEMPLATE",
        "text": f"🏐 {cfg['team']['short_name']}: {t['name']}",
        "dates": f"{fmt(start)}/{fmt(end)}",
        "details": describe(t, cfg),
        "location": t.get("address") or t.get("venue", ""),
        "ctz": cfg["site"]["timezone"],
    }
    return "https://calendar.google.com/calendar/render?" + urlencode(q, quote_via=quote)


def make_event(t: dict, cfg: dict, tz: ZoneInfo) -> Event:
    start, end, all_day = event_window(t, tz)
    ev = Event()
    ev.add("uid", f"{t['id']}@revolution-tracker")        # stable → updates, not duplicates
    ev.add("summary", f"🏐 {cfg['team']['short_name']}: {t['name']}")
    ev.add("dtstart", start)
    ev.add("dtend", end)
    ev.add("dtstamp", datetime.now(tz))
    loc = ", ".join(x for x in (t.get("venue"), t.get("address")) if x and x != "TBA")
    if loc:
        ev.add("location", loc)
    ev.add("description", describe(t, cfg))
    ev.add("url", cfg["site"]["base_url"])
    # Sequence bumps when content changes so clients refresh the entry
    ev.add("sequence", int(hashlib.md5(describe(t, cfg).encode()).hexdigest()[:6], 16) % 100000)
    alarm = Alarm()
    alarm.add("action", "DISPLAY")
    alarm.add("description", f"Tomorrow: {t['name']}")
    alarm.add("trigger", timedelta(hours=-12) if not all_day else timedelta(hours=-4))
    ev.add_component(alarm)
    return ev


def build_calendar(ts: list[dict], cfg: dict, tz: ZoneInfo) -> bytes:
    cal = Calendar()
    cal.add("prodid", "-//Revolution Force Tracker//EN")
    cal.add("version", "2.0")
    cal.add("calscale", "GREGORIAN")
    cal.add("x-wr-calname", cfg["site"]["title"])
    cal.add("x-wr-timezone", cfg["site"]["timezone"])
    cal.add("refresh-interval;value=duration", "PT6H")
    cal.add("x-published-ttl", "PT6H")
    for t in ts:
        cal.add_component(make_event(t, cfg, tz))
    try:
        cal.add_missing_timezones()  # embeds VTIMEZONE so every calendar app agrees on Toronto time
    except AttributeError:
        pass
    return cal.to_ical()


def digest(ts: list[dict], cfg: dict, today: date, div_events: list[dict] | None = None) -> str:
    up = [t for t in ts if t["_end"] >= today][:3]
    out = [f"🏐 {cfg['team']['name']} – upcoming tournaments", ""]
    if not up and div_events:
        out.append("Our division isn't posted yet. Next events in our age groups:")
        out += [f"• {e['label']} – {e['name']}" for e in div_events[:3]]
    for t in up:
        when = t["_date_label"] + (f", first match {t['first_match']}" if t.get("first_match") else "")
        out.append(f"• {t['name']} ({t.get('age_group', '')})")
        out.append(f"  {when}")
        out.append(f"  📍 {t.get('venue', 'TBA')}")
    out += ["", f"Full schedule + add to calendar: {cfg['site']['base_url']}"]
    return "\n".join(out)


def date_label(s: date, e: date) -> str:
    if s == e:
        return s.strftime("%a %b %-d")
    if s.month == e.month:
        return f"{s.strftime('%a %b %-d')}–{e.strftime('%-d')}"
    return f"{s.strftime('%b %-d')} – {e.strftime('%b %-d')}"


# ---------------------------------------------------------------- changes
def fingerprint(t: dict) -> dict:
    return {k: t.get(k) for k in ("start", "end", "first_match", "venue", "address", "placement", "matches")}


def detect_changes(ts: list[dict]) -> list[str]:
    old = json.loads(STATE.read_text()) if STATE.exists() else {}
    new = {t["id"]: fingerprint(t) for t in ts}
    changes = []
    for tid, fp in new.items():
        name = next(t["name"] for t in ts if t["id"] == tid)
        if tid not in old:
            if old:
                changes.append(f"New tournament: {name}")
            continue
        for field in ("start", "first_match", "venue", "placement"):
            if old[tid].get(field) != fp.get(field):
                changes.append(f"{name}: {field.replace('_', ' ')} → {fp.get(field) or '—'}")
        if old[tid].get("matches") != fp.get("matches") and fp.get("matches"):
            changes.append(f"{name}: match schedule updated")
    STATE.write_text(json.dumps(new, indent=2, default=str))
    return changes


def send_alert(changes: list[str], cfg: dict, digest_text: str):
    host, user, pw = (os.environ.get(k) for k in ("SMTP_HOST", "SMTP_USER", "SMTP_PASS"))
    to = cfg.get("alerts", {}).get("to") or []
    if not (cfg.get("alerts", {}).get("email_enabled") and host and user and pw and to):
        return
    msg = EmailMessage()
    msg["Subject"] = f"🏐 {cfg['team']['short_name']} schedule update"
    msg["From"], msg["To"] = user, ", ".join(to)
    msg.set_content("Changes:\n" + "\n".join(f"• {c}" for c in changes) + "\n\n" + digest_text)
    try:
        with smtplib.SMTP(host, int(os.environ.get("SMTP_PORT", 587)), timeout=30) as s:
            s.starttls()
            s.login(user, pw)
            s.send_message(msg)
        log.info("Alert email sent to %d recipients", len(to))
    except Exception as e:  # never fail the build because of email
        log.warning("Alert email failed: %s", e)


# ---------------------------------------------------------------- main
def main() -> int:
    cfg = load_yaml(ROOT / "config.yaml")
    tz = ZoneInfo(cfg["site"]["timezone"])
    raw = load_yaml(DATA / "tournaments.yaml").get("tournaments") or []
    ages = set(cfg.get("age_groups") or [])

    ts, warnings = [], []
    for t in raw:
        errs = validate(t)
        if errs:
            warnings.append(f"Skipped entry {t.get('id') or t.get('name')}: {', '.join(errs)}")
            continue
        if ages and t.get("age_group") and t["age_group"] not in ages:
            continue
        t.setdefault("links", {})
        t["placement_manual"] = bool(t.get("placement"))
        ts.append(t)

    today = datetime.now(tz).date()
    timu_ts, timu_warn, div_events = collect_timu(cfg, today)
    warnings += timu_warn
    rank_data, rank_warn, rank_changes = collect_rankings(cfg, tz)
    warnings += rank_warn
    # timu data replaces a hand entry for the same date + competition
    for t in timu_ts:
        ts[:] = [m for m in ts if not (str(m["start"]) == t["start"] and m.get("age_group") == t.get("age_group"))]
        t["placement_manual"] = False
        ts.append(t)
    ova_ts, ova_warn = collect_ova(cfg, today)
    warnings += ova_warn
    for t in ova_ts:
        # One entry per tournament: drop the timu-scan copy of the same tier, and any hand entry that day
        ts[:] = [m for m in ts if m["id"] != f"timu-{t.get('timu_tid')}"
                 and not (m.get("source") != "timu" and str(m["start"]) == t["start"] and m.get("age_group") == t["age_group"])]
        t["placement_manual"] = False
        ts.append(t)
    warnings += merge_aes(ts, cfg)

    for t in ts:
        s = date.fromisoformat(str(t["start"]))
        e = date.fromisoformat(str(t.get("end") or t["start"]))
        t["_start"], t["_end"] = s, e
        t["_date_label"] = date_label(s, e)
        if t.get("tier_pending") and (e - s).days == 1:
            t["_date_label"] = f"{s.strftime('%a %b %-d')} or {e.strftime('%a %b %-d')}"
        t["_status"] = "past" if e < today else ("live" if s <= today <= e else "upcoming")
        t["_days_until"] = (s - today).days
        t["_gcal"] = gcal_link(t, cfg, tz)
        t["_map"] = gmaps(t["address"]) if t.get("address") else None
        t["_ics"] = f"events/{t['id']}.ics"
    ts.sort(key=lambda t: t["_start"])

    live_count = add_live(ts, cfg, today)
    if os.environ.get("LIVE_ONLY") == "1" and not live_count:
        log.info("Game-day run: no tournament today – nothing to publish.")
        if os.environ.get("GITHUB_OUTPUT"):
            with open(os.environ["GITHUB_OUTPUT"], "a") as f:
                f.write("publish=false\n")
        return 0
    if os.environ.get("GITHUB_OUTPUT"):
        with open(os.environ["GITHUB_OUTPUT"], "a") as f:
            f.write("publish=true\n")

    if not ts and not div_events:
        log.error("No tournaments found at all – refusing to publish an empty site (keeping the last one).")
        return 1

    rcfg = cfg.get("rankings") or {}
    show_rank = rcfg.get("show_on_main_page", True)
    changes = detect_changes(ts) + (rank_changes if show_rank else [])
    digest_text = digest(ts, cfg, today, div_events)
    top = next((e for e in rank_data.get("entries", []) if e.get("rank")), None)
    if top and show_rank:
        digest_text = digest_text.replace("\n\nFull schedule", f"\n\n📊 OVA ranking: {ordinal(top['rank'])} of {top['of']} ({top['age_group']}, {top['title']})\n\nFull schedule", 1)

    SITE.mkdir(exist_ok=True)
    (SITE / "events").mkdir(exist_ok=True)
    (SITE / "schedule.ics").write_bytes(build_calendar(ts, cfg, tz))
    for t in ts:
        (SITE / t["_ics"]).write_bytes(build_calendar([t], cfg, tz))
    (SITE / "digest.txt").write_text(digest_text, encoding="utf-8")
    public = [{k: v for k, v in t.items() if not k.startswith("_") and k != "placement_manual"} for t in ts]
    (SITE / "data.json").write_text(json.dumps(public, indent=2, default=str), encoding="utf-8")

    base = cfg["site"]["base_url"].rstrip("/")
    feed_https = f"{base}/schedule.ics"
    feed_webcal = feed_https.replace("https://", "webcal://").replace("http://", "webcal://")
    env = Environment(loader=FileSystemLoader(ROOT / "templates"), autoescape=select_autoescape(["html", "j2"]))
    ctx = dict(
        cfg=cfg,
        upcoming=[t for t in ts if t["_status"] != "past"],
        past=list(reversed([t for t in ts if t["_status"] == "past"])),
        has_sample=any(t.get("sample") for t in ts),
        warnings=warnings,
        updated=datetime.now(tz).strftime("%b %-d, %Y at %-I:%M %p"),
        feed_https=feed_https,
        feed_webcal=feed_webcal,
        gcal_subscribe="https://calendar.google.com/calendar/r?cid=" + quote(feed_webcal, safe=""),
        share_text=digest_text,
        whatsapp="https://wa.me/?text=" + quote(digest_text),
        mailto="mailto:?subject=" + quote(f"{cfg['team']['short_name']} tournament schedule") + "&body=" + quote(digest_text),
        division_events=div_events,
        ranking=rank_data if show_rank else None,
        ordinal=ordinal,
    )
    tpl = env.get_template("index.html.j2")
    (SITE / "index.html").write_text(tpl.render(standalone=True, **ctx), encoding="utf-8")
    if not show_rank and rcfg.get("private_page") and rank_data:
        # Same page with the ranking, at an unlisted address (not linked anywhere, not indexed)
        (SITE / rcfg["private_page"]).write_text(
            tpl.render(standalone=True, **{**ctx, "ranking": rank_data, "private": True}), encoding="utf-8")
    if os.environ.get("PREVIEW_OUT"):
        Path(os.environ["PREVIEW_OUT"]).write_text(tpl.render(standalone=False, **ctx), encoding="utf-8")

    for w in warnings:
        log.warning(w)
    if changes:
        log.info("Changes detected:\n  " + "\n  ".join(changes))
        if os.environ.get("LIVE_ONLY") != "1":  # no email per score update on game day
            send_alert(changes, cfg, digest_text)
    log.info("Built %d tournaments → %s", len(ts), SITE)
    return 0


if __name__ == "__main__":
    sys.exit(main())
