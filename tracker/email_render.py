"""
email_render.py — Build the HTML digest and send via Gmail SMTP.

Mirrors the look of the tender tracker output: stats bar at top, then tiered sections.
"""

from __future__ import annotations

import logging
import os
import smtplib
import ssl
from collections import Counter
from datetime import date
from email.mime.base import MIMEBase
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from email import encoders
from html import escape
from pathlib import Path

from tracker.sources import NewsItem

log = logging.getLogger(__name__)


# ---------- HTML rendering ----------

CSS = """
<style>
  body { font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Arial, sans-serif; color: #222; line-height: 1.4; max-width: 760px; margin: 0 auto; padding: 16px; }
  h1 { font-size: 20px; margin: 0 0 4px; color: #132E53; }
  h2 { font-size: 16px; margin: 24px 0 8px; padding-bottom: 4px; border-bottom: 2px solid #132E53; color: #132E53; }
  .stats { background: #f4f6f9; border: 1px solid #d8dde5; padding: 10px 14px; border-radius: 6px; margin: 12px 0 18px; font-size: 13px; }
  .stats span { display: inline-block; margin-right: 14px; }
  .stats b { color: #132E53; }
  .item { padding: 10px 0; border-bottom: 1px solid #eee; }
  .item:last-child { border-bottom: none; }
  .title { font-weight: 600; font-size: 14px; }
  .title a { color: #132E53; text-decoration: none; }
  .title a:hover { text-decoration: underline; }
  .meta { color: #666; font-size: 12px; margin: 2px 0 4px; }
  .summary { font-size: 13px; color: #333; margin: 4px 0; }
  .why { font-size: 12px; color: #039FB8; font-style: italic; margin: 4px 0; }
  .tags { font-size: 11px; color: #666; margin-top: 4px; }
  .tags .client { background: #e6eef7; padding: 1px 6px; border-radius: 3px; margin-right: 4px; color: #132E53; }
  .tags .topic { background: #f0f0f0; padding: 1px 6px; border-radius: 3px; margin-right: 4px; }
  .footer { font-size: 11px; color: #888; margin-top: 24px; padding-top: 12px; border-top: 1px solid #eee; }
</style>
"""


def _fmt_date(item: NewsItem) -> str:
    if not item.published_at:
        return ""
    return item.published_at.strftime("%-d %b") if hasattr(item.published_at, "strftime") else ""


def _render_item(item: NewsItem, condensed: bool = False) -> str:
    title_html = f'<a href="{escape(item.url)}">{escape(item.title)}</a>'
    parts = [f'<div class="title">{title_html}</div>']
    meta = f'{escape(item.source_name)} · {_fmt_date(item)} · score {item.score}'
    parts.append(f'<div class="meta">{meta}</div>')

    if not condensed:
        snippet = item.summary[:280]
        if len(item.summary) > 280:
            snippet += "…"
        parts.append(f'<div class="summary">{escape(snippet)}</div>')
        if item.why_it_matters:
            parts.append(f'<div class="why">Why it matters: {escape(item.why_it_matters)}</div>')

    tag_html = ""
    for c in item.matched_clients:
        tag_html += f'<span class="client">{escape(c)}</span>'
    for t in item.matched_topics[:4]:
        tag_html += f'<span class="topic">{escape(t.replace("_", " "))}</span>'
    if tag_html:
        parts.append(f'<div class="tags">{tag_html}</div>')

    return f'<div class="item">{"".join(parts)}</div>'


def _render_section(name: str, items: list[NewsItem], condensed: bool = False) -> str:
    if not items:
        return ""
    rows = "\n".join(_render_item(it, condensed=condensed) for it in items)
    return f'<h2>{escape(name)}</h2>\n{rows}'


