#!/usr/bin/env python3
"""Bring the catalog up to date and package it for download.

Run daily by ``.github/workflows/update-catalog.yml``; runnable by hand for
the same result.

Three things about this script are deliberate:

* **It updates, it does not crawl.** The catalog in this repository is the
  starting point, and SMWCentral answers newest-first, so the walk stops at
  the first page holding nothing new. A normal day is one request. Only
  ``--full`` walks all ~200 pages, and there is no reason to do that unless an
  entry changed retroactively.
* **The file it writes is one entry per line.** It is still ordinary JSON —
  the player reads it unchanged — but a day's update shows up in ``git diff``
  as the handful of lines it really is, instead of one 6 MB line.
* **Nothing is written when nothing changed.** No commit and no new checksum
  on a quiet day. The artifact is still packaged, so the caller always has
  something to publish — the first run finds nothing new and still has to
  produce the download that does not exist yet.

``raw_description`` is dropped on write. SMWCentral sends both a cleaned and a
raw description; they are character-identical for half the catalog, together
they are 58% of the file, and the player only searches the cleaned one.
"""

from __future__ import annotations

import argparse
import gzip
import hashlib
import json
import os
import sys
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
sys.path.insert(0, str(ROOT / "vendor"))

import requests  # noqa: E402  (after the vendor path is set up)

from music_api import MusicAPI  # noqa: E402

#: Bumped when the shape of the file changes in a way an older player cannot
#: read. The player checks it before installing.
SCHEMA_VERSION = 1

#: Default paths, relative to the repository root.
CATALOG_PATH = ROOT / "catalog" / "smwc_catalog.json"
OUTPUT_DIR = ROOT / "dist"

#: Name of the published artifact. Rolling, not versioned: the manifest
#: carries the checksum, and a daily file would leave 365 dead assets a year.
ARTIFACT_NAME = "smwc-catalog.json.gz"

#: Field carried by the API that the artifact does not need.
DROPPED_FIELDS = ("raw_description",)

#: Seconds between requests. Slower than the player's own default — a pipeline
#: is not a person waiting, and this runs against someone else's server.
DEFAULT_DELAY = 1.5

USER_AGENT = (
    "SaphrosGameMusicPlayer-CatalogBot/1.0 "
    "(+https://github.com/{repo}; daily incremental metadata sync)"
)


# ---------------------------------------------------------------------------
# Reading and writing the catalog
# ---------------------------------------------------------------------------


def load_catalog(path: Path) -> List[Dict[str, Any]]:
    """Entries currently in the repository, or ``[]`` on the first run."""
    if not path.exists():
        return []
    with open(path, "r", encoding="utf-8") as handle:
        data = json.load(handle)
    entries = data.get("entries") if isinstance(data, dict) else None
    return list(entries or [])


def clean(entry: Dict[str, Any]) -> Dict[str, Any]:
    """One entry as it is published."""
    return {k: v for k, v in entry.items() if k not in DROPPED_FIELDS}


def render(entries: List[Dict[str, Any]], sync_time: float) -> str:
    """The catalog file's text: ordinary JSON, one entry to a line."""
    body = ",\n".join(
        json.dumps(clean(entry), ensure_ascii=False, separators=(",", ":"),
                   sort_keys=True)
        for entry in entries
    )
    head = (
        f'{{"schema_version":{SCHEMA_VERSION},\n'
        f'"sync_time":{json.dumps(sync_time)},\n'
        f'"total_entries":{len(entries)},\n'
        f'"entries":[\n'
    )
    return f"{head}{body}\n]}}\n"


def entry_block(text: str) -> str:
    """The part of a catalog file that is the entries.

    Compared instead of the whole file so a rebuilt timestamp on an otherwise
    identical catalog does not read as a change.
    """
    start = text.find('"entries":[')
    return text[start:] if start >= 0 else text


# ---------------------------------------------------------------------------
# Fetching
# ---------------------------------------------------------------------------


def fetch(known_ids: set, delay: float, full: bool, repo: str) -> List[Dict[str, Any]]:
    """New entries from SMWCentral, newest first."""
    session = requests.Session()
    session.headers["User-Agent"] = USER_AGENT.format(repo=repo)
    api = MusicAPI(delay=delay, session=session)
    api.log_callback = lambda message: print(f"  {message}", flush=True)

    def on_progress(page: int, total: int) -> None:
        print(f"  page {page}/{total}", flush=True)

    entries = api.fetch_all_pages(
        on_progress=on_progress,
        existing_ids=None if full else (known_ids or None),
    )
    return [e.to_dict() for e in entries]


