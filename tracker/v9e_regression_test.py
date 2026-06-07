"""
v9e regression test — two new clients added to the watchlist:
  - RWE (German energy major, big in UK offshore wind / generation)
  - DCC plc (FTSE 100 sales & energy-distribution group)

Both have short/ambiguous names so they're context-gated:
  - RWE: requires UK energy context; rejects unrelated noise
  - DCC: requires corporate/financial context; rejects Smart DCC (smart-meter body)
    and councils (Dundee/Derbyshire/Durham etc.)

(Northern Powergrid was requested too, but was already on the watchlist since the
original build — no change needed there.)
"""

import json
from datetime import datetime, timezone, timedelta
from pathlib import Path
from tracker import sources, filters

ROOT = Path(__file__).resolve().parent.parent
CONFIG = ROOT / "config"


def load(name):
    return json.load(open(CONFIG / name))


def mk(title):
    return sources.NewsItem(
        title=title, url=f"https://news.google.com/{abs(hash(title)) % 99999}",
        summary=title, source_name="Google News", source_domain="news.google.com",
        feed_tags=[], published_at=datetime.now(timezone.utc) - timedelta(days=2),
    )


# (title, expected_canonical_or_None, note)
CASES = [
    # RWE — should match on UK energy context
    ("RWE secures contract for Dogger Bank offshore wind expansion - Reuters", "RWE", "RWE offshore wind"),
    ("RWE Generation to convert Pembroke power station to hydrogen - Utility Week", "RWE", "RWE hydrogen conversion"),
    ("RWE wins UK capacity market auction for 500MW gas plant - Energy Live News", "RWE", "RWE capacity market"),
    # RWE — should NOT match without energy context
    ("RWE rugby club wins regional championship - Local Sport", None, "RWE rugby noise"),

    # DCC plc — should match on corporate/financial context
    ("DCC plc reports strong full year results driven by energy division - Reuters", "DCC plc", "DCC plc results"),
    ("DCC Energy acquires LPG distributor in £200m deal - Financial Times", "DCC plc", "DCC Energy M&A"),
    ("DCC plc lifts dividend as profit climbs - Investing.com", "DCC plc", "DCC plc dividend"),
    # DCC — should NOT match smart-meter body or councils
    ("Smart DCC rolls out new smart meter firmware update - ISPreview", None, "Smart DCC (smart meter)"),
    ("Data Communications Company reports smart metering milestone - Utility Week", None, "Data Communications Company"),
    ("Dundee City Council (DCC) approves new housing budget - The Courier", None, "Dundee City Council"),
    ("Derbyshire County Council DCC announces road repairs - Derby Telegraph", None, "Derbyshire County Council"),
]


def run():
    clients = load("clients.json")

    # Sanity: both new clients exist; Northern Powergrid already present
    passed = failed = 0
    for key in ("rwe", "dcc_plc", "northern_powergrid"):
        if key in clients:
            print(f"  ✓ client '{key}' present in config")
            passed += 1
        else:
            print(f"  ✗ FAIL: client '{key}' missing from config")
            failed += 1

    for title, expected, note in CASES:
        item = mk(title)
        detected = filters.detect_clients([item], clients)
        matched = detected[0].matched_clients if detected else []
        if expected is None:
            ok = (expected not in matched) and ("DCC plc" not in matched) and ("RWE" not in matched)
            # stricter: for negative cases, neither new client should appear
            ok = not any(c in ("RWE", "DCC plc") for c in matched)
        else:
            ok = expected in matched
        if ok:
            print(f"  ✓ {note}: {matched or '(no match)'}")
            passed += 1
        else:
            print(f"  ✗ FAIL {note}: expected {expected}, got {matched}")
            failed += 1

    print(f"\nv9e new-client disambiguation: {passed} passed, {failed} failed")
    return failed == 0


if __name__ == "__main__":
    ok = run()
    print("\nAll v9e tests passed." if ok else "\nSome tests failed.")
