"""
v6 regression test — verify the high-credibility-source safety net.

Articles from FT, Reuters, BBC, Bloomberg, Times, Telegraph, Guardian, and Economist
should match a tracked client even if the article doesn't contain the usual context
keywords. Rationale: these outlets don't write filler about regulated utilities, so
if they're writing about a client by name it's almost always material news.

This protects against false negatives like "BT is back" (the original miss that
prompted v6).
"""

import json
from datetime import datetime, timezone, timedelta
from pathlib import Path

from tracker import sources, filters

ROOT = Path(__file__).resolve().parent.parent
CONFIG = ROOT / "config"


def load(name):
    return json.load(open(CONFIG / name))


def make(title, summary, source_name="Test", domain="example.com", days_ago=1):
    return sources.NewsItem(
        title=title,
        url=f"https://{domain}/article-{abs(hash(title)) % 1000000}",
        summary=summary or title,
        source_name=source_name,
        source_domain=domain,
        feed_tags=[],
        published_at=datetime.now(timezone.utc) - timedelta(days=days_ago),
    )


CASES = [
    # ---- High-credibility safety net should ENABLE these matches ----
    ("BT is back — Britain's telecoms giant has turned itself around - Financial Times",
     "BT is back as a serious turnaround story.",
     "Google News", "news.google.com",
     ["BT Group"],
     "BT via FT Google News (the original miss)"),
    ("BT is back — the comeback story",
     "BT plc has staged a remarkable turnaround under its new chief executive.",
     "Financial Times", "ft.com",
     ["BT Group"],
     "BT via direct ft.com feed"),
    ("Vodafone faces tough quarter as competition heats up - Reuters",
     "Vodafone shares fell.",
     "Google News", "news.google.com",
     ["Vodafone"],
     "Vodafone via Reuters with thin summary"),
    ("Severn Trent considers strategic options - Bloomberg",
     "Brief article.",
     "Google News", "news.google.com",
     ["Severn Trent"],
     "Severn Trent via Bloomberg"),
    ("Informa share price tumbles on guidance cut - The Times",
     "Brief.",
     "Google News", "news.google.com",
     ["Informa"],
     "Informa via The Times"),

    # ---- Negative context should STILL block, even on high-cred sources ----
    ("Bitcoin (BT) hits all-time high - Financial Times",
     "Cryptocurrency markets rally.",
     "Google News", "news.google.com",
     [],
     "Bitcoin from FT — negative_context still blocks"),
    ("Vodafone Idea India seeks bailout - Reuters",
     "Indian telecoms operator seeks government aid.",
     "Google News", "news.google.com",
     [],
     "Vodafone India from Reuters — negative_context still blocks"),
    ("SSE Thermal commissions new gas plant - The Guardian",
     "SSE Thermal has commissioned a new generation asset.",
     "Google News", "news.google.com",
     [],
     "SSE Thermal from Guardian — should NOT match SSEN"),

    # ---- Low-credibility sources still need context (regression) ----
    ("BT is back on the rugby field",
     "Local rugby club BT has returned to competition.",
     "Local rugby blog", "rugbynews.example",
     [],
     "Low-cred rugby story — bare 'BT' should NOT match"),
    ("BT wins community fundraising award",
     "BT raised £500 for local charity.",
     "Local paper", "localpaper.example",
     [],
     "Low-cred + no context — bare 'BT' should NOT match"),

    # ---- Low-credibility source WITH context should still match (regression) ----
    ("BT signs new fibre deal with rural councils",
     "BT, the British telecoms operator, has signed a deal to roll out fibre broadband in rural areas.",
     "Cable news site", "cablenews.example",
     ["BT Group"],
     "Low-cred but with 'fibre/broadband/telecoms' context — should match"),
]


def run():
    clients = load("clients.json")
    passed, failed = 0, 0
    for title, summary, source_name, domain, expected, note in CASES:
        item = make(title, summary, source_name, domain)
        result = filters.detect_clients([item], clients)
        actual = result[0].matched_clients if result else []
        if set(actual) == set(expected):
            label = str(actual) if actual else "(rejected)"
            print(f"  ✓ {note}: {label}")
            passed += 1
        else:
            print(f"  ✗ FAIL {note}")
            print(f"      title:    {title[:80]}")
            print(f"      expected: {expected}")
            print(f"      actual:   {actual}")
            failed += 1
    print(f"\nHigh-credibility safety net: {passed} passed, {failed} failed")
    return failed == 0


if __name__ == "__main__":
    ok = run()
    print("\nAll v6 tests passed." if ok else "\nSome tests failed.")