def render_html(items: list[NewsItem], run_date: date, include_mentioned: bool = False) -> str:
    by_tier: dict[str, list[NewsItem]] = {"PRIORITY": [], "RELEVANT": [], "MENTIONED": []}
    for it in items:
        if it.tier in by_tier:
            by_tier[it.tier].append(it)

    # Sort each tier by score desc, then by date desc
    for tier in by_tier:
        by_tier[tier].sort(
            key=lambda it: (it.score, it.published_at.timestamp() if it.published_at else 0),
            reverse=True,
        )

    sector_counts = Counter(c for it in items if it.tier != "DISCARDED" for c in [_first_sector(it)])
    sector_line = " · ".join(f"{s}: <b>{sector_counts[s]}</b>" for s in ("Water", "Electricity", "Gas", "Regulator", "Industry Body") if sector_counts[s])

    stats = (
        f'<div class="stats">'
        f'<span>Total: <b>{sum(len(v) for k, v in by_tier.items() if k != "DISCARDED" and (k != "MENTIONED" or include_mentioned))}</b></span>'
        f'<span>⭐ Priority: <b>{len(by_tier["PRIORITY"])}</b></span>'
        f'<span>Relevant: <b>{len(by_tier["RELEVANT"])}</b></span>'
        + (f'<span>Mentioned: <b>{len(by_tier["MENTIONED"])}</b></span>' if include_mentioned else "")
        + f'<br><span>{sector_line}</span></div>'
    )

    sections = [
        _render_section("⭐ PRIORITY", by_tier["PRIORITY"], condensed=False),
        _render_section("RELEVANT", by_tier["RELEVANT"], condensed=True),
    ]
    if include_mentioned:
        sections.append(_render_section("MENTIONED", by_tier["MENTIONED"], condensed=True))

    body = "\n".join(s for s in sections if s) or "<p>No relevant articles this week.</p>"

    html = f"""<!doctype html>
<html><head><meta charset="utf-8">{CSS}</head><body>
<h1>NH Client News Digest — Week of {run_date.strftime("%-d %B %Y")}</h1>
{stats}
{body}
<div class="footer">Automated digest. {len(items)} items considered. Filters: client name detection · quality gate · relevance scoring{" · LLM enrichment" if any(i.why_it_matters for i in items) else ""}.</div>
</body></html>"""
    return html


def _first_sector(item: NewsItem) -> str:
    """Look up sector from the matched_clients list via a known mapping. Returns the first sector found."""
    # Heuristic: use the first matched client and lean on item.feed_tags or fallback
    # We don't have direct sector here; caller pre-tagged. For simplicity, infer from client name.
    if not item.matched_clients:
        return "Unknown"
    first = item.matched_clients[0]
    # Quick sector lookup based on canonical names — defensive in case clients.json isn't injected
    water_set = {"Thames Water", "Severn Trent", "United Utilities", "Anglian Water", "Yorkshire Water",
                 "Northumbrian Water", "South West Water", "Wessex Water", "Southern Water",
                 "Affinity Water", "South East Water", "SES Water", "Portsmouth Water",
                 "Bristol Water", "South Staffs Water", "Welsh Water", "Scottish Water", "NI Water"}
    elec_set = {"National Grid", "UK Power Networks", "SP Energy Networks", "SSEN",
                "Western Power Distribution", "Electricity North West", "Northern Powergrid"}
    gas_set = {"Cadent Gas", "SGN", "Northern Gas Networks", "Wales & West Utilities", "National Gas Transmission"}
    reg_set = {"Ofwat", "Ofgem", "CCW"}
    if first in water_set: return "Water"
    if first in elec_set:  return "Electricity"
    if first in gas_set:   return "Gas"
    if first in reg_set:   return "Regulator"
    return "Industry Body"


# ---------- SMTP send ----------

def send_email(html_body: str, csv_path: Path | None, run_date: date) -> None:
    host = os.environ["SMTP_HOST"]
    port = int(os.environ["SMTP_PORT"])
    user = os.environ["SMTP_USER"]
    password = os.environ["SMTP_PASS"]
    recipients = [r.strip() for r in os.environ["EMAIL_RECIPIENTS"].split(",") if r.strip()]
    if not recipients:
        raise RuntimeError("EMAIL_RECIPIENTS is empty")

    msg = MIMEMultipart("mixed")
    msg["Subject"] = f"NH Client News Digest — {run_date.strftime('%-d %b %Y')}"
    msg["From"] = user
    msg["To"] = ", ".join(recipients)

    msg.attach(MIMEText(html_body, "html", "utf-8"))

    if csv_path and csv_path.exists():
        with open(csv_path, "rb") as f:
            part = MIMEBase("application", "octet-stream")
            part.set_payload(f.read())
        encoders.encode_base64(part)
        part.add_header("Content-Disposition", f'attachment; filename="{csv_path.name}"')
        msg.attach(part)

    log.info("Sending email to %d recipients via %s:%d", len(recipients), host, port)
    context = ssl.create_default_context()
    with smtplib.SMTP(host, port) as server:
        server.starttls(context=context)
        server.login(user, password)
        server.sendmail(user, recipients, msg.as_string())
    log.info("Email sent.")
