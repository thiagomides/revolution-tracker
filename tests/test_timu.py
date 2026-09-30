"""Parser tests against saved copies of real timu.ca pages (TLS Girls Provincial Cup, Nov 15 2025).

Run:  python -m pytest -q      (or: python tests/test_timu.py)
"""
from pathlib import Path

from tracker import timu

FIX = Path(__file__).parent / "fixtures"
ALIASES = ["Markham Revolution Force"]


def load():
    sched = timu.parse_schedule((FIX / "schedule_3319.html").read_text())
    results = timu.parse_results((FIX / "results_3319.html").read_text())
    return sched, results


def test_schedule_header():
    s, _ = load()
    assert s["venue"] == "Huron Heights SS"
    assert s["address"].startswith("40 Huron Heights Dr")
    assert s["date"] == "2025-11-15"
    assert s["start_time"] == "09:00"
    assert s["seeds"]["10"] == "Markham Revolution Force"
    assert len(s["grid"]) == 12


def test_team_view():
    s, r = load()
    t = timu.team_view("3319", s, r, ALIASES)
    assert t is not None
    pool = [m for m in t["matches"] if m["round"] == "Pool play"]
    assert [m["opponent"] for m in pool] == ["Georgina Riptide Rage Sharks", "Southern Shores Storm"]
    assert pool[0]["time"] == "09:00" and pool[0]["court"] == "Court 2"
    assert pool[0]["result"] == "W 2-1"
    assert t["placement"].startswith("3rd")
    assert t["first_match"] == "09:00"


def test_other_team_not_matched():
    s, r = load()
    assert timu.team_view("3319", s, r, ["Markham Revolution Rush"]) is None


if __name__ == "__main__":
    test_schedule_header(); test_team_view(); test_other_team_not_matched()
    print("all timu tests passed")
