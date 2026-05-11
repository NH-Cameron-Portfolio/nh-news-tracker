"""
v5 regression test — verify the entertainment/sponsorship filter correctly drops
marketing fluff while preserving genuine telecoms news.
"""

import json
from datetime import datetime, timezone, timedelta
from pathlib import Path

from tracker import sources, filters

ROOT = Path(__file__).resolve().parent.parent
CONFIG = ROOT / "config"


def load(name):
    return json.load(open(CONFIG / name))


def make(title, summary, source_name="Google News", domain="news.google.com"):
    return sources.NewsItem(
        title=title,
        url=f"https://{domain}/article-{abs(hash(title)) % 1000000}",
        summary=summary or title,
        source_name=source_name,
        source_domain=domain,
        feed_tags=[],
        published_at=datetime.now(timezone.utc) - timedelta(days=1),
    )


# (title, summary, should_survive, note)
CASES = [
    # ---- Entertainment/sponsorship marketing — should ALL be dropped ----
    ("Priority presents: an O2 Evening with Trixie Mattel A FREE evening with renowned drag queen, YouTube sensation and DJ, Trixie Mattel, courtesy of Priority from O2 - Virgin Media O2",
     "Priority presents an O2 evening with Trixie Mattel.",
     False, "Trixie Mattel drag show / Priority by O2"),
    ("Virgin Media O2 customers get exclusive tickets to Glastonbury via Priority - Virgin Media O2",
     "Virgin Media O2 customers can access exclusive pre-sale tickets to Glastonbury through the Priority app.",
     False, "Exclusive tickets via Priority"),
    ("EE customers get behind the scenes access at Wembley - EE",
     "EE is offering customers behind the scenes access at Wembley Stadium as part of its sponsorship.",
     False, "EE sports sponsorship marketing"),
    ("Vodafone presents free concert tickets for loyal customers",
     "Vodafone is giving away concert tickets to loyal customers as part of its Vodafone Big Top promotion.",
     False, "Vodafone customer promo"),
    ("BT Sport presents live music night with celebrity DJs",
     "BT Sport presents a live music night featuring celebrity DJs.",
     False, "BT Sport entertainment promo"),

    # ---- Genuine news — should ALL survive ----
    ("Virgin Media O2 names new CTO to lead 5G network transformation - Reuters",
     "Virgin Media O2 has appointed a new Chief Technology Officer to lead its 5G network transformation programme.",
     True, "Real VMO2 leadership news"),
    ("Vodafone to take full control of UK mobile operator in £4.3bn deal - Financial Times",
     "Vodafone has announced it will take full control of its UK mobile joint venture with Three in a £4.3bn deal, completing the merger.",
     True, "Vodafone/Three £4.3bn deal"),
    ("BT Group reports H1 results with Openreach FTTP target met early - FT",
     "BT plc has reported its half-year results, noting Openreach has met its annual FTTP rollout target ahead of schedule.",
     True, "BT Group results"),
    ("Ofcom fines Vodafone £8m over billing failures - The Guardian",
     "Ofcom has fined Vodafone £8m following an investigation into billing failures affecting thousands of customers.",
     True, "Ofcom fine — material regulatory news"),
]


def run():
    clients = load("clients.json")
    exclusions = load("exclusions.json")

    passed, failed = 0, 0
    for title, summary, should_survive, note in CASES:
        item = make(title, summary)
        detected = filters.detect_clients([item], clients)
        quality = filters.quality_gate(detected, exclusions)
        actually_survived = len(quality) > 0

        if actually_survived == should_survive:
            status = "✓"
            passed += 1
        else:
            status = "✗ FAIL"
            failed += 1

        expected = "survive" if should_survive else "drop"
        actual = "survived" if actually_survived else "dropped"
        print(f"  {status} {note}: expected={expected}, actual={actual}")
        if status == "✗ FAIL":
            print(f"      title: {title[:80]}")

    print(f"\nEntertainment filter: {passed} passed, {failed} failed")
    return failed == 0


if __name__ == "__main__":
    ok = run()
    print("\nAll v5 tests passed." if ok else "\nSome tests failed.")
