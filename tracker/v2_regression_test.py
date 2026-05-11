"""
v2 regression test — replays the actual problem cases from the first live run
to verify the patches fix them.
"""

import json
from datetime import datetime, timezone, date, timedelta
from pathlib import Path

from tracker import sources, filters, scoring, dedupe, email_render

ROOT = Path(__file__).resolve().parent.parent
CONFIG = ROOT / "config"


def load(name):
    return json.load(open(CONFIG / name))


def make(title, summary, source_name="Test Source", domain="example.com", days_ago=1):
    return sources.NewsItem(
        title=title,
        url=f"https://{domain}/article-{abs(hash(title)) % 1000000}",
        summary=summary or title,
        source_name=source_name,
        source_domain=domain,
        feed_tags=[],
        published_at=datetime.now(timezone.utc) - timedelta(days=days_ago),
    )


# Problem cases from the live run
CASES = [
    # --- RECCo false positives — should ALL be rejected ---
    ("Savona edges Posillipo in derby; double-digit wins for Recco and Brescia",
     "Italian water polo coverage from Total Waterpolo.", "Total Waterpolo", "totalwaterpolo.com",
     [], "RECCo / Italian water polo"),
    ("Focaccia di Recco, the unmistakable flavor of Liguria, takes center stage",
     "Italian food festival.", "firstonline.info", "firstonline.info",
     [], "RECCo / focaccia"),
    ("St. John Vianney's Recco, Christian Brothers earn South, Non-Public boys golf titles",
     "High school golf coverage.", "NJ.com", "nj.com",
     [], "RECCo / US golf"),

    # --- National Grid foreign / generic false positives ---
    ("Why national grid collapses",
     "Nigerian power coverage from The Nation Newspaper Nigeria.", "The Nation Newspaper", "thenationonlineng.net",
     [], "National Grid / Nigerian grid"),
    ("Tegbe Denies Vowing to Fix National Grid in Three Months",
     "Nigerian political coverage about politician Tegbe in Osun state.", "OsunDefender", "osundefender.org",
     [], "National Grid / Nigerian politics"),
    ("Kagera to be connected to National Grid, contractor given 24 months",
     "Tanzanian electrification project.", "The Citizen Tanzania", "thecitizen.co.tz",
     [], "National Grid / Tanzania"),
    ("The British National Grid - now in Python and R",
     "Ordnance Survey announces the British National Grid coordinate reference system is now supported in Python and R libraries.",
     "Ordnance Survey", "ordnancesurvey.co.uk",
     [], "National Grid / OS coordinate system"),

    # --- Yorkshire Water vs Water Park ---
    ("Former RAF Search And Rescue Helicopters Transformed Into Glamping Pods At North Yorkshire Water Park",
     "Leisure park news.", "This is the Coast", "thisisthecoast.co.uk",
     [], "Yorkshire Water / Water Park"),
    ("North Yorkshire Water Park looks to diversify with new padel courts",
     "Padel court announcement.", "Teesside Live", "teessidelive.co.uk",
     [], "Yorkshire Water / Padel"),

    # --- Energy UK / Plastic Energy ---
    ("Plastic Energy UK companies enter administration amid cash flow crunch",
     "Plastic Energy, a UK plastics-to-fuel company, has entered administration.",
     "Plastics News", "plasticsnews.com",
     [], "Energy UK / Plastic Energy"),

    # --- These SHOULD still match (sanity checks) ---
    ("South East Water boss resigns after major outages",
     "Chief Executive of South East Water David Hinton has resigned.",
     "BBC", "bbc.co.uk",
     ["South East Water"], "Real story should still match"),
    ("NESO appoints new CDO to lead digital transformation programme",
     "The National Energy System Operator has appointed a new CDO.",
     "Utility Week", "utilityweek.co.uk",
     ["National Grid"], "NESO as exact alias should still match"),
    ("National Grid plc reports H1 results", 
     "National Grid plc has reported its half-year results to the London Stock Exchange.",
     "FT", "ft.com",
     ["National Grid"], "National Grid plc (exact alias) should still match"),
]


