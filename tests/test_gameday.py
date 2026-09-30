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
    assert bracket[0] == {"top": "Storm Blaze", "score": "25-7, 25-8", "bot": "YRV Crush"}
    g = timu.game_day("3810", sched, pool_only, bracket, A)
    assert g["next"] == {"opponent": "Southern Shores Islanders", "court": "", "round": "Playoffs", "time": ""}
    standings = {r["team"]: (r["w"], r["l"]) for r in g["standings"]}
    assert standings[A[0]] == (2, 2) or standings[A[0]][0] == 2
