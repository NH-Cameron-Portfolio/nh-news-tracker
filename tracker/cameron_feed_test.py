"""
cameron_feed regression test — verify the microsite JSON output:
  - industry resolves to exactly one of the four allowed values
  - regulators/bodies route correctly (Ofwat->utilities, Ofgem->energy)
  - source is recovered from the Google-News title suffix
  - markdown/whitespace stripped from headline/body
  - why_matters is "" when absent, populated when present
  - schema keys exactly match the spec
"""

import json
from datetime import date, datetime, timezone
from pathlib import Path
from tracker import cameron_feed, email_render, sources

ROOT = Path(__file__).resolve().parent.parent
CONFIG = ROOT / "config"

ALLOWED_INDUSTRIES = {"energy", "utilities", "media", "comms"}
REQUIRED_KEYS = {"industry", "period", "date", "source", "headline", "body", "why_matters", "relevance"}
# url is included when present, so it's an optional key


def load(name):
    return json.load(open(CONFIG / name))


def mk(title, summary, matched, why="", tier="RELEVANT", score=7, url="https://news.google.com/x"):
    it = sources.NewsItem(
        title=title, url=url, summary=summary,
        source_name="Google News", source_domain="news.google.com",
        feed_tags=[], published_at=datetime.now(timezone.utc),
    )
    it.matched_clients = matched
    it.why_it_matters = why
    it.tier = tier
    it.score = score
    return it


