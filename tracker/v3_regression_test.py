"""
v3 regression test — verify:
  - New MEC clients (BT, Vodafone, VMO2, Tesco Mobile, Lyca Mobile, Informa) match correctly
  - Disambiguation: bare "BT" needs context, "O2" the venue is rejected, "Informa" Spanish verb is rejected
  - Stock-noise via Google News title suffix is correctly dropped
  - PR/award puff is dropped
  - Score inflation fix: thin Google News summary doesn't trigger body bonus
  - Email renders into two super-sectors
"""

import json
from datetime import datetime, timezone, date, timedelta
from pathlib import Path

from tracker import sources, filters, scoring, dedupe, email_render

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


# ---- MEC client detection tests ----
DETECTION_CASES = [
    # BT Group
    ("BT Group reports Q4 results with Openreach fibre rollout ahead of plan",
     "BT plc has reported its fiscal Q4 results to the London Stock Exchange, with Openreach fibre rollout exceeding the year's target.",
     ["BT Group"], "BT Group via exact alias"),
    ("Openreach announces 1m additional FTTP premises in 2026 build",
     "Openreach has announced its 2026 FTTP rollout plan with an additional 1m premises.",
     ["BT Group"], "Openreach as exact alias for BT Group"),
    ("Bitcoin (BT) hits new high amid crypto rally",
     "Crypto markets rally as Bitcoin reaches new ATH.",
     [], "Bare 'BT' in Bitcoin context should NOT match"),
    ("BTec qualification reforms announced by Department for Education",
     "Department for Education announces BTec qualification reforms.",
     [], "BTec should NOT match BT Group"),

    # Vodafone
    ("Vodafone Group reports H1 results with Three UK merger progress",
     "Vodafone has reported its half-year results, noting progress on the Three UK merger and 5G rollout.",
     ["Vodafone"], "Vodafone exact alias"),
    ("Vodafone Idea India seeks government bailout",
     "Vodafone Idea, the Indian telecoms operator, has sought a government bailout.",
     [], "Vodafone Idea (India) should NOT match — UK focus only"),

    # VMO2 / O2
    ("Virgin Media O2 names new Chief Technology Officer",
     "Virgin Media O2 has appointed a new CTO to lead its network transformation programme.",
     ["Virgin Media O2"], "Virgin Media O2 exact alias"),
    ("Concert at the O2 Arena tonight features Taylor Swift",
     "Pop star plays the O2 Arena tonight.",
     [], "O2 Arena (venue) should NOT match"),
    ("Hospital records low blood O2 saturation in patients",
     "Medical study finds low O2 saturation correlates with poor outcomes.",
     [], "Blood O2 should NOT match"),
    ("O2 expands 5G coverage to 50 new towns in network upgrade",
     "O2, part of Virgin Media O2, has expanded its 5G coverage to 50 new towns.",
     ["Virgin Media O2"], "O2 + 5G context should match"),

    # Tesco Mobile
    ("Tesco Mobile launches new pay-as-you-go plan",
     "Tesco Mobile, the JV between Tesco and Virgin Media O2, has launched a new PAYG plan.",
     ["Tesco Mobile", "Virgin Media O2"], "Tesco Mobile + VMO2 both mentioned in body"),
    ("Tesco profits up 12% in latest results",
     "Tesco plc has reported a 12% profit increase.",
     [], "Bare 'Tesco' should NOT match — only 'Tesco Mobile' is in scope"),

    # Lyca Mobile
    ("Lyca Mobile faces tax investigation in HMRC probe",
     "Lyca Mobile is under HMRC investigation.",
     ["Lyca Mobile"], "Lyca Mobile exact alias"),

    # Informa
    ("Informa plc reports record revenue from B2B exhibition business",
     "Informa, the FTSE 100 events and publishing group, has reported record revenue.",
     ["Informa"], "Informa exact alias"),
    ("El Pais informa que el gobierno anuncia nuevas medidas",
     "Spanish news article about government announcements.",
     [], "Spanish 'informa' (3rd person verb) should NOT match"),
    ("Taylor & Francis launches new open access publishing model",
     "Taylor & Francis, an Informa subsidiary, has launched a new open access model.",
     ["Informa"], "Taylor & Francis as exact alias for Informa"),
]


def run_detection_tests():
    clients = load("clients.json")
    passed, failed = 0, 0
    for title, summary, expected, note in DETECTION_CASES:
        item = make(title, summary)
        result = filters.detect_clients([item], clients)
        actual = result[0].matched_clients if result else []
        if set(actual) == set(expected):
            print(f"  ✓ {note}: {actual or '(rejected)'}")
            passed += 1
        else:
            print(f"  ✗ FAIL {note}")
            print(f"      title:    {title[:80]}")
            print(f"      expected: {expected}")
            print(f"      actual:   {actual}")
            failed += 1
    print(f"\nMEC detection: {passed} passed, {failed} failed")
    return failed == 0


