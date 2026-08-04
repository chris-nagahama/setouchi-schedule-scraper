#!/usr/bin/env python3
"""Parse STU48 performance detail pages into the JSON the app consumes.

Mirrors `SchedulePerformanceHTMLParser` in the iOS target. Unlike the month
list, these pages carry no useful structure below the article body: the fields
are `■`-prefixed labels inside free text, so this works on lines of stripped
text rather than a DOM, exactly as the Swift parser does.

The expectations in `ScheduleFixtureTests.swift` are the spec. Run `--verify`
after changing either implementation.
"""

from __future__ import annotations

import argparse
import html as html_module
import json
import re
import sys
from dataclasses import asdict, dataclass, field, replace
from datetime import datetime, timezone
from pathlib import Path

import venues

SCHEMA_VERSION = 1

# Bump when a detail payload's shape changes — a field added, renamed or
# dropped — or when a fix here makes the parsers read the same page
# differently. The publisher refetches a detail page whose event moved or whose
# page still looks unannounced, and neither notices that the reading of an
# unchanged page has changed, so without this marker a corrected parser would
# only reach pages that happen to move afterwards. Distinct from
# SCHEMA_VERSION, which is the contract with the app; this is only how the
# publisher decides what to rebuild.
PAYLOAD_REVISION = 3

BASE_URL = "https://sp.stu48.com"
REPOSITORY_ROOT = Path(__file__).resolve().parent.parent

DOTALL_I = re.S | re.I

# Labels vary by event type: theater shows use ■出演, tours use ■出演メンバー,
# and festivals switch to an ■イベント… vocabulary entirely.
VENUE_LABELS = ("■会場", "会場・", "会場：", "会場:")
CAST_LABELS = ("■出演メンバー", "■出演メンバー情報", "■出演")
TIME_LABELS = ("■開場/開演", "■開場／開演")
DATE_LABELS = ("■公演日",)
TITLE_LABELS = ("■公演タイトル",)
# A tour announcement with no ■会場 label lists its stops under one of these.
SCHEDULE_HEADINGS = ("スケジュール", "日程")

# A labelled value stops at the next label, a bracketed heading, a footnote, a
# horizontal rule of underscores, or the ticket section — the price block
# repeats other shows' titles behind its own ■ lines.
VALUE_TERMINATORS = ("■", "＜", "※")

# The bracketed heading, on its own. It ends a value everywhere except inside a
# cast: a festival that plays more than one stage heads each group with one —
# ＜HOTSTAGE＞, the names, then the next stage — and stopping there read the
# whole cast as absent. Every other ＜…＞ on these pages opens an unrelated
# section (＜料金＞, ＜必ずお読みください＞, ＜学生チケットについて＞) and follows
# prose or a ※ line rather than a label, so a terminator above is always
# reached first.
GROUP_HEADING = "＜"

# What the pages put between two names. Four are middle dots that render almost
# identically: the Japanese ・ most pages use, its half-width form, and the
# Latin · and • that arrive when a listing is pasted in from elsewhere. A cast
# written with one of the latter used to come out as a single name.
MEMBER_SEPARATORS = "・･·•、,，／/"
MEMBER_JOIN = "・"

# A label can carry a qualifier where a value would go: ■出演メンバー(昼夜公演共通)
# names nobody. What survives removing bracketed runs tells the two apart.
QUALIFIER = re.compile(r"[（(【\[〔][^）)】\]〕]*[）)】\]〕]")


class ParseError(RuntimeError):
    """The page did not look like a detail page we know how to read."""


@dataclass(frozen=True)
class ScheduleDate:
    year: int
    month: int
    day: int


@dataclass(frozen=True)
class TimeSlot:
    label: str | None
    openingTime: str | None
    startTime: str | None


@dataclass(frozen=True)
class Occurrence:
    id: str
    title: str
    date: ScheduleDate
    openingTime: str | None
    startTime: str | None
    venue: str | None
    memberNames: list[str] = field(default_factory=list)
    # Filled by with_venue_locations, not by the parser. Nothing on the page
    # states a coordinate, and keeping it out of parse_detail is what lets the
    # fixture expectations stay the shared spec with the Swift parser, which
    # has no table to consult.
    venueLocation: dict | None = None


@dataclass(frozen=True)
class FallbackEvent:
    id: str
    title: str
    date: ScheduleDate


# ---------------------------------------------------------------- text helpers


