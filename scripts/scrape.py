#!/usr/bin/env python3
"""Scrape STU48's official schedule into the JSON the app consumes.

The app currently parses sp.stu48.com's HTML on device, which means every
structural change on the official site needs an App Store release to fix. This
script moves that parsing server-side so a scheduled job can publish JSON and a
broken selector can be repaired in minutes.

The parsing rules mirror `ScheduleHTMLParser` in the iOS target. Fixtures under
`fixtures/` and the expectations in `ScheduleFixtureTests.swift` are the shared
ground truth for both implementations; run `--verify` after changing either.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable

from bs4 import BeautifulSoup

SCHEMA_VERSION = 1
BASE_URL = "https://sp.stu48.com"
REPOSITORY_ROOT = Path(__file__).resolve().parent.parent

# Mirrors `ScheduleCategory.officialCode`. The site encodes the category as a
# `liveNN` class on each entry; the app stores the semantic name instead, so the
# mapping lives here and a newly introduced code can be handled without a
# release.
CATEGORY_BY_OFFICIAL_CODE = {
    "live02": "birthday",
    "live03": "handshake",
    "live04": "performance",
    "live05": "release",
    "live06": "event",
    "live08": "media",
    "live10": "other",
}

OFFICIAL_CODE_PATTERN = re.compile(r"^live\d{2}$")


class ParseError(RuntimeError):
    """The page did not look like a schedule page we know how to read."""


@dataclass(frozen=True)
class ScheduleDate:
    year: int
    month: int
    day: int


@dataclass(frozen=True)
class ScheduleEvent:
    id: str
    date: ScheduleDate
    category: str
    title: str
    detailURL: str


def plain_text(node) -> str:
    """Collapse an element's text the way the Swift parser does."""
    return re.sub(r"\s+", " ", node.get_text(" ", strip=True)).strip()


def detail_url(href: str) -> str | None:
    href = href.strip()
    if not href:
        return None
    if href.startswith("//"):
        return f"https:{href}"
    if re.match(r"^[a-zA-Z][a-zA-Z0-9+.-]*:", href):
        return href
    if not href.startswith("/"):
        href = f"/{href}"
    return f"{BASE_URL}{href}"


def parse_schedule_list(html: str, year: int, month: int) -> list[ScheduleEvent]:
    """Extract every entry from a month list page.

    Entries only count when they sit inside a day block. The page also renders a
    category filter whose checkbox ids look like `liveNN`, so matching those
    codes document-wide would pull in menu chrome that is not an event.
    """
    soup = BeautifulSoup(html, "lxml")

    if not soup.select_one(".list--schedule"):
        # A month with nothing scheduled yet renders without the list container
        # at all, and the far end of the publish window regularly lands on one.
        # The rest of the page is intact there — the category filter is what
        # tells the two cases apart, so treating a missing list as a structure
        # change would fail the whole run over a month that is merely empty.
        if soup.select_one(".schedule.list--category"):
            return []
        raise ParseError("no .list--schedule container; page structure changed")

    events: list[ScheduleEvent] = []
    unknown_codes: set[str] = set()

    for day_block in soup.select("li.schedule_entry_box"):
        day_node = day_block.select_one("span.md")
        if not day_node:
            continue
        try:
            day = int(plain_text(day_node))
        except ValueError:
            continue
        if not 1 <= day <= 31:
            continue

        for entry in day_block.select("div.list__txt.entry"):
            classes = entry.get("class") or []
            official_code = next(
                (name for name in classes if OFFICIAL_CODE_PATTERN.match(name)),
                None,
            )
            if official_code is None:
                continue

            anchor = entry.find("a", href=True)
            title_node = entry.select_one("p.tit")
            if not anchor or not title_node:
                continue

            url = detail_url(anchor["href"])
            title = plain_text(title_node)
            if not url or not title:
                continue

            if official_code not in CATEGORY_BY_OFFICIAL_CODE:
                unknown_codes.add(official_code)

            events.append(
                ScheduleEvent(
                    id=url.rstrip("/").rsplit("/", 1)[-1] or url,
                    date=ScheduleDate(year=year, month=month, day=day),
                    category=CATEGORY_BY_OFFICIAL_CODE.get(official_code, "other"),
                    title=title,
                    detailURL=url,
                )
            )

    if unknown_codes:
        # Worth shouting about: the site added a category and every event using
        # it is silently landing in "other" until the mapping is updated.
        print(
            f"warning: unmapped category codes {sorted(unknown_codes)}",
            file=sys.stderr,
        )

    events.sort(key=lambda event: (event.date.day, event.id))
    return events


