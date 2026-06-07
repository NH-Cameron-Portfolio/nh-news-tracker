"""
v9c regression test — two fixes:

1. Consumer promo junk ("DEAL: ... LG 43" 4K TV or £125 bill credit", "£150 bonus
   before May 27") must be FILTERED OUT, not appear in the digest.

2. When the same story comes through Google News from both a high-credibility outlet
   (FT / The Times / Reuters) and a free tabloid, the high-credibility version must win
   the dedup. This was previously impossible because Google-News-wrapped items all have
   source_domain = news.google.com, hiding the real outlet. v9c reads the title suffix
   ("- Financial Times") to recover credibility.
"""

import json
from datetime import datetime, timezone, timedelta
from pathlib import Path
from tracker import sources, filters, scoring, dedupe

ROOT = Path(__file__).resolve().parent.parent
CONFIG = ROOT / "config"


def load(name):
    return json.load(open(CONFIG / name))


def mk(title, summary=None, days_ago=2):
    return sources.NewsItem(
        title=title,
        url=f"https://news.google.com/rss/{abs(hash(title)) % 99999}",
        summary=summary or title,
        source_name="Google News",
        source_domain="news.google.com",
        feed_tags=[],
        published_at=datetime.now(timezone.utc) - timedelta(days=days_ago),
    )


def run_promo_filter_test():
    print("--- Promo junk filtering ---")
    clients = load("clients.json")
    exclusions = load("exclusions.json")

    promo_titles = [
        'DEAL: Virgin Media offers new customers an LG 43" 4K TV or £125 bill credit on selected bundles - Virgin Media O2',
        "Virgin Media customers can enjoy £150 bonus before May 27 - Virgin Media O2",
        "Best broadband deals May 2026: top offers roundup - Some Site",
        "Get a free tablet when you switch to its broadband - Promo Site",
    ]
    passed = failed = 0
    for t in promo_titles:
        item = mk(t)
        detected = filters.detect_clients([item], clients)
        quality = filters.quality_gate(detected, exclusions) if detected else []
        if not quality:
            print(f"  ✓ FILTERED: {t[:60]}")
            passed += 1
        else:
            print(f"  ✗ FAIL (kept): {t[:60]}")
            failed += 1
    return passed, failed


def run_ft_dedup_test():
    print("\n--- FT/Times wins dedup over tabloid ---")
    clients = load("clients.json")
    topics = load("relevance_topics.json")
    credibility = load("source_credibility.json")
    exclusions = load("exclusions.json")

    passed = failed = 0

    # Near-identical headlines from different outlets -> dedup, high-cred should win
    cases = [
        ("The Sun", "Financial Times",
         "Thames Water draws up rescue plan as creditors circle - The Sun",
         "Thames Water draws up rescue plan as creditors circle - Financial Times"),
        ("Daily Mirror", "The Times",
         "Severn Trent warns on investment gap ahead of price review - Daily Mirror",
         "Severn Trent warns on investment gap ahead of price review - The Times"),
    ]
    for low_name, high_name, low_title, high_title in cases:
        low = mk(low_title, days_ago=1)   # tabloid published earlier
        high = mk(high_title, days_ago=2)
        items = [low, high]
        items = filters.detect_clients(items, clients)
        items = filters.quality_gate(items, exclusions)
        items = scoring.score_and_tier(items, topics, credibility, exclusions)
        deduped = dedupe.deduplicate(items, credibility)

        if len(deduped) == 1 and high_name.lower() in deduped[0].title.lower():
            print(f"  ✓ {high_name} beat {low_name}: '{deduped[0].title[:55]}'")
            passed += 1
        elif len(deduped) == 1:
            print(f"  ✗ FAIL: deduped to 1 but {low_name} won over {high_name}: '{deduped[0].title[:55]}'")
            failed += 1
        else:
            print(f"  ✗ FAIL: did not dedup ({len(deduped)} items) — fuzzy matcher didn't recognise same story")
            failed += 1

    # Verify suffix weights are correct
    checks = [
        ("X - Financial Times", 3),
        ("X - The Times", 3),
        ("X - Reuters", 3),
        ("X - The Sun", 0),
        ("X - Daily Mirror", 0),
        ("X - Utility Week", 3),
    ]
    for title, expected_w in checks:
        actual_w = dedupe._suffix_source_weight(title)
        if actual_w == expected_w:
            print(f"  ✓ suffix weight '{title.split(' - ')[1]}' = {actual_w}")
            passed += 1
        else:
            print(f"  ✗ FAIL: suffix weight '{title}' expected {expected_w}, got {actual_w}")
            failed += 1

    return passed, failed


def run():
    p1, f1 = run_promo_filter_test()
    p2, f2 = run_ft_dedup_test()
    total_p, total_f = p1 + p2, f1 + f2
    print(f"\nv9c: {total_p} passed, {total_f} failed")
    return total_f == 0


if __name__ == "__main__":
    ok = run()
    print("\nAll v9c tests passed." if ok else "\nSome tests failed.")