def merge(
    existing: List[Dict[str, Any]], fetched: List[Dict[str, Any]]
) -> Tuple[List[Dict[str, Any]], int, int]:
    """``(entries, added, updated)``, ordered by ID so diffs stay small.

    A fetched entry replaces the stored one: an edited title or a new download
    count is the newer truth. IDs ascend with submission date, so ordering by
    ID puts today's additions at the end of the file and leaves the rest of
    the lines untouched.
    """
    by_id: Dict[Any, Dict[str, Any]] = {e.get("id"): e for e in existing}
    added = updated = 0
    for entry in fetched:
        key = entry.get("id")
        if key is None:
            continue
        if key in by_id:
            if clean(by_id[key]) != clean(entry):
                updated += 1
            else:
                continue
        else:
            added += 1
        by_id[key] = entry
    ordered = sorted(by_id.values(), key=lambda e: int(e.get("id") or 0))
    return (ordered, added, updated)


# ---------------------------------------------------------------------------
# Publishing
# ---------------------------------------------------------------------------


def artifacts(catalog: Path, out_dir: Path, entries: int, repo: str) -> Dict[str, Any]:
    """Write the compressed catalog and the manifest that describes it."""
    out_dir.mkdir(parents=True, exist_ok=True)
    archive = out_dir / ARTIFACT_NAME

    raw = catalog.read_bytes()
    # mtime=0 and an empty name: the same catalog has to compress to the same
    # bytes, or every run publishes a "new" file that differs only in the
    # timestamp inside its header — and the checksum stops meaning anything.
    with open(archive, "wb") as handle:
        with gzip.GzipFile(
            filename="", mode="wb", compresslevel=9, fileobj=handle, mtime=0
        ) as out:
            out.write(raw)

    digest = hashlib.sha256(archive.read_bytes()).hexdigest()
    manifest = {
        "schema_version": SCHEMA_VERSION,
        "catalog_version": time.strftime("%Y-%m-%d", time.gmtime()),
        "url": (
            f"https://github.com/{repo}/releases/latest/download/{ARTIFACT_NAME}"
        ),
        "mirrors": [],
        "size_bytes": archive.stat().st_size,
        "sha256": digest,
        "entries": entries,
        "built_at": int(time.time()),
    }
    write(out_dir / "manifest.json", json.dumps(manifest, indent=2) + "\n")
    return manifest


def write(path: Path, text: str) -> None:
    """Write *text* with LF endings, whatever the platform prefers.

    The artifact is compressed from the file on disk and its checksum is
    published, so a build on Windows and a build on the runner have to produce
    the same bytes. Left to Python, one of them would use CRLF.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8", newline="\n") as handle:
        handle.write(text)


def report(name: str, value: str) -> None:
    """Hand a result to the workflow step that follows."""
    path = os.environ.get("GITHUB_OUTPUT")
    if path:
        with open(path, "a", encoding="utf-8") as handle:
            handle.write(f"{name}={value}\n")


# ---------------------------------------------------------------------------


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--catalog", type=Path, default=CATALOG_PATH)
    parser.add_argument("--out", type=Path, default=OUTPUT_DIR)
    parser.add_argument("--delay", type=float, default=DEFAULT_DELAY)
    parser.add_argument(
        "--full", action="store_true",
        help="walk every page instead of stopping at the first known one",
    )
    parser.add_argument(
        "--no-fetch", action="store_true",
        help="repackage what is already here, without asking SMWCentral",
    )
    parser.add_argument(
        "--repo", default=os.environ.get("GITHUB_REPOSITORY", "OWNER/REPO"),
        help="owner/name, for the download URL in the manifest",
    )
    args = parser.parse_args(argv)

    existing = load_catalog(args.catalog)
    print(f"catalog: {len(existing)} entries")

    added = updated = 0
    entries = existing
    if args.no_fetch:
        print("skipping the fetch (--no-fetch)")
    else:
        known = {e.get("id") for e in existing if e.get("id") is not None}
        print("asking SMWCentral for what is new…" if known and not args.full
              else "walking every page…")
        fetched = fetch(known, args.delay, args.full, args.repo)
        entries, added, updated = merge(existing, fetched)
        print(f"fetched {len(fetched)} · {added} new · {updated} changed")

    text = render(entries, time.time())
    before = args.catalog.read_text(encoding="utf-8") if args.catalog.exists() else ""
    changed = entry_block(text) != entry_block(before)

    if changed:
        write(args.catalog, text)
        print(f"wrote {args.catalog} ({args.catalog.stat().st_size / 1e6:.2f} MB)")
    else:
        print("nothing changed")

    # Packaged even on a quiet day, which costs a second and means a caller
    # always has something to publish: the very first run has an unchanged
    # catalog and still has to produce the download that does not exist yet.
    manifest = artifacts(args.catalog, args.out, len(entries), args.repo)
    print(
        f"artifact: {manifest['size_bytes'] / 1e6:.2f} MB gzipped · "
        f"sha256 {manifest['sha256'][:16]}…"
    )
    report("changed", "true" if changed else "false")
    report("added", str(added))
    report("entries", str(len(entries)))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