def month_payload(year: int, month: int, events: Iterable[ScheduleEvent]) -> dict:
    return {
        "schemaVersion": SCHEMA_VERSION,
        "generatedAt": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "month": {"year": year, "month": month},
        "events": [asdict(event) for event in events],
    }


def fetch_month(year: int, month: int, timeout: int = 20) -> str:
    import requests

    response = requests.get(
        f"{BASE_URL}/schedule/list/{year}/{month}/",
        headers={
            "User-Agent": "SetouchiTrack/1.0 (schedule publisher)",
            "Accept-Language": "ja-JP,ja;q=0.9",
        },
        timeout=timeout,
    )
    response.raise_for_status()
    return response.text


def load_month(year: int, month: int, fixtures: Path | None) -> str:
    if fixtures is None:
        return fetch_month(year, month)
    return (fixtures / "schedule" / f"{year:04d}-{month:02d}.html").read_text("utf-8")


def verify(fixtures: Path) -> int:
    """Replay the captured pages and check them against the Swift expectations.

    The numbers below are the ones asserted in `ScheduleFixtureTests.swift`; if
    the two implementations ever disagree, one of them has drifted.
    """
    expectations = [
        (2026, 6, 89, 12),
        (2026, 7, 77, 9),
        (2026, 8, 30, 9),
        # A month the site has not scheduled anything for yet. It parses as
        # empty rather than raising; the publish job's own guard is what keeps a
        # month that *used* to have events from being blanked.
        (2026, 11, 0, 0),
    ]

    failures = 0
    for year, month, event_count, performance_count in expectations:
        events = parse_schedule_list(load_month(year, month, fixtures), year, month)
        performances = [e for e in events if e.category == "performance"]

        for label, actual, expected in (
            ("events", len(events), event_count),
            ("performances", len(performances), performance_count),
        ):
            status = "ok  " if actual == expected else "FAIL"
            failures += actual != expected
            print(f"{status} {year}-{month:02d} {label}: {actual} (expected {expected})")

        if len({event.id for event in events}) != len(events):
            print(f"FAIL {year}-{month:02d} duplicate detail ids")
            failures += 1

    festival = next(
        (
            event
            for event in parse_schedule_list(load_month(2026, 8, fixtures), 2026, 8)
            if event.id == "22823"
        ),
        None,
    )
    if festival and festival.date == ScheduleDate(2026, 8, 2):
        print("ok   festival entry sits on 2026-08-02")
    else:
        print(f"FAIL festival entry: {festival}")
        failures += 1

    print("\n" + ("all checks passed" if not failures else f"{failures} check(s) failed"))
    return 1 if failures else 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--verify",
        action="store_true",
        help="replay fixtures and compare against the Swift expectations",
    )
    parser.add_argument(
        "--month",
        metavar="YYYY-MM",
        help="scrape a single month and write it to stdout",
    )
    parser.add_argument(
        "--fixtures",
        nargs="?",
        const=REPOSITORY_ROOT / "fixtures",
        type=Path,
        help="read captured pages from this directory instead of the network",
    )
    args = parser.parse_args()

    if args.verify:
        return verify(args.fixtures or REPOSITORY_ROOT / "fixtures")

    if args.month:
        year, month = (int(part) for part in args.month.split("-"))
        events = parse_schedule_list(load_month(year, month, args.fixtures), year, month)
        json.dump(
            month_payload(year, month, events),
            sys.stdout,
            ensure_ascii=False,
            indent=2,
        )
        print()
        return 0

    parser.print_help()
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
