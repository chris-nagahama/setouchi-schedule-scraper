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
| `scripts/venues.py` | Venue name → coordinate, from the curated table |
| `scripts/venues.json` | The table, one entry per venue with its provenance |
| `fixtures/` | Real pages captured from the official site, and one payload this produces |

## Running it locally

```bash
python3 -m venv .venv && .venv/bin/pip install -r scripts/requirements.txt

# Check the parsers against the captured pages — no network.
.venv/bin/python scripts/scrape.py --verify --fixtures
.venv/bin/python scripts/detail.py --verify --fixtures
.venv/bin/python scripts/venues.py --verify

# Build the tree into ./public without touching the bucket.
.venv/bin/python scripts/publish.py --out public

# Which venues in a built tree have no coordinate yet.
.venv/bin/python scripts/venues.py --unlisted public
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

## Why venues carry a coordinate

The app cannot resolve a Japanese venue name to a place by itself. Apple's map
service follows the device's region, and on a China-region device it covers
mainland China only — measured there, `品川インターシティホール` returns no
result at all, and `高松festhalle` returns a factory in Ningbo. The published
`venueLocation` is what lets the app pin a venue instead of guessing at one.

`scripts/venues.json` is curated by hand rather than geocoded at run time. The
group plays a small, repeating set of rooms, a wrong pin is worse than no pin,
and a table is something a person can check — every entry records the address
it came from and how the coordinate was derived. WGS84; do not convert to
GCJ-02.

A venue the table does not cover publishes with `venueLocation: null`, which is
not a failure — the app falls back to searching by name, and that still works
for a device in Japan. The publish job names those venues in its log, and
`scripts/venues.py --unlisted` lists them on demand.

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

`fixtures/published/` is different in kind: it is a payload this repository
produced, and the app decodes it in its own tests. That is the only thing
holding the wire format between the two repositories, because an app that
cannot decode a payload falls back to the official site without saying so.
Regenerate it on both sides when the payload shape changes.
