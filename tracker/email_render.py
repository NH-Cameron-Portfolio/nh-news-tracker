"""
email_render.py — Build the HTML digest and send via Gmail SMTP.

v4 layout:
  Header (date + stats bar)
  Jump-to index (clickable client names with story counts)
  Super-sector: Energy & Utilities
    Each client section:
      - Sector-coloured bar with client name + story count
      - PRIORITY stories (full detail)
      - RELEVANT stories (condensed)
      - All headlines are clickable to the article
  Super-sector: Media, Entertainment & Communications
    (same structure)
  Footer

All layout uses HTML tables with inline styles for Outlook compatibility.
No CSS Grid, no Flexbox, no border-radius (Word renderer strips them).
"""

from __future__ import annotations

import logging
import os
import re
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

# Per-sector colour for the client bar headers
SECTOR_COLOURS = {
    "Water":         "#1f4e79",   # navy
    "Electricity":   "#0f7b8a",   # teal
    "Gas":           "#b85c0e",   # amber-orange
    "Regulator":     "#555555",   # neutral grey
    "Industry Body": "#6b6b6b",   # darker grey
    "Telecoms":      "#7d2961",   # magenta
    "Media":         "#5a3b8b",   # purple
    "Unknown":       "#777777",
}

# ---------- Inline styles (Outlook-safe — no border-radius, no flex, no grid) ----------

S = {
    "wrapper":      "font-family:Arial,Helvetica,sans-serif;color:#222;line-height:1.45;max-width:760px;margin:0 auto;padding:16px;background:#ffffff;",
    "h1":           "font-size:20px;margin:0 0 6px;color:#132E53;font-weight:bold;",
    "subtitle":     "font-size:13px;color:#666;margin:0 0 16px;",
    "stats":        "background:#f4f6f9;border:1px solid #d8dde5;padding:10px 14px;font-size:13px;",
    "stat":         "margin-right:14px;",
    # Jump-to index
    "index_box":    "background:#fafbfc;border:1px solid #d8dde5;padding:12px 14px;margin:16px 0 24px;font-size:12px;line-height:1.9;",
    "index_label":  "font-weight:bold;color:#132E53;margin-bottom:6px;display:block;",
    "index_link":   "color:#132E53;text-decoration:none;margin-right:8px;white-space:nowrap;",
    # Super-sector header
    "h2_super":     "font-size:17px;margin:32px 0 12px;padding:8px 0;border-bottom:3px solid #132E53;color:#132E53;font-weight:bold;letter-spacing:0.5px;",
    # Client section
    "client_table":     "width:100%;border-collapse:collapse;margin:16px 0;border:1px solid #e0e4eb;",
    "client_bar_cell":  "padding:8px 14px;color:#ffffff;font-size:14px;font-weight:bold;",
    "client_count":     "font-size:12px;font-weight:normal;opacity:0.9;",
    "client_body":      "padding:8px 14px;",
    # Item rendering
    "item_priority":    "padding:10px 0;border-bottom:1px solid #eee;",
    "item_relevant":    "padding:6px 0;border-bottom:1px solid #f4f4f4;",
    "title_priority":   "font-weight:bold;font-size:14px;margin-bottom:2px;line-height:1.3;",
    "title_relevant":   "font-size:13px;line-height:1.3;",
    "link":             "color:#132E53;text-decoration:none;",
    "meta":             "color:#666;font-size:11px;margin:2px 0;",
    "summary":          "font-size:12px;color:#444;margin:4px 0;",
    "why":              "font-size:12px;color:#039FB8;font-style:italic;margin:4px 0;",
    "topic_tag":        "display:inline-block;background:#f0f0f0;padding:1px 6px;margin:2px 4px 0 0;font-size:10px;color:#666;",
    "tier_label":       "font-size:11px;font-weight:bold;text-transform:uppercase;color:#888;letter-spacing:0.5px;margin:8px 0 4px;",
    "footer":           "font-size:11px;color:#888;margin-top:32px;padding-top:12px;border-top:1px solid #eee;",
    "empty":            "font-size:13px;color:#888;font-style:italic;margin:10px 0;",
}


def _fmt_date(item: NewsItem) -> str:
    if not item.published_at:
        return ""
    try:
        return item.published_at.strftime("%-d %b")
    except ValueError:
        return item.published_at.strftime("%d %b")


# ---------- Sector lookup ----------

def _client_sector_map(clients_cfg: dict) -> dict[str, str]:
    """Map canonical client name → sector for fast lookup."""
    return {cfg["canonical"]: cfg.get("sector", "Unknown") for cfg in clients_cfg.values()}