def decode_entities(source: str) -> str:
    return html_module.unescape(source)


def plain_inline_text(source: str) -> str:
    stripped = re.sub(r"<[^>]+>", " ", source)
    return re.sub(r"\s+", " ", decode_entities(stripped)).strip()


def plain_lines(source: str) -> list[str]:
    value = source
    for pattern in (r"<br\s*/?>", r"</p\s*>", r"</div\s*>", r"</li\s*>", r"</h[1-6]\s*>"):
        value = re.sub(pattern, "\n", value, flags=DOTALL_I)
    value = re.sub(r"<[^>]+>", "", value)
    value = decode_entities(value).replace(" ", " ")

    lines = []
    for line in value.split("\n"):
        # Full-width spaces are folded here, which is why the cast splitter
        # downstream reasons about plain spaces.
        collapsed = re.sub(r"[ \t　]+", " ", line).strip()
        if collapsed:
            lines.append(collapsed)
    return lines


def first_capture(pattern: str, source: str) -> str | None:
    match = re.search(pattern, source, DOTALL_I)
    return match.group(1) if match else None


def parse_date(source: str, fallback_year: int) -> ScheduleDate | None:
    match = re.search(r"(20\d{2})\s*[年./]\s*(\d{1,2})\s*[月./]\s*(\d{1,2})", source)
    if match:
        return ScheduleDate(*(int(group) for group in match.groups()))

    match = re.search(r"(?<!\d)(\d{1,2})\s*[/月]\s*(\d{1,2})(?:日)?", source)
    if match:
        return ScheduleDate(fallback_year, int(match.group(1)), int(match.group(2)))

    return None


def first_date(lines: list[str], fallback: ScheduleDate) -> ScheduleDate:
    for line in lines:
        parsed = parse_date(line, fallback.year)
        if parsed:
            return parsed
    return fallback


# --------------------------------------------------------------- label lookup


def is_label_line(line: str, labels: tuple[str, ...]) -> bool:
    trimmed = line.strip()
    return any(trimmed.startswith(label) for label in labels)


def inline_value(line: str, label: str) -> str | None:
    """The value written on the label line itself, if it holds one.

    A label may carry a qualifier instead — ■出演メンバー(昼夜公演共通) names
    nobody, and reading it as a value listed that note as a member. A real value
    that merely ends in a parenthetical still counts, because something is left
    of it once the brackets come out.
    """
    value = line.strip()[len(label):].strip("：: ・\t　")
    if not value:
        return None
    return value if QUALIFIER.sub("", value).strip() else None


def value_at(index: int, labels: tuple[str, ...], lines: list[str]) -> str | None:
    line = lines[index].strip()
    label = next((label for label in labels if line.startswith(label)), None)
    if label is None:
        return None

    inline = inline_value(line, label)
    if inline:
        return inline

    return lines[index + 1] if index + 1 < len(lines) else None


def first_labeled_values(
    labels: tuple[str, ...],
    start: int,
    end: int,
    lines: list[str],
    skip_group_headings: bool = False,
) -> list[str] | None:
    label_index = next(
        (i for i in range(start, end) if is_label_line(lines[i], labels)),
        None,
    )
    if label_index is None:
        return None

    source = lines[label_index].strip()
    matched = next((label for label in labels if source.startswith(label)), None)
    values: list[str] = []

    if matched:
        inline = inline_value(source, matched)
        if inline:
            values.append(inline)

    index = label_index + 1
    while index < end and len(values) < 4:
        line = lines[index]
        if line.startswith(GROUP_HEADING):
            # See GROUP_HEADING: only a cast reads these as part of the block.
            if not skip_group_headings:
                break
            index += 1
            continue
        if (
            any(line.startswith(prefix) for prefix in VALUE_TERMINATORS)
            or "_____" in line
            or "チケットに関して" in line
        ):
            break
        values.append(line)
        index += 1

    return values or None


def first_labeled_value(
    labels: tuple[str, ...],
    start: int,
    end: int,
    lines: list[str],
) -> str | None:
    values = first_labeled_values(labels, start, end, lines)
    return values[0] if values else None


def last_labeled_value(
    labels: tuple[str, ...],
    start: int,
    end: int,
    lines: list[str],
) -> str | None:
    for index in reversed(range(start, end)):
        if is_label_line(lines[index], labels):
            value = value_at(index, labels, lines)
            if value:
                return value
    return None


