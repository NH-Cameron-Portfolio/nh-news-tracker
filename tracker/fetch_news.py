"""
fetch_news.py — Orchestrator. Run as: python -m tracker.fetch_news

Pipeline:
  1. Load configs
  2. Build feed list (Google News per client + static feeds)
  3. Fetch all feeds (sources.py)
  4. Dedup (dedupe.py)
  5. Detect clients (filters.py)
  6. Quality gate (filters.py)
  7. Score and tier (scoring.py)
  8. Optional LLM enrichment (enrich.py)
  9. Write CSV, render HTML email, send
 10. Update last_run.json
"""

from __future__ import annotations

import csv
import json
import logging
import os
import sys
import time
from datetime import date, datetime, timezone
from pathlib import Path

from tracker import sources, dedupe, filters, scoring, enrich, email_render, cameron_feed

# ---------- Config paths ----------
ROOT = Path(__file__).resolve().parent.parent
CONFIG = ROOT / "config"
OUTPUT_DIR = ROOT / "output"
LAST_RUN = ROOT / "tracker" / "last_run.json"
CAMERON_JSON = ROOT / "data" / "cameron_news.json"   # microsite feed (committed each run)

# ---------- Time budget ----------
DEADLINE_MINUTES = 25  # leave 5 min for CSV + email + git commit before GitHub's 30 min timeout

# ---------- Logging ----------
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("nh_news")


def _load_json(name: str) -> dict:
    path = CONFIG / name
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def _save_last_run() -> None:
    LAST_RUN.write_text(
        json.dumps({"last_run": datetime.now(timezone.utc).isoformat()}, indent=2),
        encoding="utf-8",
    )


def _write_csv(items: list[sources.NewsItem], run_date: date, clients: dict) -> Path:
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    path = OUTPUT_DIR / f"nh_news_{run_date.isoformat()}.csv"
    fields = [
        "tier", "score", "client", "sector_guess", "source_name", "source_domain",
        "published_at", "title", "url", "summary", "topics", "why_it_matters",
    ]
    sector_map = email_render._client_sector_map(clients)
    # UTF-8 BOM so Excel opens it cleanly
    with open(path, "w", encoding="utf-8-sig", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=fields)
        writer.writeheader()
        for it in sorted(items, key=lambda i: (i.tier, -i.score)):
            writer.writerow({
                "tier": it.tier,
                "score": it.score,
                "client": " | ".join(it.matched_clients),
                "sector_guess": email_render._first_sector(it, sector_map),
                "source_name": it.source_name,
                "source_domain": it.source_domain,
                "published_at": it.published_at.isoformat() if it.published_at else "",
                "title": it.title,
                "url": it.url,
                "summary": it.summary[:1000],
                "topics": " | ".join(it.matched_topics),
                "why_it_matters": it.why_it_matters,
            })
    log.info("CSV written: %s (%d rows)", path, len(items))
    return path


def main() -> int:
    start = time.time()
    deadline_ts = start + DEADLINE_MINUTES * 60
    run_date = date.today()

    log.info("=== NH News Tracker — run %s ===", run_date.isoformat())

    # 1. Configs
    clients = _load_json("clients.json")
    feeds_cfg = _load_json("feeds.json")
    topics = _load_json("relevance_topics.json")
    exclusions = _load_json("exclusions.json")
    credibility = _load_json("source_credibility.json")
    log.info("Loaded %d clients, %d static feeds", len(clients), len(feeds_cfg["static_feeds"]))

    # 2. Build feed list
    feed_list: list[dict] = []
    if feeds_cfg["google_news_per_client"]["enabled"]:
        feed_list.extend(
            sources.build_google_news_feeds(clients, feeds_cfg["google_news_per_client"]["template"])
        )
    feed_list.extend(feeds_cfg["static_feeds"])
    log.info("Total feeds to fetch: %d", len(feed_list))

    # 3. Fetch
    raw_items = sources.fetch_all_feeds(feed_list, feeds_cfg["fetch"], deadline_ts=deadline_ts)
    log.info("Raw items: %d", len(raw_items))
    if not raw_items:
        log.warning("No items fetched. Sending empty digest anyway so the user knows the run happened.")

    # 4. Dedup
    deduped = dedupe.deduplicate(raw_items, credibility)

    # 5. Client detection
    with_clients = filters.detect_clients(deduped, clients)

    # 6. Quality gate
    quality = filters.quality_gate(with_clients, exclusions)

    # 7. Score and tier
    scored = scoring.score_and_tier(quality, topics, credibility, exclusions)

    # 7b. Second-pass story clustering — collapse multi-outlet coverage of the same event
    # (same primary client + same primary topic + same week)
    scored = dedupe.cluster_by_story(scored, credibility, window_days=5)

    # 8. Optional LLM enrichment
    enriched = enrich.enrich_items(scored)

    # 9. Output
    final_items = [it for it in enriched if it.tier != "DISCARDED"]
    log.info("Final items for digest: %d", len(final_items))

    csv_path = _write_csv(enriched, run_date, clients)   # CSV includes DISCARDED for transparency

    # Additional byproduct: write cameron_news.json for the Cameron Portfolio microsite.
    # This is fully isolated — wrapped so any failure here cannot affect the digest/email.
    # Uses the same final (non-DISCARDED) items the email shows.
    try:
        sector_map = email_render._client_sector_map(clients)
        cameron_feed.write_cameron_json(final_items, run_date, sector_map, CAMERON_JSON)
    except Exception as exc:
        log.exception("Failed to write cameron_news.json (non-fatal): %s", exc)

    include_mentioned = os.environ.get("INCLUDE_MENTIONED", "").lower() in ("1", "true", "yes")
    html = email_render.render_html(enriched, run_date, include_mentioned=include_mentioned, clients_cfg=clients)

    # Local HTML copy (handy for debugging / re-sending)
    html_path = OUTPUT_DIR / f"nh_news_{run_date.isoformat()}.html"
    html_path.write_text(html, encoding="utf-8")

    try:
        email_render.send_email(html, csv_path, run_date)
    except Exception as exc:
        log.exception("Failed to send email: %s", exc)
        # Don't fail the workflow over this — CSV is still committed for manual recovery
        # but raise non-zero exit code so it's visible in GitHub Actions
        _save_last_run()
        return 2

    _save_last_run()
    elapsed = time.time() - start
    log.info("Run complete in %.1fs", elapsed)
    return 0


if __name__ == "__main__":
    sys.exit(main())