def run():
    clients = load("clients.json")
    sector_map = email_render._client_sector_map(clients)
    passed = failed = 0

    cases = [
        ("Ofwat confirms £44.7m package for Welsh Water - BBC", "Body.", ["Welsh Water", "Ofwat"], "utilities", "Welsh Water+Ofwat -> utilities"),
        ("RWE secures Dogger Bank wind contract - Reuters", "Body.", ["RWE"], "energy", "RWE -> energy"),
        ("Ofgem orders OVO to pay £10.4m - Sharecast", "Body.", ["Ofgem"], "energy", "Ofgem -> energy"),
        ("BT CEO on strategy - Fortune", "Body.", ["BT Group"], "comms", "BT -> comms"),
        ("Informa lifts guidance - FT", "Body.", ["Informa"], "media", "Informa -> media"),
        ("DCC plc results strong - Reuters", "Body.", ["DCC plc"], "energy", "DCC plc -> energy"),
        ("Thames Water nationalisation debate - Guardian", "Body.", ["Thames Water"], "utilities", "Thames Water -> utilities"),
        ("National Grid £4.5bn transmission spend - Energy Live News", "Body.", ["National Grid"], "energy", "National Grid -> energy"),
        ("Cadent Gas mains upgrade - Utility Week", "Body.", ["Cadent Gas"], "energy", "Gas sector -> energy"),
        ("Vodafone Three merger approved - FT", "Body.", ["Vodafone"], "comms", "Vodafone -> comms"),
    ]

    for title, body, matched, expected_industry, note in cases:
        item = mk(title, body, matched)
        feed = cameron_feed.build_feed_items([item], date(2026, 6, 9), sector_map)
        row = feed[0]
        if row["industry"] == expected_industry:
            print(f"  ✓ {note}")
            passed += 1
        else:
            print(f"  ✗ FAIL {note}: expected {expected_industry}, got {row['industry']}")
            failed += 1

    # Schema + value checks on a representative item
    item = mk("Ofwat confirms £44.7m package for Welsh Water - BBC News",
              "**Ofwat** has confirmed a   package.", ["Ofwat"], "Matters to AMP8 work.")
    row = cameron_feed.build_feed_items([item], date(2026, 6, 9), sector_map)[0]

    checks = [
        (REQUIRED_KEYS.issubset(set(row.keys())), f"all required keys present ({set(row.keys())})"),
        (set(row.keys()) - REQUIRED_KEYS <= {"url"}, "no unexpected keys beyond optional url"),
        (row["industry"] in ALLOWED_INDUSTRIES, f"industry in allowed set"),
        (row["source"] == "BBC News", f"source from suffix ('{row['source']}')"),
        ("**" not in row["body"] and "  " not in row["body"], f"body markdown/whitespace stripped ('{row['body']}')"),
        (" - BBC News" not in row["headline"], f"headline suffix stripped ('{row['headline']}')"),
        (row["why_matters"] == "Matters to AMP8 work.", f"why_matters populated"),
        (row["period"] == "Week of 9 Jun 2026", f"period label ('{row['period']}')"),
        (isinstance(row["relevance"], int) and 0 <= row["relevance"] <= 100, f"relevance is 0-100 int ({row['relevance']})"),
        ("url" in row and row["url"].startswith("http"), f"url present and looks valid"),
        (len(row["date"]) == 10 and row["date"][4] == "-", f"date is ISO YYYY-MM-DD ('{row['date']}')"),
    ]
    for ok, desc in checks:
        if ok:
            print(f"  ✓ {desc}")
            passed += 1
        else:
            print(f"  ✗ FAIL {desc}")
            failed += 1

    # why_matters == "" when absent
    item2 = mk("RWE wind deal - Reuters", "Body.", ["RWE"])
    row2 = cameron_feed.build_feed_items([item2], date(2026, 6, 9), sector_map)[0]
    if row2["why_matters"] == "":
        print(f"  ✓ why_matters is empty string when absent")
        passed += 1
    else:
        print(f"  ✗ FAIL: why_matters should be '' when absent, got '{row2['why_matters']}'")
        failed += 1

    # All industries in any feed must be valid
    allcases = [mk(t, b, m) for t, b, m, _, _ in cases]
    feed_all = cameron_feed.build_feed_items(allcases, date(2026, 6, 9), sector_map)
    if all(f["industry"] in ALLOWED_INDUSTRIES for f in feed_all):
        print(f"  ✓ every item's industry is one of the four allowed values")
        passed += 1
    else:
        print(f"  ✗ FAIL: some industry value out of allowed set")
        failed += 1

    # Relevance banding: PRIORITY->75-100, RELEVANT->40-74, MENTIONED->10-39
    band_cases = [
        ("PRIORITY", 18, 75, 100, "PRIORITY high score"),
        ("PRIORITY", 10, 75, 100, "PRIORITY threshold"),
        ("RELEVANT", 9, 40, 74, "RELEVANT high"),
        ("RELEVANT", 5, 40, 74, "RELEVANT threshold"),
        ("MENTIONED", 4, 10, 39, "MENTIONED high"),
        ("MENTIONED", 2, 10, 39, "MENTIONED threshold"),
    ]
    for tier, score, lo, hi, note in band_cases:
        item = mk("Test - Reuters", "Body.", ["RWE"], tier=tier, score=score)
        rel = cameron_feed.relevance_score(item)
        if lo <= rel <= hi:
            print(f"  ✓ {note}: relevance {rel} in [{lo},{hi}]")
            passed += 1
        else:
            print(f"  ✗ FAIL {note}: relevance {rel} not in [{lo},{hi}]")
            failed += 1

    # Sort order: higher relevance first
    mixed = [
        mk("Noise - Sharecast", "x", ["United Utilities"], tier="MENTIONED", score=2),
        mk("Big deal - BBC", "x", ["Thames Water"], tier="PRIORITY", score=18),
        mk("Medium - FT", "x", ["BT Group"], tier="RELEVANT", score=7),
    ]
    feed_sorted = cameron_feed.build_feed_items(mixed, date(2026, 6, 9), sector_map)
    rels = [f["relevance"] for f in feed_sorted]
    if rels == sorted(rels, reverse=True):
        print(f"  ✓ feed pre-sorted by relevance descending {rels}")
        passed += 1
    else:
        print(f"  ✗ FAIL: feed not sorted by relevance: {rels}")
        failed += 1

    # url omitted when genuinely absent
    item_nourl = mk("No url item - BBC", "Body.", ["RWE"])
    item_nourl.url = ""
    row_nourl = cameron_feed.build_feed_items([item_nourl], date(2026, 6, 9), sector_map)[0]
    if "url" not in row_nourl:
        print(f"  ✓ url omitted when absent")
        passed += 1
    else:
        print(f"  ✗ FAIL: url should be omitted when empty, got '{row_nourl.get('url')}'")
        failed += 1

    print(f"\ncameron_feed: {passed} passed, {failed} failed")
    return failed == 0


if __name__ == "__main__":
    ok = run()
    print("\nAll cameron_feed tests passed." if ok else "\nSome tests failed.")
