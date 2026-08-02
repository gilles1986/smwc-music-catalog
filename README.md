# SMWC music catalog

The music metadata of [SMWCentral](https://www.smwcentral.net/?p=section&s=smwmusic)
as one file, rebuilt every day.

It exists so that [Saphros Game Music Player](https://github.com/Saphros) does
not have to walk ~200 API pages on every installation to learn what music
exists. One machine asks SMWCentral once a day; everybody else downloads the
answer.

## Download

| | |
|---|---|
| Catalog | <https://github.com/gilles1986/smwc-music-catalog/releases/latest/download/smwc-catalog.json.gz> |
| Manifest | <https://github.com/gilles1986/smwc-music-catalog/releases/latest/download/manifest.json> |

Fetch the manifest first — it is small, and it says whether a download is
worth doing:

```json
{
  "schema_version": 1,
  "catalog_version": "2026-08-02",
  "url": "https://github.com/gilles1986/smwc-music-catalog/releases/latest/download/smwc-catalog.json.gz",
  "mirrors": [],
  "size_bytes": 1343488,
  "sha256": "…",
  "entries": 9694,
  "built_at": 1785508243
}
```

`sha256` is over the compressed file, and it is the same from one build to the
next when the catalog did not change. Check it before installing what you
downloaded.

## The file

Ordinary JSON, gzipped for transport:

```json
{"schema_version":1,
"sync_time":1785508243.6,
"total_entries":9694,
"entries":[
{"authors":[{"id":28467,"name":"brickblock369"}],"id":42228,…},
{…}
]}
```

One entry per line, ordered by ID. That is still a single JSON document —
nothing has to parse it line by line — but a day's update shows up in
`git diff` as the handful of lines it is, and git stores it as such.

Fields are what SMWCentral's API returns, with one exception: **`raw_description`
is dropped.** The API sends a cleaned and a raw description; they are
character-identical for half the catalog, together they are 58% of the file,
and the player searches the cleaned one. 11 MB became 6.2 MB, and 1.3 MB
compressed.

`schema_version` changes when the shape changes in a way that would break a
reader. Anything else — new entries, new fields — leaves it alone.

## How it is built

[`tools/build_catalog.py`](tools/build_catalog.py) updates rather than
crawls. SMWCentral answers newest-first, so the walk stops at the first page
holding nothing new: an ordinary day is **one request**. The catalog in this
repository is the starting point, so the first run had nothing to catch up on
either.

```bash
pip install requests
python tools/build_catalog.py                 # update, package, write dist/
python tools/build_catalog.py --full          # walk every page (rarely needed)
python tools/build_catalog.py --no-fetch      # repackage, ask nobody
```

Nothing is written when nothing changed — no commit and no new checksum on a
quiet day. The artifact is packaged either way, so there is always something to
publish; the workflow decides whether it needs to.

`vendor/music_api.py` is a copy of the player's own API client, so the catalog
is parsed by the same code that reads it. When that file changes in the
player, copy it here again; nothing else in this repository knows how
SMWCentral answers.

## The daily run

[`.github/workflows/update-catalog.yml`](.github/workflows/update-catalog.yml)
runs at 04:17 UTC, commits the catalog when it changed, and uploads the
artifact to the `latest` release. It can also be started by hand from the
Actions tab, with a checkbox for a full walk.

Two things worth knowing: GitHub switches scheduled workflows off after 60
days without repository activity (a day with new songs commits, which counts),
and the job asks SMWCentral for one page a day with a delay between requests
and a User-Agent that says who it is.

The workflow needs nothing but the repository itself — no secrets, no tokens.
The download URL in the manifest is built from `GITHUB_REPOSITORY`, so a fork
publishes under its own name without an edit.

The `latest` release is created by the first run that finds it missing. Until
that has happened there is nothing at the download links above; start it from
**Actions → Update catalog → Run workflow**, or wait for the schedule.