def run_stock_source_test():
    print("\n--- Stock-noise via Google News title suffix ---")
    clients = load("clients.json")
    exclusions = load("exclusions.json")
    # These all came via Google News (so url=news.google.com) but the real source is in the title suffix
    cases = [
        make("National Grid plc stock (GB00BDR05C01): CFO buys shares amid infrastructure push - AD HOC NEWS",
             "National Grid stock analysis.", "Google News", "news.google.com"),
        make("National Grid shares: a classic sleep-well stock for uncertain markets? - Fool UK",
             "National Grid as defensive stock.", "Google News", "news.google.com"),
        make("Yorkshire Water reports record sewage spills - The Guardian",
             "Yorkshire Water has reported record sewage spill incidents to the Environment Agency.",
             "Google News", "news.google.com"),  # this SHOULD survive
    ]
    detected = filters.detect_clients(cases, clients)
    after = filters.quality_gate(detected, exclusions)
    print(f"  Input: 3 articles (2 stock-noise via Google News, 1 real story) | Survived: {len(after)}")
    for it in after:
        print(f"  Survived: {it.title[:70]}")
    assert len(after) == 1, f"Expected 1 survivor, got {len(after)}"
    assert "Yorkshire Water" in after[0].title, "Wrong article survived"
    print("  ✓ Stock-noise dropped via title-suffix source match")


def run_score_inflation_test():
    print("\n--- Score inflation fix: thin summary should not get body bonus ---")
    clients = load("clients.json")
    topics = load("relevance_topics.json")
    credibility = load("source_credibility.json")
    exclusions = load("exclusions.json")

    # Thin summary (just repeats title) — v3 should NOT give +3 body bonus
    thin = make("Yorkshire Water postpone Skipton Road works",
                "Yorkshire Water postpone Skipton Road works",
                "Google News: Yorkshire Water", "news.google.com")
    # Substantive summary — should get +3 body bonus
    fat = make("Yorkshire Water postpone Skipton Road works",
               "Yorkshire Water has announced a postponement of major works on Skipton Road following residents' complaints. The roadworks were originally due to begin Monday but have now been delayed by three weeks while the company consults further with the local council and businesses.",
               "Local Paper", "ilkleychat.co.uk")

    detected = filters.detect_clients([thin, fat], clients)
    scoring.score_and_tier(detected, topics, credibility, exclusions)
    thin_score = detected[0].score
    fat_score = detected[1].score
    print(f"  Thin summary score: {thin_score}")
    print(f"  Fat summary score:  {fat_score}")
    assert fat_score > thin_score, f"Substantive summary should score higher (got thin={thin_score}, fat={fat_score})"
    assert thin_score == 5, f"Thin summary should score 5 (title only, no body bonus), got {thin_score}"
    print("  ✓ Score inflation fixed — body bonus only when summary is substantive")


def run_pr_drop_test():
    print("\n--- PR/award puff drop test ---")
    clients = load("clients.json")
    exclusions = load("exclusions.json")
    cases = [
        make("Scottish Water Specialists Win Major Industry Award - Scottish Water",
             "Scottish Water has won a major industry award.", "Google News", "news.google.com"),
        make("Welsh Water's ORAI system shortlisted for three categories at the British Data Awards 2026",
             "Welsh Water shortlisted for awards.", "Google News", "news.google.com"),
        make("Southern Water is embarking on its largest ever investment programme",
             "Southern Water PR release.", "Google News", "news.google.com"),
        make("Severn Trent seeks to grub out 70-foot hedge",
             "Severn Trent applies to remove a hedge.", "Google News", "news.google.com"),
    ]
    detected = filters.detect_clients(cases, clients)
    after = filters.quality_gate(detected, exclusions)
    print(f"  Input: 4 PR/award articles | Survived: {len(after)}")
    for it in after:
        print(f"  Survived: {it.title[:70]}")
    assert len(after) == 0, f"Expected all 4 to be dropped, but {len(after)} survived"
    print("  ✓ All PR/award puff dropped")


def run_email_render_test():
    print("\n--- Email render: two super-sectors ---")
    clients = load("clients.json")
    topics = load("relevance_topics.json")
    credibility = load("source_credibility.json")
    exclusions = load("exclusions.json")

    items = [
        make("Thames Water creditors prepare debt-for-equity swap",
             "Senior bondholders at Thames Water are preparing a debt-for-equity proposal. Ofwat is monitoring closely.",
             "Financial Times", "ft.com"),
        make("Vodafone Group reports H1 results with Three UK merger progress",
             "Vodafone has reported H1 results, noting Three UK merger progress.",
             "Reuters", "reuters.com"),
        make("BT Group announces Openreach FTTP target met early",
             "BT plc has reported that Openreach has hit its annual FTTP target ahead of schedule.",
             "FT", "ft.com"),
        make("South East Water boss resigns after major outages",
             "South East Water CEO David Hinton has resigned after major supply outages in Kent.",
             "BBC", "bbc.co.uk"),
    ]
    detected = filters.detect_clients(items, clients)
    scored = scoring.score_and_tier(detected, topics, credibility, exclusions)

    html = email_render.render_html(scored, date.today(), clients_cfg=clients)
    # Write to /tmp for visual inspection
    out_path = "/tmp/v3_preview.html"
    with open(out_path, "w") as f:
        f.write(html)

    assert "ENERGY &amp; UTILITIES" in html, "Missing Energy & Utilities super-sector header"
    assert "MEDIA, ENTERTAINMENT &amp; COMMUNICATIONS" in html, "Missing MEC super-sector header"
    assert "Vodafone" in html
    assert "BT Group" in html
    assert "Thames Water" in html
    print(f"  ✓ Both super-sectors rendered. Preview saved to {out_path}")


if __name__ == "__main__":
    ok = run_detection_tests()
    run_stock_source_test()
    run_score_inflation_test()
    run_pr_drop_test()
    run_email_render_test()
    print("\nAll v3 tests passed." if ok else "\nSome tests failed.")