# -------------------------------------------------------------- field parsing


def time_slots(source: str) -> list[TimeSlot]:
    """Read opening/start pairs, tolerating the four layouts seen in the wild.

    `11:45:開場/12:30開演`, `16:45開場/17:30開演`, `1部11:00 /11:30` and a bare
    `12:45 /13:30` all appear across captured pages.
    """
    slots: list[TimeSlot] = []

    pair = re.compile(
        r"(?:(\d+\s*部)\s*)?(\d{1,2}:\d{2})\s*(?::?\s*開場)?\s*[/／]\s*"
        r"(\d{1,2}:\d{2})\s*(?:開演)?"
    )
    for match in pair.finditer(source):
        label, opening, start = match.groups()
        slots.append(
            TimeSlot(
                label=re.sub(r"\s+", "", label) if label else None,
                openingTime=opening,
                startTime=start,
            )
        )

    if not slots:
        explicit = re.compile(
            r"(\d{1,2}:\d{2})\s*:?\s*開場\s*[/／]\s*(\d{1,2}:\d{2})\s*開演"
        )
        slots = [
            TimeSlot(label=None, openingTime=opening, startTime=start)
            for opening, start in explicit.findall(source)
        ]

    if not slots:
        start = first_capture(r"(\d{1,2}:\d{2})\s*開演", source)
        if start:
            slots = [TimeSlot(label=None, openingTime=None, startTime=start)]

    if not slots:
        opening = first_capture(r"(\d{1,2}:\d{2})\s*:?\s*開場", source)
        if opening:
            slots = [TimeSlot(label=None, openingTime=opening, startTime=None)]

    unique: list[TimeSlot] = []
    for slot in slots:
        if slot not in unique:
            unique.append(slot)
    return unique


def split_member_names(source: str, separators: str) -> list[str]:
    parts = re.split(f"[{re.escape(separators)}]", source)
    names = []
    for part in parts:
        name = re.sub(r"\s+", " ", part).strip()
        if not name or name.startswith("※") or "改めて" in name or "発表" in name:
            continue
        names.append(name)
    return names


def member_names(source: str) -> list[str]:
    names: list[str] = []
    for run in split_member_names(source, MEMBER_SEPARATORS):
        # Festival pages separate the cast with spaces instead of punctuation. A
        # space also separates a surname from a given name, so a run is only
        # read as a list once it holds more spaces than one person's name would.
        # Per run rather than over the whole cast: a festival that groups its
        # stages gives one space-separated run for each of them, alongside runs
        # naming a single member.
        if run.count(" ") >= 2:
            names.extend(split_member_names(run, " "))
        else:
            names.append(run)

    # Those stage groups name again whoever appears in more than one, and the
    # app shows the cast as one flat list.
    unique: list[str] = []
    seen: set[str] = set()
    for name in names:
        key = name.replace(" ", "")
        if key not in seen:
            seen.add(key)
            unique.append(name)
    return unique


def clean_venue(source: str) -> str:
    return source.strip("・：: \n\t　")


def schedule_block_range(start: int, end: int, lines: list[str]) -> tuple[int, int] | None:
    """The span of a "■【…】スケジュール" listing, if the page has one."""
    heading = next(
        (
            index
            for index in range(start, end)
            if lines[index].startswith("■")
            and any(marker in lines[index] for marker in SCHEDULE_HEADINGS)
        ),
        None,
    )
    if heading is None:
        return None

    block_start = heading + 1
    block_end = next(
        (index for index in range(block_start, end) if lines[index].startswith("■")),
        end,
    )
    return (block_start, block_end) if block_start < block_end else None


def is_venue_line(line: str, fallback_year: int) -> bool:
    return (
        not line.startswith(VALUE_TERMINATORS)
        and parse_date(line, fallback_year) is None
    )


def schedule_block_venue(
    date: ScheduleDate,
    start: int,
    end: int,
    lines: list[str],
) -> str | None:
    """The venue a tour announcement lists under this occurrence's own date.

    A tour announced before its dates get their own detail pages lists every
    stop under a schedule heading, as a date line followed by a venue line, and
    carries no ■会場 label at all.

    Confined to that block on purpose: prose elsewhere on these pages mentions
    dates too, and the line after one of those is a sentence, not a venue.
    """
    block = schedule_block_range(start, end, lines)
    if block is None:
        return None

    block_start, block_end = block
    for index in range(block_start, block_end):
        if parse_date(lines[index], date.year) != date:
            continue
        venue_index = index + 1
        if venue_index < block_end and is_venue_line(lines[venue_index], date.year):
            return lines[venue_index]
    return None


