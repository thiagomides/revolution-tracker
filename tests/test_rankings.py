"""Rankings parser tests against a trimmed copy of the real OVA girls rankings page (Sept 2026)."""
from pathlib import Path

from tracker import rankings

PAGE = (Path(__file__).parent / "fixtures" / "rankings_sample.html").read_text()


def test_force_final_rank():
    r = rankings.parse(PAGE, ["Markham Revolution Force"])
    assert len(r["entries"]) == 1
    e = r["entries"][0]
    assert e["age_group"] == "6v6 Girls"
    assert e["rank"] == "59" and e["of"] == 4
    assert e["detail"]["Pre OC Rank"] == "39"
    assert r["splits"][0]["label"].startswith("15U Girls Division Split")


def test_points_table_and_unranked():
    rush = rankings.parse(PAGE, ["Markham Revolution Rush"])["entries"][0]
    assert rush["rank"] == "40" and rush["detail"]["Top 2 Average"] == "795"
    flash = rankings.parse(PAGE, ["Markham Revolution Flash"])["entries"][0]
    assert flash["rank"] is None   # listed but not officially ranked


def test_no_substring_match():
    assert rankings.parse(PAGE, ["Revolution"])["entries"] == []
