"""OVA calendar + event-page parser tests (trimmed copies of real pages, Sept 2026)."""
from pathlib import Path

from tracker import ova_events as ova

FIX = Path(__file__).parent / "fixtures"
A = ["Markham Revolution Force"]


def test_month_filter_regular_d1_only():
    events = ova.parse_month((FIX / "ova_month_2026_11.js").read_text())
    assert len(events) == 4 and events[0] == {"id": "588428285", "title": "TLS Girls Division 1 Provincial Cup (Sat OR Sun)", "date": "2026-11-14"}
    import re
    keep = [e for e in events if re.search(r"^TLS Girls Division 1 ", e["title"]) and any(c in e["title"] for c in ova.REGULAR_CUPS)]
    assert [e["id"] for e in keep] == ["588428285"]   # D2, 15U and exhibition dropped


def test_event_finds_only_our_tier():
    r = ova.parse_event((FIX / "ova_event_579584612.html").read_text(), A, "2025-11-15")
    t = r["tier"]
    assert t["tier"] == "Trillium I" and t["day"] == "2025-11-15"
    assert t["venue"] == "Huron Heights" and t["address"].startswith("40 Huron Heights")
    assert t["timu_tid"] == "3319"


def test_event_splits_not_posted():
    r = ova.parse_event((FIX / "ova_event_pending.html").read_text(), A, "2026-11-14")
    assert r == {"splits_posted": False, "tier": None}
    assert ova.clean_title("TLS Girls Division 1 Challenge Cup (Sat Or Sun)") == "TLS Girls Division 1 Challenge Cup"