def title_marks_stage(title: str, label: str) -> bool:
    """Whether a title already announces the stage a time slot belongs to.

    Titles carry the marker bare (`1部 STU48 Live`) or decorated
    (`【1部】STU48 Live`), so the decoration is stripped before comparing;
    otherwise the label gets prepended a second time.
    """
    return title.startswith(label) or title.strip("【】[]〔〕（）() 　").startswith(label)


# ------------------------------------------------------------------ assembling


def make_occurrences(
    id_prefix: str,
    title: str,
    date: ScheduleDate,
    start: int,
    end: int,
    lines: list[str],
) -> list[Occurrence]:
    venue_value = first_labeled_value(VENUE_LABELS, start, end, lines) or (
        schedule_block_venue(date, start, end, lines)
    )
    venue = clean_venue(venue_value) if venue_value else None

    cast_values = first_labeled_values(
        CAST_LABELS, start, end, lines, skip_group_headings=True
    )
    cast = member_names(MEMBER_JOIN.join(cast_values)) if cast_values else []

    time_values = first_labeled_values(TIME_LABELS, start, end, lines)
    time_source = " ".join(time_values) if time_values else " ".join(lines[start:end])
    slots = time_slots(time_source) or [TimeSlot(None, None, None)]

    occurrences = []
    for index, slot in enumerate(slots):
        if slot.label and not title_marks_stage(title, slot.label):
            occurrence_title = f"{slot.label} {title}"
        else:
            occurrence_title = title

        occurrences.append(
            Occurrence(
                id=f"{id_prefix}-{index}",
                title=occurrence_title,
                date=date,
                openingTime=slot.openingTime,
                startTime=slot.startTime,
                venue=venue,
                memberNames=cast,
            )
        )
    return occurrences


def parse_detail(html: str, fallback: FallbackEvent) -> list[Occurrence]:
    if not re.search(r"section--detail", html, DOTALL_I):
        raise ParseError("no section--detail container; page structure changed")

    title_html = first_capture(
        r"<p\b[^>]*class\s*=\s*[\"'][^\"']*\btit\b[^\"']*[\"'][^>]*>(.*?)</p>", html
    )
    page_title = plain_inline_text(title_html) if title_html else fallback.title

    header_html = first_capture(
        r"<time\b[^>]*class\s*=\s*[\"'][^\"']*\bdate\b[^\"']*\bevent\b[^\"']*[\"'][^>]*>"
        r"(.*?)</time>",
        html,
    )
    header_date_text = plain_inline_text(header_html) if header_html else None
    header_date = (
        parse_date(header_date_text, fallback.date.year) if header_date_text else None
    ) or fallback.date

    body_html = first_capture(
        r"<div\b[^>]*class\s*=\s*[\"'][^\"']*\btxt\b[^\"']*[\"'][^>]*>(.*?)"
        r"</div>\s*<!--\s*/?\s*txt\s*-->",
        html,
    )
    if body_html is None:
        return [
            Occurrence(
                id=f"{fallback.id}-fallback",
                title=fallback.title,
                date=fallback.date,
                openingTime=None,
                startTime=None,
                venue=None,
                memberNames=[],
            )
        ]

    lines = plain_lines(body_html)
    date_markers = [i for i in range(len(lines)) if is_label_line(lines[i], DATE_LABELS)]

    if not date_markers:
        # The header states the day the group appears. Body text may open with
        # an unrelated date — a festival lists its whole run before naming the
        # group's slot — so scanning it is only a last resort for pages that
        # ship no header date at all.
        date = header_date if header_date_text else first_date(lines, header_date)
        return make_occurrences(
            id_prefix=fallback.id,
            # The page heading names the event. Body prose was preferred here
            # once, but an unannounced date opens with a sentence mentioning
            # 公演 ("公演詳細…は後日お知らせ") that outranked the real title, and
            # no captured page benefits from the scan.
            title=page_title,
            date=date,
            start=0,
            end=len(lines),
            lines=lines,
        )

    occurrences: list[Occurrence] = []
    for offset, marker in enumerate(date_markers):
        previous = 0 if offset == 0 else date_markers[offset - 1] + 1
        following = date_markers[offset + 1] if offset + 1 < len(date_markers) else len(lines)

        title = last_labeled_value(TITLE_LABELS, previous, marker, lines) or page_title
        date_text = value_at(marker, DATE_LABELS, lines)
        date = (parse_date(date_text, header_date.year) if date_text else None) or header_date

        occurrences.extend(
            make_occurrences(
                id_prefix=f"{fallback.id}-{offset}",
                title=title,
                date=date,
                start=marker,
                end=following,
                lines=lines,
            )
        )

    return occurrences


