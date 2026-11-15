"""Game-day view on a real tournament (TLS McGregor Cup Trillium I, Jan 24 2026), replayed mid-day."""
from pathlib import Path

from tracker import timu

FIX = Path(__file__).parent / "fixtures"
A = ["Markham Revolution Force"]
PAGE = (FIX / "tid_3810.html").read_text()


def test_mid_pool_play():
    sched = timu.parse_schedule(PAGE)
    results = timu.parse_results(PAGE)
    # Pretend only the first two of our pool matches are finished
    ours = [r for r in results if any(t["name"] == A[0] for t in r["teams"])]
    partial = ours[:2]
    g = timu.game_day("3810", sched, partial, [], A)
    assert g["pool"].startswith("Pool A")
    assert g["last"]["opponent"] == "Vaughan Legends Emerald"
    assert g["next"]["opponent"] == "Georgina Riptide Rage Sharks"
    assert g["next"]["court"] == "Court 3" and g["next"]["time"] == "Match 3 on Court 3"
    assert g["standings"][0]["team"] == A[0]
    assert g["links"]["playoffs"].endswith("playoffs.php?tid=3810")


def test_next_playoff_from_bracket():
    sched = timu.parse_schedule(PAGE)
    pool_only = [r for r in timu.parse_results(PAGE) if r["round"].startswith("Pool")]
    bracket = timu.parse_playoffs((FIX / "playoffs_sample.html").read_text())
    assert {k: bracket[0][k] for k in ("top", "score", "bot", "played")} == \
        {"top": "Storm Blaze", "score": "25-7, 25-8", "bot": "YRV Crush", "played": True}
    g = timu.game_day("3810", sched, pool_only, bracket, A)
    assert g["next"] == {"opponent": "Southern Shores Islanders", "court": "", "round": "Playoffs", "time": ""}
    standings = {r["team"]: (r["w"], r["l"]) for r in g["standings"]}
    assert standings[A[0]] == (2, 2) or standings[A[0]][0] == 2


def test_bracket_upcoming_with_time_and_court():
    page = ('<div class="team-top">Markham Revolution FORCE</div>'
            '<div class="team-space-border">&nbsp;1:30pm Court 2</div>'
            '<div class="team-bot">Titans Velocity</div>'
            '<div class="team-top">KNT Torque</div><div class="team-space-border">25-20, 25-18</div><div class="team-bot">Unity Amethyst</div>')
    b = timu.parse_playoffs(page)
    assert b[0]["played"] is False and b[0]["when"] == "1:30pm" and b[0]["court"] == "Court 2"
    assert b[1]["played"] is True
    sched = timu.parse_schedule(PAGE)
    pool_only = [r for r in timu.parse_results(PAGE) if r["round"].startswith("Pool")]
    g = timu.game_day("3810", sched, pool_only, b, ["Markham Revolution FORCE"])
    assert g["next"] == {"opponent": "Titans Velocity", "court": "Court 2", "round": "Playoffs", "time": "1:30pm"}
    assert g["bracket"][0]["opponent"] == "Titans Velocity"


def test_waiting_for_bracket_after_pool():
    sched = timu.parse_schedule(PAGE)
    pool_only = [r for r in timu.parse_results(PAGE) if r["round"].startswith("Pool")]
    g = timu.game_day("3810", sched, pool_only, [], A)
    assert g["next"] is None and g["waiting_playoffs"] is True


def test_finished_after_last_playoff():
    sched = timu.parse_schedule(PAGE)
    pool_only = [r for r in timu.parse_results(PAGE) if r["round"].startswith("Pool")]
    bracket = timu.parse_playoffs('<div class="team-top">Markham Revolution Force</div>'
                                  '<div class="team-space-border">27-25, 25-23</div><div class="team-bot">PVC SC Legacy</div>')
    g = timu.game_day("3810", sched, pool_only, bracket, A)
    assert g["next"] is None and g["finished"] is True and g["waiting_playoffs"] is False


def test_bracket_score_from_our_side():
    b = timu.parse_playoffs('<div class="team-top">KINETIKA Torque</div>'
                            '<div class="team-space-border">18-25, 25-22, 17-15</div><div class="team-bot">Markham Revolution Force</div>')
    g = timu.game_day("3810", timu.parse_schedule(PAGE), [], b, A)
    assert g["bracket"][0]["score"] == "L 25-18, 22-25, 15-17"
