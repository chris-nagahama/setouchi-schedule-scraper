# setouchi-schedule-scraper

Publishes STU48's official schedule as JSON, on a schedule, so an iOS app can
read structured data instead of parsing HTML on the device.

This is an unofficial personal project. It is not affiliated with, endorsed by,
or connected to STU48 or its management. It reads only pages that are already
public on `sp.stu48.com`.

## Why this exists

The app used to fetch and parse the official HTML itself. That works until the
site's markup changes — and then every user sees an empty schedule until a fix
clears App Store review, which takes days. Moving the parsing here means a
markup change is a script edit that reaches everyone within the hour, including
people who never update the app.

## What it produces

```
v1/index.json                 window, generation time, event signatures
v1/schedule/YYYY-MM.json      one month of events
v1/performance/<id>.json      one performance's occurrences
```

Published to a Cloudflare R2 bucket by `.github/workflows/publish-schedule.yml`,
which runs hourly and can be triggered by hand.

## Layout

| Path | Purpose |
| --- | --- |
| `scripts/scrape.py` | Month list pages → events |
| `scripts/detail.py` | Performance detail pages → occurrences |
| `scripts/publish.py` | Runs both, writes the JSON tree, enforces the guards |
| `fixtures/` | Real pages captured from the official site |

## Running it locally

```bash
python3 -m venv .venv && .venv/bin/pip install -r scripts/requirements.txt

# Check the parsers against the captured pages — no network.
.venv/bin/python scripts/scrape.py --verify --fixtures
.venv/bin/python scripts/detail.py --verify --fixtures

# Build the tree into ./public without touching the bucket.
.venv/bin/python scripts/publish.py --out public
```

## What the publish job refuses to do

Every reader is a phone that will display whatever it finds, so the job guards
the published tree harder than it guards freshness:

- The fixtures are verified before any network work. A scraper that no longer
  agrees with the captured pages never reaches live data.
- Nothing is written until every month has parsed. A partial run leaves the
  previous tree untouched.
- A month that used to have events is never replaced with an empty one. That
  shape means the site changed, not that the schedule was cleared.
- A single unreadable detail page only warns. Failing the whole run over one
  page would strand the entire schedule.

Detail pages are fetched only for performances — the one category the app opens
in-app — and only when an event is new or its title or date moved. A steady
run makes five requests; the first makes forty-five.

## Required secrets

| Secret | Where to find it |
| --- | --- |
| `R2_ACCOUNT_ID` | Cloudflare dashboard, account ID |
| `R2_BUCKET` | The bucket's name |
| `R2_ACCESS_KEY_ID` | R2 → Manage API Tokens, Object Read & Write |
| `R2_SECRET_ACCESS_KEY` | Shown once when the token is created |

The bucket needs a custom domain bound to it. The `r2.dev` development URL is
rate limited and unsupported for production traffic.

## A note on the fixtures

`fixtures/` is duplicated in the private iOS repository, where the app's Swift
tests read the same pages to check that both parsers agree. The two copies are
static and only ever gain files, but they can drift: add a capture on one side
without the other and the two implementations are no longer verified against
the same ground truth. Add new fixtures to both.