# ------------------------------------------------------------------------ I/O


def with_venue_locations(occurrences: list[Occurrence]) -> list[Occurrence]:
    """Attach the coordinate the app pins, for the venues the table knows.

    A venue the table does not cover keeps `venueLocation` null and the app
    falls back to searching for it by name. `venues.py --unlisted` reports
    those; see venues.json for why this is a table and not a geocoder call.
    """
    located = []
    for occurrence in occurrences:
        coordinate = venues.coordinate_for(occurrence.venue)
        located.append(
            replace(
                occurrence,
                venueLocation=(
                    {"latitude": coordinate.latitude, "longitude": coordinate.longitude}
                    if coordinate
                    else None
                ),
            )
        )
    return located


def detail_payload(event_id: str, occurrences: list[Occurrence]) -> dict:
    return {
        "schemaVersion": SCHEMA_VERSION,
        "generatedAt": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "id": event_id,
        "occurrences": [
            asdict(occurrence) for occurrence in with_venue_locations(occurrences)
        ],
    }


def fetch_detail(event_id: str, timeout: int = 20) -> str:
    import requests

    response = requests.get(
        f"{BASE_URL}/schedule/detail/{event_id}",
        headers={
            "User-Agent": "SetouchiTrack/1.0 (schedule publisher)",
            "Accept-Language": "ja-JP,ja;q=0.9",
        },
        timeout=timeout,
    )
    response.raise_for_status()
    return response.text


def load_detail(event_id: str, fixtures: Path | None) -> str:
    if fixtures is None:
        return fetch_detail(event_id)
    return (fixtures / "performance" / f"{event_id}.html").read_text("utf-8")


# --------------------------------------------------------------- verification