def run():
    clients = load("clients.json")
    exclusions = load("exclusions.json")
    topics = load("relevance_topics.json")
    credibility = load("source_credibility.json")

    passed, failed = 0, 0
    for title, summary, src, dom, expected, note in CASES:
        item = make(title, summary, src, dom)
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

    print(f"\nDetection: {passed} passed, {failed} failed\n")

    # ---- Stock-noise quality gate test ----
    print("--- Stock-noise quality gate ---")
    stock_cases = [
        make("United Utilities Group stock (GB00B39J2M42): Shares up 16% YTD amid hold rating",
             "Stock analysis.", "AD HOC NEWS", "adhocnews.de"),
        make("National Grid plc stock (GB00BDR05C01): CFO buys shares amid infrastructure push",
             "Stock analysis.", "AD HOC NEWS", "adhocnews.de"),
        make("Severn Trent (LON:SVT) Shares Pass Above Two Hundred Day Moving Average",
             "Stock analysis.", "MarketBeat", "marketbeat.com"),
        make("LONDON BROKER RATINGS: Citi cuts Severn Trent and United Utilities",
             "Citi has lowered its price targets on Severn Trent and United Utilities citing PR24 headwinds.",
             "London South East", "londonsoutheast.co.uk"),
        make("National Grid Stock Drops Before Results After JPMorgan Cuts Target",
             "Stock analysis.", "TechStock", "techstock2.com"),
    ]
    detected = filters.detect_clients(stock_cases, clients)
    after = filters.quality_gate(detected, exclusions)
    print(f"  Input: {len(stock_cases)} stock-y articles | After quality gate: {len(after)}")
    for it in after:
        print(f"  Surviving: {it.title[:80]} ({it.source_domain})")
    print()

    # ---- Local-noise drop test ----
    print("--- Local-noise drop test ---")
    local_cases = [
        make("Reopening of road in Reading delayed as Thames Water works continue",
             "Local roadworks update.", "Reading Today", "readingtoday.example"),
        make("Town centre route closed due to South East Water roadworks",
             "Local roadworks.", "Kent Online", "kentonline.example"),
        make("Severn Trent seeks to grub out 70-foot hedge",
             "Severn Trent hedge removal application.", "Punchline Gloucester", "punchline-gloucester.example"),
        make("Rural road in Llannefydd shut for three weeks for Welsh Water works",
             "Roadworks update.", "Denbighshire Free Press", "denbighshirefreepress.example"),
    ]
    detected = filters.detect_clients(local_cases, clients)
    after = filters.quality_gate(detected, exclusions)
    print(f"  Input: {len(local_cases)} local-noise articles | After quality gate: {len(after)}")
    for it in after:
        print(f"  Surviving (should be 0): {it.title[:80]}")
    print()

    # ---- Story clustering test ----
    print("--- Story clustering test ---")
    se_water_cluster = [
        make(f"South East Water CEO resigns - {src}",
             f"Coverage of David Hinton resignation from {src}.",
             src, f"{src.lower().replace(' ', '')}.com")
        for src in ["BBC", "Guardian", "FT", "Times", "Telegraph", "ITV News", "Sky News",
                    "GB News", "Independent", "Bloomberg", "Mirror", "Utility Week",
                    "ENDS Report", "Kent Online", "Water Magazine"]
    ]
    detected = filters.detect_clients(se_water_cluster, clients)
    quality = filters.quality_gate(detected, exclusions)
    scored = scoring.score_and_tier(quality, topics, credibility, exclusions)
    deduped = dedupe.deduplicate(scored, credibility)
    clustered = dedupe.cluster_by_story(deduped, credibility)
    print(f"  Input: 15 articles of same story | After dedup + clustering: {len(clustered)}")
    for it in clustered:
        print(f"  -> {it.title[:80]} (score {it.score}, why={it.why_it_matters!r})")


if __name__ == "__main__":
    run()
