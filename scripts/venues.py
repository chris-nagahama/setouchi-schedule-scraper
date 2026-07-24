#!/usr/bin/env python3
"""Resolve a venue name from the official pages to a coordinate.

The app cannot look these up itself. Apple's map service follows the device's
region, and on a China-region device it covers mainland China only: measured
there, "品川インターシティホール" returns no result at all and "高松festhalle"
returns a factory in Ningbo. So the publisher says where the venue is, and the
app pins what it is told.

The table lives in `venues.json`, curated by hand — see the comment at the top
of it for why, and for what to record when adding an entry. A venue that is not
in it publishes without a coordinate, which is not a failure: the app falls
back to searching by name, and that still works for a device in Japan.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
import unicodedata
from dataclasses import dataclass
from pathlib import Path

TABLE_PATH = Path(__file__).resolve().parent / "venues.json"

# Japan corner to corner: Yonaguni south-west, the Nemuro peninsula north-east,
# the Ogasawara islands out east. Mirrors the bounding box the app filters its
# own search results with.
JAPAN_LATITUDES = (20.0, 46.5)
JAPAN_LONGITUDES = (122.0, 154.0)

# The pages write the prefecture as a prefix off the venue's own name, with a
# separator: "香川県・高松festhalle". The separator is required, because a venue
# can begin with a prefecture name in its own right — 広島県民文化センター is
# the 広島県民 culture centre, not 広島県 + 民文化センター.
PREFECTURE_PREFIX = re.compile(r"^(?:東京都|北海道|(?:京都|大阪)府|.{2,3}県)[・\s]+")

# Radicals the official pages use where the ideograph belongs. NFKC does not
# fold these: the CJK Radicals Supplement block (U+2E80–U+2EFF) carries no
# compatibility decomposition, unlike the Kangxi Radicals block above it. Kept
# in step with `VenueSearchText` in the iOS target.
RADICAL_IDEOGRAPHS = {
    "⻘": "青",  # CJK RADICAL BLUE, seen in "お台場・⻘海周辺エリア"
}


class VenueTableError(RuntimeError):
    """The table itself is wrong, which is worth failing a run over."""


@dataclass(frozen=True)
class Coordinate:
    latitude: float
    longitude: float


def normalize(name: str) -> str:
    """A venue name folded to what two spellings of it have in common.

    Width, case, the stand-in radicals, and the spaces that come and go between
    the words. Deliberately not the prefecture prefix: this is what a table key
    is built with, and stripping there would file 広島県民文化センター under
    民文化センター. Variants are the caller's business — see lookup_keys.
    """
    mapped = "".join(RADICAL_IDEOGRAPHS.get(character, character) for character in name)
    folded = unicodedata.normalize("NFKC", mapped).casefold()
    return re.sub(r"[\s　]+", "", folded.strip())


def lookup_keys(name: str) -> list[str]:
    """Every key one venue string from the pages should be allowed to match.

    The pages prefix the prefecture — "香川県・高松festhalle" for the room the
    table calls 高松festhalle — and name a room by its operator's name for it as
    well, "STU48広島劇場(広島クラブクアトロ)". Any of those should find the
    entry, and the table should not have to carry every combination.
    """
    variants = [name]

    match = re.search(r"[（(]([^）)]*)[）)]", name)
    if match:
        variants.append(name[: match.start()] + name[match.end() :])
        variants.append(match.group(1))

    keys = []
    for variant in variants:
        stripped = variant.strip()
        if stripped:
            keys.append(normalize(stripped))
            keys.append(normalize(PREFECTURE_PREFIX.sub("", stripped)))

    return [key for index, key in enumerate(keys) if key and key not in keys[:index]]


def load_table(path: Path = TABLE_PATH) -> dict[str, Coordinate]:
    raw = json.loads(path.read_text("utf-8"))
    table: dict[str, Coordinate] = {}
    origins: dict[str, str] = {}

    for entry in raw["venues"]:
        latitude = float(entry["latitude"])
        longitude = float(entry["longitude"])

        if not JAPAN_LATITUDES[0] <= latitude <= JAPAN_LATITUDES[1] or (
            not JAPAN_LONGITUDES[0] <= longitude <= JAPAN_LONGITUDES[1]
        ):
            raise VenueTableError(
                f"{entry['names'][0]}: {latitude},{longitude} is outside Japan"
            )
        if not entry.get("address") or not entry.get("source"):
            raise VenueTableError(
                f"{entry['names'][0]}: every entry records its address and source"
            )

        for name in entry["names"]:
            key = normalize(name)
            if key in table:
                raise VenueTableError(
                    f"{name} and {origins[key]} both normalise to {key!r}"
                )
            table[key] = Coordinate(latitude, longitude)
            origins[key] = name

    return table


_TABLE: dict[str, Coordinate] | None = None


def coordinate_for(venue: str | None) -> Coordinate | None:
    global _TABLE
    if venue is None:
        return None
    if _TABLE is None:
        _TABLE = load_table()

    for key in lookup_keys(venue):
        found = _TABLE.get(key)
        if found:
            return found
    return None


# --------------------------------------------------------------- verification


def verify() -> int:
    """Check the table loads and that its own names find their entries.

    load_table does the real work; this reports it, and catches an entry whose
    name normalises to something its own lookup would never produce.
    """
    try:
        table = load_table()
    except (VenueTableError, KeyError, ValueError) as error:
        print(f"FAIL venues.json: {error}")
        return 1

    raw = json.loads(TABLE_PATH.read_text("utf-8"))
    failures = 0
    for entry in raw["venues"]:
        for name in entry["names"]:
            found = coordinate_for(name)
            if found is None:
                print(f"FAIL {name}: not found by its own name")
                failures += 1
            elif (found.latitude, found.longitude) != (
                float(entry["latitude"]),
                float(entry["longitude"]),
            ):
                print(f"FAIL {name}: resolves to another entry")
                failures += 1

    print(f"ok   {len(table)} name(s) over {len(raw['venues'])} venue(s)")
    print("\n" + ("all checks passed" if not failures else f"{failures} check(s) failed"))
    return 1 if failures else 0


def report_unlisted(published: Path) -> int:
    """Name the venues the published tree uses that the table does not cover.

    How a new tour stop gets noticed. Never fails the run: a missing coordinate
    degrades to a name search in the app, it does not break anything.
    """
    unlisted: dict[str, int] = {}
    for path in sorted((published / "v1" / "performance").glob("*.json")):
        payload = json.loads(path.read_text("utf-8"))
        for occurrence in payload.get("occurrences", []):
            venue = occurrence.get("venue")
            if venue and coordinate_for(venue) is None:
                unlisted[venue] = unlisted.get(venue, 0) + 1

    if not unlisted:
        print("every published venue has a coordinate")
        return 0

    print("venues without a coordinate, most used first:")
    for venue, count in sorted(unlisted.items(), key=lambda item: -item[1]):
        print(f"  {count:3d}  {venue}")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--verify", action="store_true")
    parser.add_argument(
        "--unlisted",
        type=Path,
        nargs="?",
        const=Path("published"),
        help="report venues in a published tree that the table does not cover",
    )
    parser.add_argument("name", nargs="*", help="look a venue name up")
    args = parser.parse_args()

    if args.verify:
        return verify()
    if args.unlisted:
        return report_unlisted(args.unlisted)

    for name in args.name:
        found = coordinate_for(name)
        print(f"{name}: {found.latitude},{found.longitude}" if found else f"{name}: —")
    return 0


if __name__ == "__main__":
    sys.exit(main())
