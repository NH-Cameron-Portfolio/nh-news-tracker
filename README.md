# NH Regulated Utilities News Tracker

Weekly automated news tracker for North Highland. Monitors press coverage of 43 target client organisations (water companies, electricity/gas networks, regulators, industry bodies) and emails a curated digest every Monday.

Sister project to `nh-tender-tracker` — same infrastructure pattern (GitHub Actions + cron-job.org + Gmail SMTP), different data sources and filtering logic.

---

## How it works

```
Google News RSS (1 feed per client, 43 feeds)
+ Static feeds (BBC, FT, Reuters, Utility Week, Ofgem, Ofwat, gov.uk, ...)
            │
            ▼
   ┌──────────────────────┐
   │ 1. Fetch & parse RSS │
   │ 2. Dedup URL + title │
   │ 3. Client detection  │  ← with disambiguation (SSE/SSEN, CCW/CCC, etc.)
   │ 4. Quality gate      │  ← length, sponsored, stock-only, language, age
   │ 5. Relevance scoring │  ← title/position/source/topic/length factors
   │ 6. Tier (PRI/REL/MEN)│
   │ 7. (opt) Claude pass │  ← "why it matters" enrichment via Haiku
   │ 8. Render + email    │
   └──────────────────────┘
```

---

## Repository structure

```
nh-news-tracker/
├── .github/workflows/news_tracker.yml   # GitHub Actions workflow
├── tracker/
│   ├── fetch_news.py                    # Main orchestrator (entry point)
│   ├── sources.py                       # RSS fetching + parsing
│   ├── dedupe.py                        # URL + fuzzy title dedup
│   ├── filters.py                       # Client detection + quality gate
│   ├── scoring.py                       # Numeric relevance scoring
│   ├── enrich.py                        # Optional Claude API enrichment
│   ├── email_render.py                  # HTML digest + SMTP send
│   ├── smoke_test.py                    # Synthetic article test suite
│   └── last_run.json                    # State (auto-managed)
├── config/
│   ├── clients.json                     # 43 orgs + aliases + disambiguation
│   ├── feeds.json                       # RSS sources
│   ├── relevance_topics.json            # Consulting-relevant keyword buckets
│   ├── exclusions.json                  # Noise filters
│   └── source_credibility.json          # Per-source scoring weights
├── output/                              # CSV + HTML committed here each run
└── requirements.txt
```

---

## GitHub Secrets / Variables required

In **Settings → Secrets and variables → Actions**:

### Secrets
| Name | Value |
|---|---|
| `SMTP_HOST` | `smtp.gmail.com` |
| `SMTP_PORT` | `587` |
| `SMTP_USER` | Sending Gmail address |
| `SMTP_PASS` | 16-character Gmail App Password |
| `EMAIL_RECIPIENTS` | Comma-separated recipient list |
| `ANTHROPIC_API_KEY` | *(optional)* For Stage 6 LLM enrichment |

### Variables (not secrets — these are toggles)
| Name | Value | Effect |
|---|---|---|
| `ENABLE_LLM_ENRICHMENT` | `1` to enable | Runs each PRIORITY/RELEVANT item through Claude Haiku for a "why it matters" blurb |
| `INCLUDE_MENTIONED` | `1` to enable | Includes the MENTIONED tier (low-confidence) in the email; off by default |

---

## Scheduling (carry over from tender tracker)

GitHub's native `schedule:` cron is unreliable. The workflow also exposes `workflow_dispatch` so cron-job.org can trigger it externally:

1. Create a GitHub Personal Access Token (PAT) with scope: `workflow`.
2. In cron-job.org, schedule a job for `0 6 * * 1` (Mondays 06:00 UTC).
3. Set the request:
   - URL: `https://api.github.com/repos/<owner>/nh-news-tracker/actions/workflows/news_tracker.yml/dispatches`
   - Method: POST
   - Headers:
     - `Accept: application/vnd.github+json`
     - `Authorization: Bearer <PAT>`
     - `X-GitHub-Api-Version: 2022-11-28`
   - Body: `{"ref":"main"}`

A native `schedule:` block is also included as a belt-and-braces fallback.

---

## Running locally

```bash
pip install -r requirements.txt
export SMTP_HOST=smtp.gmail.com SMTP_PORT=587 SMTP_USER=... SMTP_PASS=... EMAIL_RECIPIENTS=you@example.com
python -m tracker.fetch_news
```

For a dry run without sending email, comment out the `send_email` call in `fetch_news.py` and inspect `output/nh_news_*.html`.

### Smoke test (no network)

```bash
python -m tracker.smoke_test
```

Runs 13 synthetic articles through the filtering pipeline to verify disambiguation rules. Should print `Detection: 13 passed, 0 failed`.

---

## Tuning the filters

### When false positives appear in PRIORITY
1. Open the offending article's source URL.
2. Identify the client alias that matched.
3. In `config/clients.json`, add the false-positive phrase to that client's `negative_context` array.
4. Re-run smoke test to confirm you haven't broken legitimate matches.

### When real news is being missed
1. Check the run logs in GitHub Actions — was it fetched? Was it dropped at quality gate or scoring?
2. If dropped at client detection: add the relevant alias (or shorter context word) to that client's `exact_aliases` or `contextual_aliases`.
3. If scored too low: check whether the relevant topic keywords are in `config/relevance_topics.json`. Add as needed.

### When the digest is too long
- Raise the PRIORITY threshold in `scoring.py` (currently `>= 10`).
- Set `INCLUDE_MENTIONED=0` (default).
- Cap PRIORITY at N items in `email_render.py` (not currently implemented — add a slice).

---

## Known limitations

1. **Paywalled outlets** (FT, Times, Telegraph): headlines visible via RSS, full body not fetchable. Email surfaces the headline + source so the user can click through with their own subscription.
2. **Generic phrases**: "the national grid struggled..." may match `National Grid` if title-cased. Scoring downranks these (no topic keywords + low credibility source = below threshold), but expect occasional bleed-through.
3. **Private commercial companies** (Centrica, OVO, EDF retail, Drax, Octopus, SSE generation/retail) are NOT tracked — they fall outside NH's regulated-utility focus.
4. **Client list drift**: organisational renames (WPD → NGED, NESO spin-out) need quarterly review of the alias map in `clients.json`.
5. **Same Reuters story across 20 outlets**: dedup clusters these by fuzzy title; the highest-credibility version wins. Occasional misses possible.

---

## First-run guidance

Per spec §"Suggested First Move": run the MVP for one full week before tuning. Look at what comes through. Adjust `clients.json` aliases and `relevance_topics.json` against real false positives and real misses. Two days of theoretical tuning before any real data is premature.