def _first_sector(item: NewsItem, sector_map: dict[str, str]) -> str:
    if not item.matched_clients:
        return "Unknown"
    return sector_map.get(item.matched_clients[0], "Unknown")


def _super_sector(item: NewsItem, sector_map: dict[str, str]) -> str:
    sector = _first_sector(item, sector_map)
    if sector in ENERGY_UTILITIES_SECTORS:
        return "Energy & Utilities"
    if sector in MEC_SECTORS:
        return "Media, Entertainment & Communications"
    return "Other"


def _slugify(name: str) -> str:
    return "c-" + re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")


# ---------- Item rendering ----------

def _strip_title_suffix(title: str) -> str:
    """Strip Google News-style ' - Source' suffix for cleaner display."""
    return re.sub(r"\s+[-|–]\s+[^-|–]{2,60}$", "", title or "").strip()


def _render_priority_item(item: NewsItem) -> str:
    display_title = _strip_title_suffix(item.title)
    title_html = f'<a href="{escape(item.url)}" style="{S["link"]}">{escape(display_title)}</a>'
    parts = [f'<div style="{S["title_priority"]}">{title_html}</div>']
    parts.append(f'<div style="{S["meta"]}">{escape(item.source_name)} · {_fmt_date(item)}</div>')

    # Show summary only if it's substantively different from the title
    snippet = item.summary[:280]
    if len(item.summary) > 280:
        snippet += "…"
    if snippet.strip().lower() != display_title.lower() and snippet.strip().lower() != item.title.lower():
        parts.append(f'<div style="{S["summary"]}">{escape(snippet)}</div>')

    if item.why_it_matters:
        parts.append(f'<div style="{S["why"]}">Why it matters: {escape(item.why_it_matters)}</div>')

    if item.matched_topics:
        tag_html = "".join(
            f'<span style="{S["topic_tag"]}">{escape(t.replace("_", " "))}</span>'
            for t in item.matched_topics[:3]
        )
        parts.append(f'<div>{tag_html}</div>')

    return f'<div style="{S["item_priority"]}">{"".join(parts)}</div>'


def _render_relevant_item(item: NewsItem) -> str:
    display_title = _strip_title_suffix(item.title)
    title_html = f'<a href="{escape(item.url)}" style="{S["link"]}">{escape(display_title)}</a>'
    meta = f'<span style="{S["meta"]}">{escape(item.source_name)} · {_fmt_date(item)}</span>'
    return (
        f'<div style="{S["item_relevant"]}">'
        f'<div style="{S["title_relevant"]}">{title_html}</div>{meta}'
        f'</div>'
    )


# ---------- Client section rendering ----------

def _render_client_section(client_name: str, sector: str, priority_items: list[NewsItem], relevant_items: list[NewsItem]) -> str:
    """Render one client's card: coloured header bar, then priority + relevant items."""
    anchor = _slugify(client_name)
    colour = SECTOR_COLOURS.get(sector, SECTOR_COLOURS["Unknown"])
    total = len(priority_items) + len(relevant_items)
    count_text = f' <span style="{S["client_count"]}">· {total} {"story" if total == 1 else "stories"}</span>'

    parts = [
        f'<a name="{anchor}"></a>',
        f'<table cellpadding="0" cellspacing="0" border="0" style="{S["client_table"]}">',
        f'  <tr><td style="{S["client_bar_cell"]};background:{colour};">{escape(client_name)}{count_text}</td></tr>',
        f'  <tr><td style="{S["client_body"]}">',
    ]

    if priority_items:
        parts.append(f'<div style="{S["tier_label"]}">⭐ Priority</div>')
        for it in priority_items:
            parts.append(_render_priority_item(it))

    if relevant_items:
        if priority_items:
            parts.append(f'<div style="{S["tier_label"]}">Relevant</div>')
        # If only relevant items, no label needed — the client header is enough
        for it in relevant_items:
            parts.append(_render_relevant_item(it))

    parts.append('  </td></tr>')
    parts.append('</table>')
    return "\n".join(parts)


# ---------- Main entry point ----------

