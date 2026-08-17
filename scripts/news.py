#!/usr/bin/env python3
"""Scrape STU48's official news index into the JSON the app consumes.

Same reasoning as `scrape.py`: parsing the official markup on the device means
a change to sp.stu48.com blanks the section until a release clears review, so
the parsing lives here and the app reads a published payload.

The parsing rules mirror `NewsHTMLParser` in the iOS target. `fixtures/news/`
and the expectations in `NewsFixtureTests.swift` are the shared ground truth
for both implementations; run `--verify` after changing either.
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
NEWS_URL = f"{BASE_URL}/news/"
REPOSITORY_ROOT = Path(__file__).resolve().parent.parent

# How many entries to publish. The app shows one day of headlines and links out
# for the rest, so the whole index is never needed — but the newest day has to
# be complete however busy it was, and the site puts around twenty entries on
# the page. Taking all of them costs nothing and leaves room for a day that
# runs long.
MAX_ITEMS = 40

# Mirrors `NewsCategory.officialCode`. The site encodes the category as a
# `category--N` class whose number matches the `tabN` options in the page's own
# filter menu; the app stores the semantic name, so the mapping lives here and a
# newly introduced code can be handled without a release.
CATEGORY_BY_OFFICIAL_CODE = {
    "2": "announcement",  # お知らせ
    "3": "theater",  # 劇場
    "4": "handshake",  # 握手会
    "5": "event",  # イベント
    "6": "youtube",  # YouTube
    "7": "goods",  # グッズ
    "8": "media",  # メディア
    "9": "release",  # リリース
    "10": "other",  # その他
}

CATEGORY_CLASS_PATTERN = re.compile(r"^category--(\d+)$")
DATE_PATTERN = re.compile(r"^(\d{4})[.\-/](\d{1,2})[.\-/](\d{1,2})$")


class ParseError(RuntimeError):
    """The page did not look like a news index we know how to read."""


@dataclass(frozen=True)
class ScheduleDate:
    year: int
    month: int
    day: int


@dataclass(frozen=True)
class NewsItem:
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


def parse_date(value: str) -> ScheduleDate | None:
    match = DATE_PATTERN.match(value.strip())
    if not match:
        return None
    year, month, day = (int(part) for part in match.groups())
    if not (1 <= month <= 12 and 1 <= day <= 31):
        return None
    return ScheduleDate(year=year, month=month, day=day)


def parse_news_list(html: str) -> list[NewsItem]:
    """Extract every entry from the news index page.

    Entries only count when they sit inside `ul.list--information`. The page
    also renders a category filter carrying the same `N` codes on its options,
    so matching those codes document-wide would pull in menu chrome that is not
    an article.
    """
    soup = BeautifulSoup(html, "lxml")

    listing = soup.select_one("ul.list--information")
    if listing is None:
        # Unlike a month with nothing scheduled, there is no legitimate reason
        # for this page to carry no list: it is the whole point of the URL.
        raise ParseError("no ul.list--information container; page structure changed")

    items: list[NewsItem] = []
    unknown_codes: set[str] = set()
    seen: set[str] = set()

    for entry in listing.select("li"):
        anchor = entry.find("a", href=True)
        category_node = entry.select_one("p.category")
        date_node = entry.select_one("time.date, .date")
        title_node = entry.select_one("p.tit")
        if not anchor or not category_node or not date_node or not title_node:
            continue

        official_code = next(
            (
                match.group(1)
                for name in (category_node.get("class") or [])
                if (match := CATEGORY_CLASS_PATTERN.match(name))
            ),
            None,
        )
        if official_code is None:
            continue

        # `datetime` is the machine-readable copy of the same date; the visible
        # text is the fallback for an entry served without the attribute.
        date = parse_date(date_node.get("datetime") or "") or parse_date(
            plain_text(date_node)
        )
        url = detail_url(anchor["href"])
        title = plain_text(title_node)
        if not date or not url or not title:
            continue

        if official_code not in CATEGORY_BY_OFFICIAL_CODE:
            unknown_codes.add(official_code)

        identifier = url.rstrip("/").rsplit("/", 1)[-1] or url
        if identifier in seen:
            continue
        seen.add(identifier)

        items.append(
            NewsItem(
                id=identifier,
                date=date,
                category=CATEGORY_BY_OFFICIAL_CODE.get(official_code, "other"),
                title=title,
                detailURL=url,
            )
        )

    if unknown_codes:
        # Worth shouting about: the site added a category and every article
        # using it is silently landing in "other" until the mapping is updated.
        print(
            f"warning: unmapped news category codes {sorted(unknown_codes)}",
            file=sys.stderr,
        )

    if not items:
        raise ParseError("list--information held no readable entries")

    # The site already serves newest first, and the app trusts that order to
    # decide which day it is showing. Sorting here rather than relying on it
    # keeps a page that ever comes back shuffled from picking a stale day. The
    # sort is stable, so entries sharing a date keep the order the site gave
    # them — which is the only ordering there is within a day.
    items.sort(
        key=lambda item: (item.date.year, item.date.month, item.date.day),
        reverse=True,
    )
    return items[:MAX_ITEMS]


def news_payload(items: Iterable[NewsItem]) -> dict:
    return {
        "schemaVersion": SCHEMA_VERSION,
        "generatedAt": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "items": [asdict(item) for item in items],
    }


def fetch_news(timeout: int = 20) -> str:
    import requests

    response = requests.get(
        NEWS_URL,
        headers={
            "User-Agent": "SetouchiTrack/1.0 (schedule publisher)",
            "Accept-Language": "ja-JP,ja;q=0.9",
        },
        timeout=timeout,
    )
    response.raise_for_status()
    return response.text


def load_news(fixtures: Path | None) -> str:
    if fixtures is None:
        return fetch_news()
    return (fixtures / "news" / "index.html").read_text("utf-8")


def verify(fixtures: Path) -> int:
    """Replay the captured page and check it against the Swift expectations.

    The numbers below are the ones asserted in `NewsFixtureTests.swift`; if the
    two implementations ever disagree, one of them has drifted.
    """
    items = parse_news_list(load_news(fixtures))

    failures = 0

    def check(label: str, actual, expected) -> None:
        nonlocal failures
        status = "ok  " if actual == expected else "FAIL"
        failures += actual != expected
        print(f"{status} {label}: {actual!r} (expected {expected!r})")

    check("items", len(items), 10)
    check("unique ids", len({item.id for item in items}), len(items))
    check("newest id", items[0].id, "23196")
    check("newest category", items[0].category, "goods")
    check("newest date", items[0].date, ScheduleDate(2026, 8, 17))
    check(
        "newest url",
        items[0].detailURL,
        "https://sp.stu48.com/news/detail/23196",
    )
    check(
        "newest title",
        items[0].title,
        "8月18日（火）STU48「アイドルの夜明け」公演〜奥田唯菜生誕祭〜、会場での物販実施のお知らせ",
    )
    check(
        "entries on 2026-08-17",
        len([i for i in items if i.date == ScheduleDate(2026, 8, 17)]),
        3,
    )
    check(
        "categories present",
        sorted({item.category for item in items}),
        ["announcement", "event", "goods", "theater"],
    )
    check(
        "newest first",
        items == sorted(
            items,
            key=lambda i: (i.date.year, i.date.month, i.date.day),
            reverse=True,
        ),
        True,
    )

    print("\n" + ("all checks passed" if not failures else f"{failures} check(s) failed"))
    return 1 if failures else 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--verify",
        action="store_true",
        help="replay the fixture and compare against the Swift expectations",
    )
    parser.add_argument(
        "--fixtures",
        nargs="?",
        const=REPOSITORY_ROOT / "fixtures",
        type=Path,
        help="read the captured page from this directory instead of the network",
    )
    args = parser.parse_args()

    if args.verify:
        return verify(args.fixtures or REPOSITORY_ROOT / "fixtures")

    items = parse_news_list(load_news(args.fixtures))
    json.dump(news_payload(items), sys.stdout, ensure_ascii=False, indent=2)
    print()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