# Transcribed from `ScheduleFixtureTests.DetailExpectation`.
EXPECTATIONS = {
    "22820": [
        {
            "title": "STU48「アイドルの夜明け」公演〜宗雪里香生誕祭〜",
            "date": ScheduleDate(2026, 7, 11),
            "openingTime": "11:45",
            "startTime": "12:30",
            "venue": "STU48広島劇場(広島クラブクアトロ)",
            "memberCount": 8,
            "firstMember": "内海 里音",
            "lastMember": "渡辺 菜月",
        }
    ],
    "22821": [
        {
            "title": "STU48「アイドルの夜明け」公演〜原田清花生誕祭〜",
            "date": ScheduleDate(2026, 7, 11),
            "openingTime": "16:45",
            "startTime": "17:30",
            "venue": "STU48広島劇場(広島クラブクアトロ)",
            "memberCount": 8,
            "firstMember": "池田 裕楽",
            "lastMember": "渡辺 菜月",
        }
    ],
    "22482": [
        {
            "title": "【1部】STU48 Live Tour 2026",
            "date": ScheduleDate(2026, 6, 27),
            "openingTime": "11:00",
            "startTime": "11:30",
            "venue": "香川県・高松festhalle",
            "memberCount": 16,
            "firstMember": "石原 侑奈",
            "lastMember": "藤田 愛結",
        }
    ],
    "22692": [
        {
            "title": "STU48 Live Tour 2026",
            "date": ScheduleDate(2026, 7, 19),
            "openingTime": "12:45",
            "startTime": "13:30",
            "venue": "東京都・品川インターシティホール",
            "memberCount": 16,
            "firstMember": "奥田 唯菜",
            "lastMember": "髙村 栞里",
        },
        {
            "title": "STU48 Live Tour 2026",
            "date": ScheduleDate(2026, 7, 19),
            "openingTime": "17:15",
            "startTime": "18:00",
            "venue": "東京都・品川インターシティホール",
            "memberCount": 16,
            "firstMember": "岡田 あずみ",
            "lastMember": "吉田 彩良",
        },
    ],
    "22802": [
        {
            "title": "STU48 Live Tour 2026",
            "date": ScheduleDate(2026, 9, 1),
            "openingTime": None,
            "startTime": None,
            # No ■会場 label on this page; the venue is under the schedule
            # heading, on the line below its date.
            "venue": "宮城県・仙台Rensa",
            "memberCount": 0,
            "firstMember": None,
            "lastMember": None,
        }
    ],
    # A festival: a different label vocabulary, a multi-day run where only one
    # day is STU48's, and a cast separated by full-width spaces.
    "22876": [
        {
            "title": "SKY ART FESTIVAL 2026",
            "date": ScheduleDate(2026, 8, 1),
            "openingTime": None,
            "startTime": None,
            "venue": "白良浜海水浴場",
            "memberCount": 7,
            "firstMember": "新井梨杏",
            "lastMember": "吉田彩良",
        }
    ],
    # The same shape after it grew a second and third stage: the cast is split
    # into ＜…＞ groups, one per stage, and whoever plays more than one is named
    # again under each.
    "22823": [
        {
            "title": "TOKYO IDOL FESTIVAL 2026 supported by にしたんクリニック",
            "date": ScheduleDate(2026, 8, 2),
            "openingTime": None,
            "startTime": None,
            "venue": "お台場・⻘海周辺エリア",
            "memberCount": 16,
            "firstMember": "新井梨杏",
            "lastMember": "曽我部あこ",
        }
    ],
    # A cast separated by · (U+00B7) rather than the ・ the rest of the site
    # uses, under a label carrying a qualifier where its value would go.
    "22801": [
        {
            "title": "STU48 Live Tour 2026",
            "date": ScheduleDate(2026, 8, 31),
            "openingTime": None,
            "startTime": "18:30",
            "venue": "北海道·cube garden",
            "memberCount": 16,
            "firstMember": "石原 侑奈",
            "lastMember": "吉田 彩良",
        }
    ],
    # The same qualifier on a page that separates its cast normally, which pins
    # the qualifier down on its own.
    "22803": [
        {
            "title": "STU48 Live Tour 2026",
            "date": ScheduleDate(2026, 9, 12),
            "openingTime": None,
            "startTime": None,
            "venue": "愛知県・名古屋ReNY limited",
            "memberCount": 17,
            "firstMember": "内海 里音",
            "lastMember": "渡辺 菜月",
        }
    ],
}


def verify(fixtures: Path) -> int:
    failures = 0

    for event_id, expected_list in EXPECTATIONS.items():
        fallback = FallbackEvent(
            id=event_id,
            title=f"fallback-{event_id}",
            date=ScheduleDate(2026, 1, 1),
        )
        occurrences = parse_detail(load_detail(event_id, fixtures), fallback)

        if len(occurrences) != len(expected_list):
            print(
                f"FAIL {event_id} occurrences: {len(occurrences)} "
                f"(expected {len(expected_list)})"
            )
            failures += 1
            continue

        for index, (actual, expected) in enumerate(zip(occurrences, expected_list)):
            checks = [
                ("title", actual.title, expected["title"]),
                ("date", actual.date, expected["date"]),
                ("opening", actual.openingTime, expected["openingTime"]),
                ("start", actual.startTime, expected["startTime"]),
                ("venue", actual.venue, expected["venue"]),
                ("members", len(actual.memberNames), expected["memberCount"]),
                (
                    "first member",
                    actual.memberNames[0] if actual.memberNames else None,
                    expected["firstMember"],
                ),
                (
                    "last member",
                    actual.memberNames[-1] if actual.memberNames else None,
                    expected["lastMember"],
                ),
            ]
            bad = [(name, got, want) for name, got, want in checks if got != want]
            if bad:
                failures += len(bad)
                for name, got, want in bad:
                    print(f"FAIL {event_id}[{index}] {name}: {got!r} (expected {want!r})")
            else:
                print(f"ok   {event_id}[{index}] all fields match")

    print("\n" + ("all checks passed" if not failures else f"{failures} check(s) failed"))
    return 1 if failures else 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--verify", action="store_true")
    parser.add_argument("--id", metavar="EVENT_ID", help="scrape one detail page")
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

    if args.id:
        fallback = FallbackEvent(
            id=args.id,
            title=f"fallback-{args.id}",
            date=ScheduleDate(2026, 1, 1),
        )
        occurrences = parse_detail(load_detail(args.id, args.fixtures), fallback)
        json.dump(
            detail_payload(args.id, occurrences),
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
