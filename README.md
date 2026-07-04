# capellaInterface

Interface for downloading and performing transformations on Capella SAR datasets.

`capella` is a CLI + Python library that:

1. **Downloads Capella Space SLC** (the single Capella product that is slant-plane,
   complex, single-look) — processing level **L1**.
2. **Derives all higher processing levels locally** from that SLC — L2, L2LCR, L2ML.

Capella's other stock products (GEO/GEC/SIDD detected-multilooked, SICD/CPHD) are **not**
used; everything above L1 is built locally from SLC.

## Processing levels

| Level | Meaning | Built from |
|---|---|---|
| **L1** | slant-plane image (complex, single-look) | Capella **SLC** — **download** |
| **L2** | ground-plane image (complex, single-look) | **L1** — coherent geocoding |
| **L2LCR** | ground plane, registered across looks | **L1** — sub-aperture looks → geocode → co-register |
| **L2ML** | ground-plane, multi-looked + detected | **L2LCR** — average co-registered looks + detect |

Pipeline order: **L1 → L2 → L2LCR → L2ML**.

> **Status:** the SLC downloader (L1) and `search`/`download` CLI are implemented.
> The SAR processing backend and the L2/L2LCR/L2ML transforms are not yet built —
> their dependencies (`rasterio`, `sarsen`/`sarpy`, `numpy`/`scipy`, `geopandas`) and
> a Copernicus DEM input will be added when that work lands.

## Setup

Environment is managed with [`uv`](https://docs.astral.sh/uv/). From the repo root:

```bash
uv sync --extra dev        # creates .venv, installs editable package + dev deps
uv run capella --help
```

## Quickstart

Discover SLC scenes covering an area (no files written):

```bash
uv run capella search --point -121.87085436,37.318451845 --limit 10
uv run capella search --bbox -121.9,37.2,-121.7,37.4 --datetime 2025-08 --json
```

Guided download (writes the SLC bundle — complex COG + extended-metadata JSON — to
`<dest>/<item_id>/`). Downloads are **resumable**: a file already complete is skipped; a
partial file is continued via HTTP `Range`; and a dropped connection mid-stream is
retried by resuming from the bytes on disk (up to 5 attempts). Re-running `download` after
an interrupted run picks up where it left off.

```bash
# non-interactive
uv run capella download --point -121.87085436,37.318451845 --range all --yes --dest ./data

# interactive (prompts for area, destination, range, and confirmation when flags are omitted)
uv run capella download
```

### IEEE Data Contest 2026

SLC search **walks the IEEE Data Contest 2026 collection by default** alongside the
`capella-open-data-slc` product-type collection. The contest collection ships paired
**SLC + GEO** Spotlight items over a Big Island of Hawaiʻi AOI (~lon −155.3, lat 19.4);
only its **SLC** items are surfaced (GEO pairs are skipped), and acquisitions appearing in
both collections are **deduped by `item.id`**.

> **Note (verified against the live catalog):** the contest's 791 SLC items are a subset of
> the 2274 SLC items already in `capella-open-data-slc`. So contest SLC scenes appear in
> search results **regardless** of the contest collection — walking it guarantees coverage
> if Capella ever publishes contest-only SLC. `--no-ieee-contest` skips walking the contest
> collection; it does **not** remove SLC items, since they are already present in the `slc`
> collection. The contest collection's distinct contribution is its GEO pairs, which are
> skipped for SLC-only download. The contest collection's 791 GEO item links are now
> skipped **without a fetch** (id pre-filter), so `--no-ieee-contest` is mainly a small
> further opt-out that avoids resolving the contest's 791 SLC candidates.

```bash
uv run capella search --bbox -155.4,19.3,-155.1,19.5 --limit 5   # contest AOI SLC scenes
uv run capella search --bbox -155.4,19.3,-155.1,19.5 --limit 5 --no-ieee-contest
```

## CLI reference

```
capella search [--bbox MINLON,MINLAT,MAXLON,MAXLAT | --point LON,LAT]
               [--datetime 2025-08 | 2025-08-01/2025-08-31]
               [--mode spotlight|sliding_spotlight|stripmap]
               [--limit N] [--json] [--no-ieee-contest]

capella download [--bbox ... | --point LON,LAT]
                 [--datetime ...] [--mode ...]
                 [--range 3-8 | all | 5]
                 [--dest DIR] [--yes] [--no-ieee-contest]
                 [--level L1|L2|L2LCR|L2ML]
```

`download` only ever fetches SLC (L1). Calling it with `--level L2|L2LCR|L2ML` prints the
matching local transform command instead of downloading.

## Catalog & data facts

- Static STAC catalog (not a STAC API):
  `https://capella-open-data.s3.us-west-2.amazonaws.com/stac/catalog.json`
- SLC product-type collection:
  `…/stac/capella-open-data-slc/collection.json`
- IEEE Data Contest 2026 collection:
  `…/stac/capella-open-data-ieee-data-contest/collection.json`
- Anonymous access; assets live in `s3://capella-open-data/data/` (region `us-west-2`).
- Spatial filtering is client-side (shapely-free bounding-box/point intersection) over
  both collections. Each collection lists items as flat `item` links with no bbox/datetime
  in the link, so the item JSON must be fetched to filter spatially. Search resolves the
  candidate items **concurrently** (64 worker threads over a shared keep-alive session) and
  **pre-filters by the item id** — which encodes product type (`SLC`/`GEO`), instrument mode
  (`SP`/`SS`/`SM`), and the acquisition start/end timestamps — so contest GEO pairs,
  wrong-mode items, and out-of-range datetimes are skipped *without a fetch*. A filtered
  search (bbox + datetime and/or mode) typically returns in a few seconds; an unfiltered
  bbox search fetches more candidates and takes longer. `--limit N` stops early once N
  matches are found.

## Development

```bash
uv run pytest -q
uv run ruff check .
```