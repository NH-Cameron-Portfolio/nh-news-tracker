"""
email_render.py — Build the HTML digest and send via Gmail SMTP.

v3 changes:
  - Split into super-sectors: "Energy & Utilities" and "Media, Entertainment & Communications"
  - Within each super-sector, show PRIORITY then RELEVANT tiers
  - Inline styles (carried over from v2)
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

# ---------- Sector groupings ----------

ENERGY_UTILITIES_SECTORS = {"Water", "Electricity", "Gas", "Regulator", "Industry Body"}
MEC_SECTORS = {"Telecoms", "Media"}

SUPER_SECTOR_DEFS = [
    ("Energy & Utilities", ENERGY_UTILITIES_SECTORS),
    ("Media, Entertainment & Communications", MEC_SECTORS),
]


# ---------- Inline styles ----------

S = {
    "body":         "font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',Arial,sans-serif;color:#222;line-height:1.4;max-width:760px;margin:0 auto;padding:16px;",
    "h1":           "font-size:20px;margin:0 0 4px;color:#132E53;",
    "h2_super":     "font-size:18px;margin:28px 0 4px;padding:8px 0 6px;border-top:3px solid #132E53;border-bottom:1px solid #132E53;color:#132E53;letter-spacing:0.5px;",
    "h3_tier":      "font-size:15px;margin:18px 0 6px;padding-bottom:3px;border-bottom:1px solid #d8dde5;color:#132E53;",
    "stats":        "background:#f4f6f9;border:1px solid #d8dde5;padding:10px 14px;border-radius:6px;margin:12px 0 18px;font-size:13px;",
    "stat":         "display:inline-block;margin-right:14px;",
    "item":         "padding:10px 0;border-bottom:1px solid #eee;",
    "title":        "font-weight:600;font-size:14px;margin-bottom:2px;",
    "link":         "color:#132E53;text-decoration:none;",
    "meta":         "color:#666;font-size:12px;margin:2px 0 4px;",
    "summary":      "font-size:13px;color:#333;margin:4px 0;",
    "why":          "font-size:12px;color:#039FB8;font-style:italic;margin:4px 0;",
    "tags":         "font-size:11px;color:#666;margin-top:4px;",
    "client":       "display:inline-block;background:#e6eef7;padding:1px 6px;border-radius:3px;margin:0 4px 0 0;color:#132E53;",
    "topic":        "display:inline-block;background:#f0f0f0;padding:1px 6px;border-radius:3px;margin:0 4px 0 0;",
    "footer":       "font-size:11px;color:#888;margin-top:24px;padding-top:12px;border-top:1px solid #eee;",
    "empty":        "font-size:13px;color:#888;font-style:italic;margin:10px 0;",
}


def _fmt_date(item: NewsItem) -> str:
    if not item.published_at:
        return ""
    try:
        return item.published_at.strftime("%-d %b")
    except ValueError:
        return item.published_at.strftime("%d %b")


# ---------- Sector lookup ----------

def _first_sector(item: NewsItem, clients_cfg: dict | None = None) -> str:
    if not item.matched_clients:
        return "Unknown"
    first = item.matched_clients[0]
    if clients_cfg:
        for cfg in clients_cfg.values():
            if cfg["canonical"] == first:
                return cfg["sector"]
    # Fallback hardcoded mapping (in case clients_cfg isn't passed)
    water_set = {"Thames Water", "Severn Trent", "United Utilities", "Anglian Water", "Yorkshire Water",
                 "Northumbrian Water", "South West Water", "Wessex Water", "Southern Water",
                 "Affinity Water", "South East Water", "SES Water", "Portsmouth Water",
                 "Bristol Water", "South Staffs Water", "Welsh Water", "Scottish Water", "NI Water"}
    elec_set = {"National Grid", "UK Power Networks", "SP Energy Networks", "SSEN",
                "Western Power Distribution", "Electricity North West", "Northern Powergrid"}
    gas_set = {"Cadent Gas", "SGN", "Northern Gas Networks", "Wales & West Utilities", "National Gas Transmission"}
    reg_set = {"Ofwat", "Ofgem", "CCW"}
    telecoms_set = {"BT Group", "Vodafone", "Virgin Media O2", "Tesco Mobile", "Lyca Mobile"}
    media_set = {"Informa"}
    if first in water_set:    return "Water"
    if first in elec_set:     return "Electricity"
    if first in gas_set:      return "Gas"
    if first in reg_set:      return "Regulator"
    if first in telecoms_set: return "Telecoms"
    if first in media_set:    return "Media"
    return "Industry Body"


def _super_sector(item: NewsItem, clients_cfg: dict | None = None) -> str:
    sector = _first_sector(item, clients_cfg)
    if sector in ENERGY_UTILITIES_SECTORS:
        return "Energy & Utilities"
    if sector in MEC_SECTORS:
        return "Media, Entertainment & Communications"
    return "Other"


# ---------- Rendering ----------

def _render_item(item: NewsItem, condensed: bool = False) -> str:
    title_html = f'<a href="{escape(item.url)}" style="{S["link"]}">{escape(item.title)}</a>'
    parts = [f'<div style="{S["title"]}">{title_html}</div>']
    meta = f'{escape(item.source_name)} · {_fmt_date(item)} · score {item.score}'
    parts.append(f'<div style="{S["meta"]}">{meta}</div>')

    if not condensed:
        snippet = item.summary[:280]
        if len(item.summary) > 280:
            snippet += "…"
        # Only show the summary if it's substantively different from the title
        if snippet.lower().strip() != item.title.lower().strip():
            parts.append(f'<div style="{S["summary"]}">{escape(snippet)}</div>')
        if item.why_it_matters:
            parts.append(f'<div style="{S["why"]}">Why it matters: {escape(item.why_it_matters)}</div>')

    tag_html = ""
    for c in item.matched_clients:
        tag_html += f'<span style="{S["client"]}">{escape(c)}</span>'
    for t in item.matched_topics[:4]:
        tag_html += f'<span style="{S["topic"]}">{escape(t.replace("_", " "))}</span>'
    if tag_html:
        parts.append(f'<div style="{S["tags"]}">{tag_html}</div>')

    return f'<div style="{S["item"]}">{"".join(parts)}</div>'


def _render_tier_in_super_sector(tier_label: str, items: list[NewsItem], condensed: bool, cap: int | None) -> str:
    if not items:
        return ""
    shown = items[:cap] if cap else items
    rows = "\n".join(_render_item(it, condensed=condensed) for it in shown)
    overflow = ""
    if cap and len(items) > cap:
        overflow = f'<div style="{S["meta"]}">+{len(items)-cap} more in this tier (see attached CSV).</div>'
    return f'<h3 style="{S["h3_tier"]}">{escape(tier_label)}</h3>\n{rows}\n{overflow}'


def render_html(items: list[NewsItem], run_date: date, include_mentioned: bool = False, clients_cfg: dict | None = None) -> str:
    # Bucket items: super_sector -> tier -> [items]
    buckets: dict[str, dict[str, list[NewsItem]]] = {}
    for it in items:
        if it.tier == "DISCARDED":
            continue
        if it.tier == "MENTIONED" and not include_mentioned:
            continue
        ss = _super_sector(it, clients_cfg)
        buckets.setdefault(ss, {"PRIORITY": [], "RELEVANT": [], "MENTIONED": []})
        if it.tier in buckets[ss]:
            buckets[ss][it.tier].append(it)

    # Sort each tier by score desc, then date desc
    for ss in buckets.values():
        for tier in ss:
            ss[tier].sort(
                key=lambda it: (it.score, it.published_at.timestamp() if it.published_at else 0),
                reverse=True,
            )

    # Build stats bar
    total = sum(len(items_) for ss in buckets.values() for items_ in ss.values())
    priority_total = sum(len(ss["PRIORITY"]) for ss in buckets.values())
    relevant_total = sum(len(ss["RELEVANT"]) for ss in buckets.values())
    ss_counts = {ss: sum(len(v) for v in tiers.values()) for ss, tiers in buckets.items()}

    stats_bits = [
        f'<span style="{S["stat"]}">Total: <b>{total}</b></span>',
        f'<span style="{S["stat"]}">⭐ Priority: <b>{priority_total}</b></span>',
        f'<span style="{S["stat"]}">Relevant: <b>{relevant_total}</b></span>',
    ]
    if include_mentioned:
        mentioned_total = sum(len(ss["MENTIONED"]) for ss in buckets.values())
        stats_bits.append(f'<span style="{S["stat"]}">Mentioned: <b>{mentioned_total}</b></span>')

    sector_bits = []
    for ss_name, count in ss_counts.items():
        if count > 0:
            sector_bits.append(f'<span style="{S["stat"]}">{ss_name}: <b>{count}</b></span>')

    stats = (
        f'<div style="{S["stats"]}">'
        + "".join(stats_bits)
        + (f'<br>' + "".join(sector_bits) if sector_bits else "")
        + '</div>'
    )

    # Build super-sector sections (in defined order)
    section_html = []
    for ss_name, _ in SUPER_SECTOR_DEFS:
        if ss_name not in buckets:
            continue
        ss = buckets[ss_name]
        # Skip empty super-sectors
        if not any(ss[t] for t in ("PRIORITY", "RELEVANT", "MENTIONED")):
            continue

        section_html.append(f'<h2 style="{S["h2_super"]}">{escape(ss_name.upper())}</h2>')
        priority_html = _render_tier_in_super_sector("⭐ PRIORITY", ss["PRIORITY"], condensed=False, cap=20)
        relevant_html = _render_tier_in_super_sector("RELEVANT", ss["RELEVANT"], condensed=True, cap=40)

        if not priority_html and not relevant_html:
            section_html.append(f'<div style="{S["empty"]}">No material items this week.</div>')
            continue

        if priority_html:
            section_html.append(priority_html)
        if relevant_html:
            section_html.append(relevant_html)

        if include_mentioned and ss["MENTIONED"]:
            mentioned_html = _render_tier_in_super_sector("MENTIONED", ss["MENTIONED"], condensed=True, cap=25)
            if mentioned_html:
                section_html.append(mentioned_html)

    body = "\n".join(section_html) or "<p>No relevant articles this week.</p>"

    try:
        date_str = run_date.strftime("%-d %B %Y")
    except ValueError:
        date_str = run_date.strftime("%d %B %Y")

    html = f"""<!doctype html>
<html><head><meta charset="utf-8"></head><body style="{S["body"]}">
<h1 style="{S["h1"]}">NH Client News Digest — Week of {date_str}</h1>
{stats}
{body}
<div style="{S["footer"]}">Automated digest. {len(items)} items considered after dedup. Filters: client name detection · quality gate · relevance scoring{" · LLM enrichment" if any(i.why_it_matters for i in items) else ""}.</div>
</body></html>"""
    return html


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
    try:
        msg["Subject"] = f"NH Client News Digest — {run_date.strftime('%-d %b %Y')}"
    except ValueError:
        msg["Subject"] = f"NH Client News Digest — {run_date.strftime('%d %b %Y')}"
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
