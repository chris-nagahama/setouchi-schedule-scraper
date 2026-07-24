#!/usr/bin/env python3
"""Build the JSON tree the app fetches, from the official schedule pages.

Layout produced under `--out`:

    v1/index.json                 window, generation time, event signatures
    v1/schedule/YYYY-MM.json      one month of events
    v1/performance/<id>.json      one performance's occurrences

Two properties matter more than freshness, because every reader of this data is
a phone that will show whatever it finds:

* Nothing is written until every month has parsed. A partial run leaves the
  previously published tree untouched.
* A month that used to have events is never replaced with an empty one. That
  pattern means the site changed shape, not that the schedule was cleared, and
  publishing it would blank the app for everyone.

Detail pages are fetched only for performances — the only category the app
opens in-app — and only when the event is new or its title or date moved, so a
steady-state run makes four requests.
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from dataclasses import asdict
from datetime import datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import detail as detail_module
import scrape as scrape_module
import venues

SCHEMA_VERSION = 1
JAPAN = ZoneInfo("Asia/Tokyo")
REQUEST_INTERVAL_SECONDS = 1.0


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
    """What has to change before a detail page is worth fetching again."""
    return f"{event.date.year:04d}-{event.date.month:02d}-{event.date.day:02d}|{event.title}"


def build(
    out: Path,
    fixtures: Path | None,
    back: int,
    ahead: int,
    force: bool,
) -> int:
    now = datetime.now(timezone.utc)
    window = month_window(datetime.now(JAPAN), back, ahead)
    previous_index = read_json(out / "v1" / "index.json") or {}
    previous_signatures = previous_index.get("signatures", {})
    # A payload shape change makes every detail page stale, however still its
    # event looks. Nothing else would rebuild them: the signature below only
    # tracks the event, so a page whose date has passed would keep a payload
    # missing the new field for good.
    reshaped = previous_index.get("payloadRevision") != detail_module.PAYLOAD_REVISION

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

    # Detail pages: new events, or ones whose title or date moved.
    signatures = {event.id: signature(event) for event in performances}
    stale = [
        event
        for event in performances
        if force
        or reshaped
        or previous_signatures.get(event.id) != signatures[event.id]
        or not (out / "v1" / "performance" / f"{event.id}.json").exists()
    ]

    details: dict[str, dict] = {}
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

        details[event.id] = {
            "schemaVersion": SCHEMA_VERSION,
            "generatedAt": now.strftime("%Y-%m-%dT%H:%M:%SZ"),
            "id": event.id,
            "occurrences": [
                asdict(occurrence)
                for occurrence in detail_module.with_venue_locations(occurrences)
            ],
        }

        for occurrence in occurrences:
            if occurrence.venue and venues.coordinate_for(occurrence.venue) is None:
                unlocated.add(occurrence.venue)

        if fixtures is None:
            time.sleep(REQUEST_INTERVAL_SECONDS)

    for key, payload in months.items():
        write_json(out / "v1" / "schedule" / f"{key}.json", payload)
    for event_id, payload in details.items():
        write_json(out / "v1" / "performance" / f"{event_id}.json", payload)

    write_json(
        out / "v1" / "index.json",
        {
            "schemaVersion": SCHEMA_VERSION,
            "payloadRevision": detail_module.PAYLOAD_REVISION,
            "generatedAt": now.strftime("%Y-%m-%dT%H:%M:%SZ"),
            "months": sorted(months),
            "signatures": {
                **{k: v for k, v in previous_signatures.items() if k in signatures},
                **{k: v for k, v in signatures.items() if k not in skipped},
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
        f"{len(details)} detail page(s) refreshed, "
        f"{len(performances) - len(stale)} unchanged, {len(skipped)} skipped"
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
