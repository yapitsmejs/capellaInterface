# SLC Download CLI — Interaction Design

## Context

The repo plan at `plans/capella-interface-plan.md` already specifies the overall tool (SLC-only download → L1; L2/L2LCR/L2ML derived locally) and outlines `catalog.py` / `download.py` / `cli.py`. This plan **refines only the user-facing SLC download interaction** — how someone actually drives the CLI to find and fetch SLC scenes — and supersedes the download-section sketches in the repo plan.

The user wants a guided, location-driven download: pick an area, see the available SLC datasets, choose a range, confirm, fetch. Through clarification we established the real constraints:

- **Capella's open-data STAC catalog never names "San Jose"** (nor arbitrary cities). Verified across all six top-level branches:
  - By Capital: 206 world-capital collections (Dakar, Tokyo, Berlin…; no San Jose)
  - By Use Case: 16 thematic collections (Maritime, Agriculture, InSAR…) — not geographic
  - By Datetime: year sub-catalogs only; By Product Type / Instrument Mode: product/mode, not place
  - IEEE Data Contest 2026: a single AOI on the **Big Island of Hawaiʻi** (≈ lon −155.29, lat 19.42), not California
- Therefore a named-location menu is impossible from the catalog alone. The user chose **bbox/point entry** as the location step: scenes covering San Jose (the user reports SLC at lon −121.87085436, lat 37.318451845 via Capella's Felt viewer) are reachable only by spatial search across the `slc` collection, never by a place name.

Confirmed facts baked into the design:
- SLC product-type collection: `https://capella-open-data.s3.us-west-2.amazonaws.com/stac/capella-open-data-slc/collection.json`
- IEEE Data Contest 2026 collection (a STAC **Collection**, not a sub-catalog): `https://capella-open-data.s3.us-west-2.amazonaws.com/stac/capella-open-data-ieee-data-contest/collection.json` — ships paired **SLC+GEO** Spotlight items (mixed HH/VV, ~Jul–Nov 2025). The contest's **SLC items are included in search by default**; its GEO pairs are skipped (filter by `capella:product_type == "slc"` / item id contains `_SLC_`). The same acquisition can appear in both collections → results deduped by `item.id`.
- Anonymous access; items have `bbox`, `datetime`, and `capella:instrument_mode` properties; assets resolved by role/media-type.
- Static catalog (no spatial query API) → bbox/point filtering is **client-side** (iterate items, shapely intersection) over both collections.

Locked decisions: location = bbox/point only (no name menu); selection = contiguous index range; confirm = list-then-confirm default with `--yes`; non-interactive = prompts **and** flags share one code path; output = flat `<dest>/<item_id>/`; filters = bbox, datetime, instrument mode; **IEEE contest SLC included by default**, opt out with `--no-ieee-contest`.

## Approach — the download interaction

Two CLI commands. `search` is discovery (no fetching); `download` is the guided fetch.

### `capella search` (discovery, no download)
```
capella search --bbox MINLON,MINLAT,MAXLON,MAXLAT | --point LON,LAT
              [--datetime 2025-08 | --datetime 2025-08-01/2025-08-31]
              [--mode spotlight|sliding_spotlight|stripmap]
              [--limit N] [--json] [--no-ieee-contest]
```
- Searches the `slc` product-type collection **and** the IEEE Data Contest 2026 collection (SLC items only, deduped by `item.id`), filters client-side by bbox/point intersection + datetime + mode, sorts by datetime ascending, prints a numbered table (`#  ID  datetime  mode  pol  size`). `--json` emits machine-readable rows for piping. `--no-ieee-contest` restricts the search to the `slc` product-type collection only. Zero matches → clear message, exit non-zero. Used to learn item IDs / counts before downloading; does not write files.

### `capella download` (guided fetch) — the primary command
```
capella download [--bbox ... | --point LON,LAT]
                 [--datetime ...] [--mode ...]
                 [--range 3-8 | all | 5]
                 [--dest DIR] [--yes] [--no-ieee-contest]
```
Interactive when required input is missing and stdin is a TTY; flag-driven otherwise (same code path). Steps:

1. **Area entry.** If neither `--bbox` nor `--point` given, prompt:
   `Enter area as bbox (minLon,minLat,maxLon,maxLat) or point (lon,lat):`
   Parse to a spatial filter. (Optional refine prompts for datetime/mode, default "any".)
2. **Search.** `catalog.find_slc_items(bbox|point=…, datetime=…, mode=…, include_ieee_contest=not no_ieee_contest)` → datetime-ascending SLC item list (contest SLC included by default; GEO pairs skipped; deduped across sources).
3. **List.** Print the numbered table (same as `search`). Zero matches → message + exit non-zero.
4. **Range.** If `--range` not given, prompt: `Enter range to download (e.g. 3-8, 'all', or a single N):`
   `parse_range(expr, n)` accepts `3-8`, `all`, `5`; invalid input re-prompts.
5. **Confirm.** Print selected items + total download size and prompt `Download N scenes (~X MB) to <dest>/<id>/? [y/N]`. Skipped by `--yes`.
6. **Download.** For each selected item, `download.download_item(item, dest)` writes the 3-file SLC bundle (complex COG + STAC JSON + extended-metadata JSON) to **`<dest>/<item_id>/`** (flat) with `tqdm` progress and idempotent skip when the file exists and `Content-Length` matches (resume-friendly). `--dest` defaults to `./data`.
7. **Summary.** Print per-item output paths + any skips; exit 0.

Non-interactive example (scripts/CI/tests):
```
capella download --point -121.87085436,37.318451845 --range all --yes --dest ./data
capella download --bbox -121.9,37.2,-121.7,37.4 --range 1-3 --dest ./data
```

## Implementation touches (existing files from repo plan)

- **`src/capella/catalog.py`** — add/extend:
  - `SLC_COLLECTION_URL` and `IEEE_CONTEST_COLLECTION_URL` constants.
  - `find_slc_items(*, bbox=None, point=None, datetime=None, instrument_mode=None, max_items=None, include_ieee_contest=True)` — load the `capella-open-data-slc` collection via `pystac` and (when `include_ieee_contest`) the IEEE contest collection too; iterate `get_all_items()` (lazy); for the contest collection drop GEO pairs (`capella:product_type == "slc"` / id contains `_SLC_`); dedupe by `item.id` across the two sources; filter by shapely bbox/point intersection, `item.datetime`, `item.properties.get("capella:instrument_mode")`; return a datetime-ascending **list** (numbering needs materialization). `point` becomes a shapely point tested with `intersects`.
  - `item_assets(item)` — role/media-type resolution (data→COG, JSON media-types→STAC + extended metadata). Already in repo plan; reused by downloader.
  - `item_download_size(item)` — sum asset file sizes for the listing/confirm totals (from asset `file:size` or a `Content-Length` HEAD).
- **`src/capella/download.py`** — add:
  - `parse_range(expr: str, n: int) -> list[int]` — `3-8`→[3..8] (1-indexed), `all`→[1..n], `5`→[5]; validate bounds, raise `ValueError` with a friendly message on bad input.
  - `format_item_table(items) -> str` — numbered listing (`#, ID, datetime, mode, pol, size`).
  - `download_item(item, dest_root)` — stream the 3 files to `<dest_root>/<item.id>/<basename>`, idempotent skip-on-size-match (already in repo plan; output path now flat under `dest_root`, no `L1/` level dir).
  - `total_size(items) -> int`.
- **`src/capella/cli.py`** — implement `search` and `download` Click commands:
  - `download` owns the interactive flow (area → list → range → confirm → fetch → summary). Uses `click.prompt`/`click.confirm`; prompts are **auto-skipped** when the equivalent flag is supplied or stdin is not a TTY (so `CliRunner` tests and `--yes` scripts run headless). All flag paths and prompt paths call the same `run_download(...)` core.
  - `search` prints the table / `--json`. Both `search` and `download` expose `--no-ieee-contest` (passed through as `include_ieee_contest=False`); default is contest included.
  - `--help` documents the guided flow + the SLC-only constraint (derived levels point at `capella l2`/`l2lcr`/`l2ml`) + that the IEEE Data Contest 2026 SLC items are searched by default.
- **`src/capella/levels.py`** — unchanged; `download` only ever fetches SLC (L1). Calling `download --level L2|L2LCR|L2ML` prints the matching transform command (per repo plan).
- **`tests/test_cli.py`** (new) — `click.testing.CliRunner` with `catalog.find_slc_items` + `download.download_item` monkeypatched: assert (a) interactive flow prints the table then prompts for range (input "all") and downloads; (b) `--range all --yes` runs fully non-interactively; (c) zero matches → non-zero exit with message; (d) `--level L2` prints transform hint, fetches nothing; (e) `--no-ieee-contest` is passed through as `include_ieee_contest=False`.
- **`tests/test_download.py`** — add `parse_range` unit tests (`3-8`, `all`, `5`, out-of-range, garbage) + flat `<dest>/<id>/` layout + idempotent skip (already in repo plan, repointed to flat layout).
- **`tests/test_catalog.py`** — add bbox/point/datetime/mode filtering + datetime-ascending sort on a fixture collection; add a fixture IEEE contest collection with paired SLC/GEO items → assert SLC items included by default, GEO pairs skipped, dedupe by `item.id` across the two collections, and `include_ieee_contest=False` drops contest items.

## Notes & risks

- **Client-side spatial search over a static catalog can be slow**: the `slc` collection may hold thousands of items; first listing streams them through `pystac` before filtering. Mitigation: filter lazily during iteration and stop early at `--limit` for `search`; for `download`, materialize only after the area filter. Document that the first listing can take a few seconds.
- **San Jose is not guaranteed in open data.** The user reports SLC at lon −121.87085436 / lat 37.318451845 via Capella's Felt viewer; if the open-data STAC catalog has no SLC item there, `search`/`download` honestly report zero matches. The smoke test below uses that point and will surface whatever Capella actually exposes.
- **Flat output**: SLC writes to `<dest>/<item_id>/` (no `L1/` level dir). Transforms (`l2`/`l2lcr`) take an explicit `<l1_scene_dir>` path, so they don't depend on a level folder; `--out` paths keep transform outputs from clashing with downloads. This updates the repo plan's `data/L1/<id>/` layout.
- **IEEE contest AOI is separate from San Jose.** The contest collection covers its own AOI (a Capella-provided contest site, not California). With no spatial filter, contest SLC items appear in `search`/`download` by default; with a `--bbox`/`--point` over San Jose, only contest items whose `bbox` intersects that area are returned — so a San Jose search will not surface the contest stack unless the AOI overlaps. `--no-ieee-contest` opts out entirely.

## Verification

1. `uv run pytest -q` green, including new `test_cli.py` (CliRunner headless flow + `--yes`), `parse_range` cases, flat-layout download, catalog bbox/point/mode filtering, and IEEE default-include / GEO-skip / dedupe.
2. Live smoke (network; optional, auto-skip if offline):
   - `uv run capella search --limit 10` (no area filter) → lists SLC items including IEEE Data Contest 2026 SLC items by default.
   - `uv run capella search --point -121.87085436,37.318451845 --limit 10` → lists SLC items covering San Jose (or a clean "0 SLC items found" if none).
   - `uv run capella search --limit 10 --no-ieee-contest` → contest items absent.
   - `uv run capella download --point -121.87085436,37.318451845 --range 1 --yes --dest ./data` → writes one 3-file SLC bundle to `./data/<id>/`; idempotent on rerun (skip-on-size-match).
   - Interactive: `uv run capella download` (no flags) walks area prompt → list → range prompt → confirm → fetch → summary.
3. `uv run ruff check .` clean.

## Files touched
```
src/capella/catalog.py     (find_slc_items w/ bbox+point+datetime+mode+include_ieee_contest; SLC+IEEE_CONTEST collection URLs; item_download_size)
src/capella/download.py    (parse_range, format_item_table, flat download_item, total_size)
src/capella/cli.py         (search + download commands, guided interactive flow, --no-ieee-contest)
src/capella/levels.py      (unchanged — SLC-only)
tests/test_cli.py          (new — CliRunner flow, --yes, zero-match, --level hint, --no-ieee-contest)
tests/test_download.py     (parse_range + flat layout + idempotent skip)
tests/test_catalog.py      (bbox/point/datetime/mode filter + sort + IEEE default-include/GEO-skip/dedupe)
```