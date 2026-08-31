#!/usr/bin/env python3
"""Build the JSON tree the app fetches, from the official schedule pages.

Layout produced under `--out`:

    v1/index.json                 window, generation time, event signatures
    v1/schedule/YYYY-MM.json      one month of events
    v1/performance/<id>.json      one performance's occurrences
    v1/news.json                  the newest entries from the official news index

Two properties matter more than freshness, because every reader of this data is
a phone that will show whatever it finds:

* Nothing is written until every month has parsed. A partial run leaves the
  previously published tree untouched.
* A month that used to have events is never replaced with an empty one. That
  pattern means the site changed shape, not that the schedule was cleared, and
  publishing it would blank the app for everyone.

Detail pages are fetched only for performances — the only category the app
opens in-app — when the event is new, when its title or date moved, every few
hours while its page is still missing a cast or a start time, and every few
hours once the show is within a week whether or not the payload looks
finished. Those last two are the only way the announcements that never touch
the month list get picked up; a steady-state run makes a handful of requests.
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from dataclasses import asdict
from datetime import date, datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import detail as detail_module
import news as news_module
import scrape as scrape_module
import venues

SCHEMA_VERSION = 1
JAPAN = ZoneInfo("Asia/Tokyo")
REQUEST_INTERVAL_SECONDS = 1.0

# How long to leave a performance before looking at its page again. A cast and
# a start time are announced on the detail page alone, without the month list
# changing at all, so nothing else would ever notice.
RECHECK_INTERVAL_SECONDS = 6 * 60 * 60

# How near a show has to be for its page to be reread whether or not the
# payload already looks finished.
#
# A tour date is published twice. It goes up as an announcement — the whole
# tour's dates, one line of prose for the times — and is replaced, as the date
# nears, by that stop's own page carrying the venue, the door time and, where
# the day runs twice, both stages. Nothing about that rewrite reaches the month
# list, and the announcement parses into a payload with a cast and a start time
# that `is_incomplete` is satisfied by, so a payload could sit frozen on the
# announcement until the show had come and gone. Both stops of the August tour
# did: read once on the 4th, still describing a page that had been replaced by
# the 31st, one of them hiding a second stage nobody was told about.
#
# A week. The rewrite lands with the ticket details rather than months out, and
# a handful of shows fall inside it, so this is a couple of requests a run.
# A recheck that finds the page unchanged writes nothing — see `says_the_same`.
RECHECK_WINDOW_DAYS = 7


class PublishError(RuntimeError):
    """Something looked wrong enough that publishing would do harm."""


def month_window(today: datetime, back: int, ahead: int) -> list[tuple[int, int]]:
    months = []
    for offset in range(-back, ahead + 1):
        total = (today.year * 12 + today.month - 1) + offset
        months.append((total // 12, total % 12 + 1))
    return months


def read_json(path: Path) -> dict | None:
    try:
        return json.loads(path.read_text("utf-8"))
    except (OSError, ValueError):
        return None


def write_json(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )


def signature(event) -> str:
    """What has to change on the month list before a detail page is refetched."""
    return f"{event.date.year:04d}-{event.date.month:02d}-{event.date.day:02d}|{event.title}"


def is_incomplete(payload: dict | None) -> bool:
    """Whether a published detail page is still waiting on its own page.

    A performance is announced in stages: the date and venue first, the cast and
    the times later, all on the detail page and none of it visible from the
    month list. A payload missing either is one the page may since have filled
    in — which is not the same as the show having no cast, and the app draws
    them the same way, so the only way to be right is to look again.
    """
    if not payload:
        return True
    occurrences = payload.get("occurrences") or []
    if not occurrences:
        return True
    if not any(occurrence.get("memberNames") for occurrence in occurrences):
        return True
    return not any(occurrence.get("startTime") for occurrence in occurrences)


def says_the_same(first: dict, second: dict) -> bool:
    """Whether two payloads carry the same content, their timestamps aside."""
    return {key: value for key, value in first.items() if key != "generatedAt"} == {
        key: value for key, value in second.items() if key != "generatedAt"
    }


def read_timestamp(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.strptime(value, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
    except ValueError:
        return None


def build(
    out: Path,
    fixtures: Path | None,
    back: int,
    ahead: int,
    force: bool,
) -> int:
    now = datetime.now(timezone.utc)
    today = datetime.now(JAPAN).date()
    window = month_window(datetime.now(JAPAN), back, ahead)
    previous_index = read_json(out / "v1" / "index.json") or {}
    previous_signatures = previous_index.get("signatures", {})
    previous_checks = previous_index.get("checkedAt", {})
    venues_fingerprint = venues.fingerprint()
    # A payload shape change, a parser correction or a new venue coordinate
    # makes every detail page stale, however still its event looks. Nothing
    # else would rebuild them: the signature below only tracks the event, so a
    # page whose date has passed would keep the old reading for good.
    reshaped = (
        previous_index.get("payloadRevision") != detail_module.PAYLOAD_REVISION
        or previous_index.get("venuesFingerprint") != venues_fingerprint
    )

    # Parse everything before writing anything.
    months: dict[str, dict] = {}
    performances: list = []

    for year, month in window:
        key = f"{year:04d}-{month:02d}"
        try:
            html = scrape_module.load_month(year, month, fixtures)
            events = scrape_module.parse_schedule_list(html, year, month)
        except scrape_module.ParseError as error:
            raise PublishError(f"{key}: {error}") from error
        except Exception as error:  # network, decoding, anything else
            raise PublishError(f"{key}: fetch failed: {error}") from error

        published = read_json(out / "v1" / "schedule" / f"{key}.json")
        if published and published.get("events") and not events and not force:
            raise PublishError(
                f"{key}: parsed 0 events but {len(published['events'])} were "
                "published before; refusing to blank a month"
            )

        months[key] = {
            "schemaVersion": SCHEMA_VERSION,
            "generatedAt": now.strftime("%Y-%m-%dT%H:%M:%SZ"),
            "month": {"year": year, "month": month},
            "events": [asdict(event) for event in events],
        }
        performances.extend(e for e in events if e.category == "performance")

        if fixtures is None:
            time.sleep(REQUEST_INTERVAL_SECONDS)

    # Detail pages: new events, ones whose title or date moved, and ones whose
    # page has not finished announcing itself.
    signatures = {event.id: signature(event) for event in performances}

    def due_for_recheck(event) -> bool:
        """Whether a page is worth another look this run.

        Two reasons to look: the payload has not said everything yet, or the
        show is near enough that its page may have been rewritten under a
        payload that looks finished. A show that has already happened is past
        either, and one looked at recently will not have moved since, so
        neither is fetched again.
        """
        event_date = date(event.date.year, event.date.month, event.date.day)
        if event_date < today:
            return False
        if (
            not is_incomplete(read_json(out / "v1" / "performance" / f"{event.id}.json"))
            and (event_date - today).days > RECHECK_WINDOW_DAYS
        ):
            return False
        checked = read_timestamp(previous_checks.get(event.id))
        return (
            checked is None
            or (now - checked).total_seconds() >= RECHECK_INTERVAL_SECONDS
        )

    stale: list = []
    rechecks = 0
    for event in performances:
        if (
            force
            or reshaped
            or previous_signatures.get(event.id) != signatures[event.id]
            or not (out / "v1" / "performance" / f"{event.id}.json").exists()
        ):
            stale.append(event)
        elif due_for_recheck(event):
            stale.append(event)
            rechecks += 1

    details: dict[str, dict] = {}
    read_now: list[str] = []
    skipped: list[str] = []
    unlocated: set[str] = set()
    for event in stale:
        fallback = detail_module.FallbackEvent(
            id=event.id,
            title=event.title,
            date=detail_module.ScheduleDate(
                event.date.year, event.date.month, event.date.day
            ),
        )
        try:
            html = detail_module.load_detail(event.id, fixtures)
            occurrences = detail_module.parse_detail(html, fallback)
        except Exception as error:
            # One unreadable detail page is not worth failing the run over: the
            # month list still publishes, and the app falls back to the values
            # it already carries. Failing here would strand the whole schedule.
            print(f"warning: detail {event.id}: {error}", file=sys.stderr)
            skipped.append(event.id)
            continue

        read_now.append(event.id)
        payload = {
            "schemaVersion": SCHEMA_VERSION,
            "generatedAt": now.strftime("%Y-%m-%dT%H:%M:%SZ"),
            "id": event.id,
            "occurrences": [
                asdict(occurrence)
                for occurrence in detail_module.with_venue_locations(occurrences)
            ],
        }

        # Most rechecks find a page that still says what it said last time.
        # Rewriting it would move `generatedAt` — which is meant to say when the
        # performance last changed, not when it was last looked at — and hand
        # the upload a tree of files to push that differ only in that.
        published_detail = read_json(out / "v1" / "performance" / f"{event.id}.json")
        if published_detail is None or not says_the_same(published_detail, payload):
            details[event.id] = payload

        for occurrence in occurrences:
            if occurrence.venue and venues.coordinate_for(occurrence.venue) is None:
                unlocated.add(occurrence.venue)

        if fixtures is None:
            time.sleep(REQUEST_INTERVAL_SECONDS)

    # The news index. Deliberately not fatal: it is one secondary section of one
    # screen, and failing here would strand the schedule that every other reader
    # came for. A run that cannot read it leaves the published file alone, and
    # the app decides for itself whether what it finds is too old to show.
    news_items: list | None = None
    try:
        news_items = news_module.parse_news_list(news_module.load_news(fixtures))
    except Exception as error:
        print(f"warning: news: {error}", file=sys.stderr)

    for key, payload in months.items():
        write_json(out / "v1" / "schedule" / f"{key}.json", payload)
    for event_id, payload in details.items():
        write_json(out / "v1" / "performance" / f"{event_id}.json", payload)

    if news_items is not None:
        # Rewritten every run even when the entries are identical, unlike the
        # detail pages. It is a single small file, and the timestamp is what
        # lets the app tell "nothing was published today" from "this job has
        # been dead for a week" — which it cannot do if the file only moves
        # when the news does.
        write_json(
            out / "v1" / "news.json",
            {
                "schemaVersion": news_module.SCHEMA_VERSION,
                "generatedAt": now.strftime("%Y-%m-%dT%H:%M:%SZ"),
                "items": [asdict(item) for item in news_items],
            },
        )

    write_json(
        out / "v1" / "index.json",
        {
            "schemaVersion": SCHEMA_VERSION,
            "payloadRevision": detail_module.PAYLOAD_REVISION,
            "venuesFingerprint": venues_fingerprint,
            "generatedAt": now.strftime("%Y-%m-%dT%H:%M:%SZ"),
            "months": sorted(months),
            "signatures": {
                **{k: v for k, v in previous_signatures.items() if k in signatures},
                **{k: v for k, v in signatures.items() if k not in skipped},
            },
            # When each detail page was last read, which is what paces the
            # rechecks above. Kept here rather than in the payloads: a page
            # rewritten hourly to update its own timestamp would churn the whole
            # tree to say nothing had changed.
            "checkedAt": {
                **{k: v for k, v in previous_checks.items() if k in signatures},
                **{
                    event_id: now.strftime("%Y-%m-%dT%H:%M:%SZ")
                    for event_id in read_now
                },
            },
        },
    )

    if unlocated:
        # Not a failure. The app searches for a venue it was given no
        # coordinate for, which still works in Japan — but the search is what
        # this table exists to avoid, so a new tour stop should be visible in
        # the log rather than waiting for someone to notice a blank map.
        print(
            "note: no coordinate in scripts/venues.json for "
            + ", ".join(sorted(unlocated)),
            file=sys.stderr,
        )

    if reshaped and previous_index:
        print(
            f"note: payload revision {detail_module.PAYLOAD_REVISION}; "
            "refetched every detail page"
        )

    total_events = sum(len(payload["events"]) for payload in months.values())
    print(
        f"published {len(months)} months ({total_events} events), "
        f"{len(read_now)} detail page(s) read ({rechecks} rechecked), "
        f"{len(details)} changed, {len(performances) - len(stale)} left alone, "
        f"{len(skipped)} skipped, "
        + (f"{len(news_items)} news item(s)" if news_items is not None else "news unread")
    )
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", type=Path, required=True, help="output directory")
    parser.add_argument("--months-back", type=int, default=1)
    parser.add_argument("--months-ahead", type=int, default=3)
    parser.add_argument(
        "--fixtures",
        nargs="?",
        const=scrape_module.REPOSITORY_ROOT / "fixtures",
        type=Path,
        help="read captured pages from this directory instead of the network",
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help="refetch every detail page and allow a month to become empty",
    )
    args = parser.parse_args()

    try:
        return build(
            out=args.out,
            fixtures=args.fixtures,
            back=args.months_back,
            ahead=args.months_ahead,
            force=args.force,
        )
    except PublishError as error:
        print(f"error: {error}", file=sys.stderr)
        print("nothing was written; the published tree is unchanged", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
