"""
Render a realistic preview using real article titles from the user's last weekly digest.
This is purely for visual inspection — saves HTML to /tmp/v4_preview.html
"""

import json
from datetime import datetime, timezone, date, timedelta
from pathlib import Path
from tracker import sources, filters, scoring, dedupe, email_render

ROOT = Path(__file__).resolve().parent.parent
CONFIG = ROOT / "config"


def load(name):
    return json.load(open(CONFIG / name))


def make(title, summary, source_name, domain, days_ago=2):
    return sources.NewsItem(
        title=title,
        url=f"https://{domain}/article",
        summary=summary,
        source_name=source_name,
        source_domain=domain,
        feed_tags=[],
        published_at=datetime.now(timezone.utc) - timedelta(days=days_ago),
    )


# Real articles from the user's last digest
ARTICLES = [
    # ---- Energy & Utilities ----
    ("Complaint against Thames Water regarding GSS payments", "Case reference: OFW-061246 Case summary We are investigating a request for determination relating to GSS compensation following a supply interruption. Date opened 10 April 2026 Status Open Relevant powers Water Supply and Sewerage Services (Customer compensation).", "Ofwat News", "ofwat.gov.uk", 3),
    ("Ofgem urges CMA to dismiss gas networks' RIIO3 appeals - Utility Week", "Ofgem has urged the Competition and Markets Authority to dismiss appeals by gas distribution networks against its RIIO3 price control decision.", "Google News: Northern Gas Networks", "news.google.com", 3),
    ("South East Water boss quits after supply failures", "South East Water Chief Executive David Hinton has resigned following major supply outages in Kent and Sussex.", "BBC Business", "bbc.co.uk", 3),
    ("South East Water boss quits a week after chairman", "South East Water CEO David Hinton has resigned a week after the company chairman also stepped down.", "Sky News Business", "news.sky.com", 3),
    ("South East Water CEO to step down after Kent and Sussex supply outages | Water industry - The Guardian", "South East Water has announced that CEO David Hinton will step down following major outages in Kent and Sussex.", "Google News: South East Water", "news.google.com", 2),
    ("South East Water boss finally resigns after Tunbridge Wells taps run dry - The Telegraph", "South East Water CEO David Hinton has resigned following supply failures in Tunbridge Wells.", "Google News: South East Water", "news.google.com", 3),
    ("NI Water responsible for 11% of water pollution incidents in five years - The Irish News", "NI Water has been responsible for 11% of water pollution incidents in Northern Ireland over the past five years according to new figures.", "Google News: NI Water", "news.google.com", 0),
    ("Welsh Water slammed over 'disgusting' Afon Conwy sewage spill - North Wales Live", "Welsh Water has come under fire over a major sewage spill into the Afon Conwy.", "Google News: Welsh Water", "news.google.com", 4),
    ("RBC Capital raises United Utilities stock price target on equity raise - Investing.com", "RBC Capital has raised its price target on United Utilities following the company's equity raise to fund AMP8 investment.", "Google News: United Utilities", "news.google.com", 6),
    ("Yorkshire Water pumped sewage into bathing waters for almost 8,000 hours in 2025 - Yorkshire Post", "Yorkshire Water has been criticised after data showed sewage was discharged into bathing waters for almost 8,000 hours in 2025.", "Google News: Yorkshire Water", "news.google.com", 6),
    ("SSEN Transmission unveils 'revolutionary' modular substations - Project Scotland", "SSEN Transmission has unveiled modular substations as part of its grid modernisation programme.", "Google News: SSEN", "news.google.com", 0),
    ("Water customers want clearer explanations of company finances, says CCW report - Water Magazine", "CCW has published a report calling on water companies to provide clearer explanations of their finances to customers.", "Google News: CCW", "news.google.com", 0),
    ("Northern Powergrid strengthens leadership with double appointment - Insider Media Ltd", "Northern Powergrid has announced two new senior leadership appointments to strengthen its team.", "Google News: Northern Powergrid", "news.google.com", 6),
    ("Innovate UK pulls out of £40m Ofwat programme - Utility Week", "Innovate UK has withdrawn from a £40m Ofwat-funded innovation programme.", "Google News: Ofwat", "news.google.com", 4),
    ("Southern Water reveals £42m works overhaul plan for Kent - BBC", "Southern Water has revealed a £42m investment plan for Kent infrastructure.", "Google News: Southern Water", "news.google.com", 4),
    ("White & Case advises ATLAS Infrastructure on £400 million cornerstone investment in United Utilities - White & Case LLP", "Law firm White & Case has advised ATLAS Infrastructure on a £400 million cornerstone investment in United Utilities.", "Google News: United Utilities", "news.google.com", 6),
    ("SES Water completes major investment to improve water resilience in Box Hill - MSN", "SES Water has completed a major investment programme to improve water resilience in the Box Hill area.", "Google News: SES Water", "news.google.com", 2),
    ("'We're already taking actions': South Staffs Water details drought plans as 'super El Niño' looms", "South Staffs Water has detailed its drought response plans amid concerns about a super El Niño weather pattern.", "Google News: South Staffs Water", "news.google.com", 3),
    ("Energy UK carbon pricing report: comment", "Energy UK, the trade body for energy suppliers, has published a report on carbon pricing.", "Google News: Energy UK", "news.google.com", 4),
    ("Campaigners demand answers from Ofgem over British Gas meter inquiry - The Times", "Campaigners have demanded answers from Ofgem over the British Gas meter inquiry.", "Google News: Ofgem", "news.google.com", 5),

    # ---- MEC ----
    ("Vodafone to take full control of UK mobile operator in £4.3bn deal - Financial Times", "Vodafone has announced it will take full control of its UK mobile joint venture with Three in a £4.3bn deal, completing the merger of the two UK operators.", "Google News: Vodafone", "news.google.com", 6),
    ("Vodafone Attract Most Ofcom UK Complaints for Broadband and O2 for Mobile – Q4 2025", "New Ofcom data shows Vodafone attracted the most broadband complaints while Virgin Media O2 led mobile complaints in Q4 2025.", "Google News: Vodafone", "news.google.com", 0),
    ("Virgin Media O2 UK Sees Record Broadband Traffic Spike from Footy Streaming - ISPreview UK", "Virgin Media O2 UK has reported record broadband traffic levels driven by football streaming.", "Google News: Virgin Media O2", "news.google.com", 4),
    ("O2 brings next-generation 5G+ connectivity to communities across Wales", "Virgin Media O2 has expanded its 5G+ network to communities across Wales as part of a nationwide network transformation programme.", "Google News: Virgin Media O2", "news.google.com", 5),
    ("Vodafone UK launches new 5G Broadband service - Telecompaper", "Vodafone UK has launched a new 5G broadband service for residential customers.", "Google News: Vodafone", "news.google.com", 0),
    ("Informa Connect Announces the Launch of Construction IMPEX Canada in Ottawa", "Informa Connect, part of Informa plc, has announced the launch of Construction IMPEX Canada, a major new trade exhibition in Ottawa.", "Google News: Informa", "news.google.com", 4),
    ("BT International and STACKIT partner to expand reach and resilience of European sovereign cloud access", "BT International, part of BT Group, has partnered with STACKIT to expand European sovereign cloud access for enterprise customers.", "Google News: BT Group", "news.google.com", 5),
]


def main():
    clients = load("clients.json")
    topics = load("relevance_topics.json")
    credibility = load("source_credibility.json")
    exclusions = load("exclusions.json")

    items = [make(t, s, src, dom, days) for t, s, src, dom, days in ARTICLES]
    items = filters.detect_clients(items, clients)
    items = filters.quality_gate(items, exclusions)
    items = scoring.score_and_tier(items, topics, credibility, exclusions)

    print(f"Items after pipeline: {len(items)}")
    for it in items:
        print(f"  [{it.tier} score={it.score}] {it.title[:80]}")
        print(f"    clients={it.matched_clients}")

    html = email_render.render_html(items, date.today(), clients_cfg=clients)
    out_path = "/tmp/v4_preview.html"
    with open(out_path, "w") as f:
        f.write(html)
    print(f"\nPreview saved to {out_path} ({len(html)} chars)")


if __name__ == "__main__":
    main()