def render_html(items: list[NewsItem], run_date: date, include_mentioned: bool = False, clients_cfg: dict | None = None) -> str:
    sector_map = _client_sector_map(clients_cfg) if clients_cfg else {}

    # Bucket: super_sector -> client_name -> (priority_list, relevant_list)
    by_ss_client: dict[str, dict[str, dict[str, list[NewsItem]]]] = {}
    for it in items:
        if it.tier == "DISCARDED":
            continue
        if it.tier == "MENTIONED" and not include_mentioned:
            continue
        ss = _super_sector(it, sector_map)
        if ss == "Other":
            continue  # shouldn't happen but defensive
        client = it.matched_clients[0] if it.matched_clients else "Unknown"
        by_ss_client.setdefault(ss, {}).setdefault(client, {"priority": [], "relevant": [], "mentioned": []})
        if it.tier == "PRIORITY":
            by_ss_client[ss][client]["priority"].append(it)
        elif it.tier == "RELEVANT":
            by_ss_client[ss][client]["relevant"].append(it)
        elif it.tier == "MENTIONED" and include_mentioned:
            by_ss_client[ss][client]["relevant"].append(it)

    # Sort items within each client bucket by (score desc, date desc)
    for ss_clients in by_ss_client.values():
        for client_buckets in ss_clients.values():
            for key in ("priority", "relevant"):
                client_buckets[key].sort(
                    key=lambda it: (it.score, it.published_at.timestamp() if it.published_at else 0),
                    reverse=True,
                )

    # ---- Build stats bar ----
    total = sum(
        len(b["priority"]) + len(b["relevant"])
        for ss in by_ss_client.values() for b in ss.values()
    )
    priority_total = sum(
        len(b["priority"]) for ss in by_ss_client.values() for b in ss.values()
    )
    relevant_total = sum(
        len(b["relevant"]) for ss in by_ss_client.values() for b in ss.values()
    )

    stats_html = (
        f'<div style="{S["stats"]}">'
        f'<span style="{S["stat"]}">Total: <b>{total}</b></span>'
        f'<span style="{S["stat"]}">⭐ Priority: <b>{priority_total}</b></span>'
        f'<span style="{S["stat"]}">Relevant: <b>{relevant_total}</b></span>'
        f'</div>'
    )

    # ---- Build jump-to index ----
    index_parts = [f'<div style="{S["index_box"]}">', f'<span style="{S["index_label"]}">Jump to client</span>']
    for ss_name, _ in SUPER_SECTOR_DEFS:
        if ss_name not in by_ss_client:
            continue
        clients = sorted(
            by_ss_client[ss_name].items(),
            key=lambda kv: (-(len(kv[1]["priority"]) + len(kv[1]["relevant"])), kv[0])
        )
        for client_name, buckets in clients:
            count = len(buckets["priority"]) + len(buckets["relevant"])
            if count == 0:
                continue
            anchor = _slugify(client_name)
            sector = sector_map.get(client_name, "Unknown")
            colour = SECTOR_COLOURS.get(sector, SECTOR_COLOURS["Unknown"])
            star = "⭐ " if buckets["priority"] else ""
            index_parts.append(
                f'<a href="#{anchor}" style="{S["index_link"]};border-left:3px solid {colour};padding-left:6px;">'
                f'{star}{escape(client_name)} ({count})</a>'
            )
    index_parts.append('</div>')
    index_html = "\n".join(index_parts)

    # ---- Build sections ----
    section_html_parts: list[str] = []
    for ss_name, _ in SUPER_SECTOR_DEFS:
        if ss_name not in by_ss_client:
            continue
        ss_data = by_ss_client[ss_name]

        section_html_parts.append(f'<h2 style="{S["h2_super"]}">{escape(ss_name.upper())}</h2>')

        clients_sorted = sorted(
            ss_data.items(),
            key=lambda kv: (
                -len(kv[1]["priority"]),                                        # priority count desc
                -max((it.score for it in kv[1]["priority"]), default=0),        # then highest priority score
                -len(kv[1]["relevant"]),                                        # then relevant count
                kv[0].lower(),                                                  # then alphabetical
            ),
        )
        for client_name, buckets in clients_sorted:
            if not buckets["priority"] and not buckets["relevant"]:
                continue
            sector = sector_map.get(client_name, "Unknown")
            section_html_parts.append(_render_client_section(
                client_name, sector, buckets["priority"], buckets["relevant"]
            ))

    body_html = "\n".join(section_html_parts) or '<div style="margin:20px 0;">No relevant articles this week.</div>'

    # ---- Assemble ----
    try:
        date_str = run_date.strftime("%-d %B %Y")
    except ValueError:
        date_str = run_date.strftime("%d %B %Y")

    html = f"""<!doctype html>
<html><head><meta charset="utf-8"><title>NH Client News Digest</title></head>
<body style="margin:0;padding:0;background:#f4f4f4;">
<div style="{S["wrapper"]}">
  <div style="{S["h1"]}">NH Client News Digest</div>
  <div style="{S["subtitle"]}">Week of {date_str}</div>
  {stats_html}
  {index_html}
  {body_html}
  <div style="{S["footer"]}">Automated digest. {len(items)} items considered after dedup. Filters: client name detection · quality gate · relevance scoring{" · LLM enrichment" if any(i.why_it_matters for i in items) else ""}. Tip: ⭐ marks clients with priority-tier stories this week.</div>
</div>
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
