"""
sources.py — Fetch and normalise RSS/Atom feeds from configured sources.

Returns a flat list of raw NewsItem dicts. Filtering / scoring happens downstream.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, asdict, field
from datetime import datetime, timezone
from typing import Any, Iterable
from urllib.parse import quote, urlparse

import feedparser
import requests
from dateutil import parser as dateparser

log = logging.getLogger(__name__)

# Some feeds (FT, Reuters) block default UA. Pretend to be a real browser.
USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/124.0.0.0 Safari/537.36"
)


@dataclass
class NewsItem:
    """Normalised representation of one news article."""

    title: str
    url: str
    summary: str
    source_name: str           # human-readable feed name
    source_domain: str         # the actual publisher domain (extracted from URL)
    feed_tags: list[str] = field(default_factory=list)
    published_at: datetime | None = None
    matched_clients: list[str] = field(default_factory=list)   # filled by filters.py
    matched_topics: list[str] = field(default_factory=list)    # filled by scoring.py
    score: int = 0                                              # filled by scoring.py
    tier: str = ""                                              # filled by scoring.py
    why_it_matters: str = ""                                    # filled by enrich.py

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d["published_at"] = self.published_at.isoformat() if self.published_at else ""
        return d


# ---------- HTTP helpers ----------

def _fetch_url(url: str, timeout: int) -> bytes | None:
    """Fetch raw bytes with a browser-like UA. Returns None on failure."""
    try:
        resp = requests.get(
            url,
            headers={"User-Agent": USER_AGENT, "Accept": "application/rss+xml, application/atom+xml, application/xml, text/xml, */*"},
            timeout=timeout,
            allow_redirects=True,
        )
        if resp.status_code != 200:
            log.warning("HTTP %d for %s", resp.status_code, url)
            return None
        return resp.content
    except requests.RequestException as exc:
        log.warning("Request failed for %s: %s", url, exc)
        return None


# ---------- Feed parsing ----------

def _parse_date(entry: dict) -> datetime | None:
    """Try multiple date fields. Always return UTC-aware datetime or None."""
    for key in ("published", "updated", "created", "pubDate"):
        raw = entry.get(key)
        if not raw:
            continue
        try:
            dt = dateparser.parse(raw)
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            return dt.astimezone(timezone.utc)
        except (ValueError, TypeError):
            continue
    # Try struct_time fields
    for key in ("published_parsed", "updated_parsed"):
        raw = entry.get(key)
        if raw:
            try:
                return datetime(*raw[:6], tzinfo=timezone.utc)
            except (TypeError, ValueError):
                continue
    return None


def _domain_of(url: str) -> str:
    try:
        netloc = urlparse(url).netloc.lower()
        if netloc.startswith("www."):
            netloc = netloc[4:]
        return netloc
    except Exception:
        return ""


def _extract_real_url(entry: dict) -> str:
    """
    Google News RSS wraps links in news.google.com redirects. Try to recover the source URL.
    Falls back to the wrapped URL.
    """
    link = entry.get("link", "")
    # Some entries have a list of links with rel="alternate"
    links = entry.get("links", [])
    for l in links:
        if l.get("rel") in (None, "alternate") and l.get("href"):
            link = l["href"]
            break
    return link


def parse_feed(content: bytes, feed_name: str, feed_tags: list[str]) -> list[NewsItem]:
    """Parse one feed's bytes into NewsItem list."""
    if not content:
        return []
    parsed = feedparser.parse(content)
    items: list[NewsItem] = []
    for entry in parsed.entries:
        url = _extract_real_url(entry)
        if not url:
            continue
        summary = entry.get("summary") or entry.get("description") or ""
        # Strip HTML tags from summary (cheap version)
        if "<" in summary:
            from bs4 import BeautifulSoup
            summary = BeautifulSoup(summary, "html.parser").get_text(" ", strip=True)
        item = NewsItem(
            title=entry.get("title", "").strip(),
            url=url,
            summary=summary.strip(),
            source_name=feed_name,
            source_domain=_domain_of(url),
            feed_tags=feed_tags,
            published_at=_parse_date(entry),
        )
        if item.title and item.url:
            items.append(item)
    return items


# ---------- Top-level fetch orchestration ----------

def build_google_news_feeds(clients: dict, template: str) -> list[dict]:
    """One Google News RSS feed per client canonical name."""
    feeds = []
    for client_id, cfg in clients.items():
        canonical = cfg["canonical"]
        url = template.replace("{client}", quote(canonical))
        feeds.append({
            "name": f"Google News: {canonical}",
            "url": url,
            "tags": ["aggregator", "google_news", f"client:{client_id}"],
        })
    return feeds


def fetch_all_feeds(feed_configs: Iterable[dict], fetch_cfg: dict, deadline_ts: float | None = None) -> list[NewsItem]:
    """
    Fetch all configured feeds sequentially. Respects per-feed retry policy and inter-request delay.
    If deadline_ts is set, stops fetching once we pass it (returns whatever we have).
    """
    timeout = fetch_cfg.get("timeout_seconds", 15)
    delay = fetch_cfg.get("delay_between_requests_seconds", 1.5)
    max_retries = fetch_cfg.get("max_retries_per_feed", 2)

    all_items: list[NewsItem] = []
    stats: dict[str, int] = {"feeds_attempted": 0, "feeds_failed": 0, "items_total": 0}

    for feed in feed_configs:
        if deadline_ts is not None and time.time() > deadline_ts:
            log.warning("Deadline reached during fetch; stopping with %d items so far", len(all_items))
            break

        name = feed.get("name", "unknown")
        url = feed["url"]
        tags = feed.get("tags", [])
        stats["feeds_attempted"] += 1

        content = None
        for attempt in range(max_retries + 1):
            content = _fetch_url(url, timeout)
            if content:
                break
            if attempt < max_retries:
                time.sleep(2 ** attempt)
        if not content:
            log.warning("Giving up on %s after %d attempts", name, max_retries + 1)
            stats["feeds_failed"] += 1
            time.sleep(delay)
            continue

        items = parse_feed(content, name, tags)
        all_items.extend(items)
        stats["items_total"] += len(items)
        log.info("%s: %d items", name, len(items))
        time.sleep(delay)

    log.info(
        "Fetch summary — feeds: %d attempted, %d failed; items: %d",
        stats["feeds_attempted"], stats["feeds_failed"], stats["items_total"],
    )
    return all_items
