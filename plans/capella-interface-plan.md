# Capella Interface — SLC Download + Level Transformations

## Context

`capellaInterface/` is currently a greenfield repo (empty `src/`, `scripts/`, `tests/` + stub `README.md`). The goal is a Python tool that (1) downloads Capella Space **SLC** only (the single Capella product that meets the requirements — slant-plane, complex, single-look), and (2) **derives all higher processing levels locally** from that SLC. Capella's other stock products (GEO/GEC/SIDD detected-multilooked, SICD/CPHD) are **not** used.

Level taxonomy — a single processing chain, each level built on the previous:

| Level | Meaning | Built from |
|---|---|---|
| **L1** | slant-plane image (complex, single-look) | Capella **SLC** — **download** |
| **L2** | ground-plane image (complex, single-look) | **L1** — coherent geocoding (slant→ground, phase preserved, no multi-look) |
| **L2LCR** | ground plane, **registered across looks** | **L1** — sub-aperture look decomposition → geocode each look → co-register onto a common ground grid (complex, per-look) |
| **L2ML** | ground-plane image, **multi-looked** + detected | **L2LCR** — average the co-registered looks (complex) + detect (`|·|`) → speckle-reduced amplitude ground image |

Pipeline order: **L1 → L2 → L2LCR → L2ML**. L2ML is last because proper multi-looking = averaging the co-registered looks (L2LCR), not an independent geocoding.

Catalog facts (download side):
- Root STAC catalog (static, not a STAC API): `https://capella-open-data.s3.us-west-2.amazonaws.com/stac/catalog.json`
- Sub-catalogs organize data by product type / instrument mode / use case / capital / datetime, plus an `IEEE Data Contest 2026` STAC collection (`capella-open-data-ieee-data-contest`) that ships paired **SLC+GEO** Spotlight items (mixed HH/VV, ~Jul–Nov 2025). We target SLC items from **both** the `slc` product-type collection **and** the IEEE contest collection — the contest's SLC items are included in search **by default**; its GEO pairs are skipped. The same acquisition can appear in both collections, so results are deduped by `item.id`.
- Assets live in anonymous bucket `s3://capella-open-data/data/` (region `us-west-2`, `--no-sign-request`). Download via HTTP GET on the asset `href`.
- Each SLC scene = a 3-file TIFF+JSON bundle: complex COG (CInt16: 16-bit real + 16-bit imag) + STAC metadata JSON + extended metadata JSON (image geometry: PFA `slant_plane_normal`/`ground_plane_normal`, orbit state vectors, Doppler, sample spacing — the inputs the SAR-geocoding backend needs).

Environment: Python 3.12 on Windows. **Environment managed with [`uv`](https://docs.astral.sh/uv/)** — `uv sync` creates the venv, pins deps via `uv.lock`, and runs everything via `uv run` (no manual `.venv`/`pip` script). Because every level above L1 is a SAR processing step, the stack is **rasterio (bundles GDAL) + pystac + a SAR-geocoding backend (`sarsen`, with `sarpy` as fallback) + numpy/scipy + geopandas**, plus a **DEM** (Copernicus DEM GLO-30, open on AWS) for terrain geocoding.

## Approach

Installable Python package with a **library API** and a **CLI**. Layers, bottom-up:

### 1. Package skeleton & deps (uv)
- `pyproject.toml` (`src/` layout). `[project] dependencies`: `pystac>=1.8`, `requests`, `rasterio>=1.4`, `numpy`, `scipy`, `geopandas`, `click` (CLI), `tqdm`, and the SAR backend `sarsen` (pulls `xarray`, `rioxarray`) with `sarpy` as fallback. `[project.optional-dependencies]`: `dev = ["pytest", "ruff"]`. `[project.scripts] capella = "capella.cli:main"`.
- DEM is a runtime input (`--dem <path>`), not a pip dep; a small optional helper fetches Copernicus DEM GLO-30 tiles from AWS open data.
- Bootstrap: `uv sync --extra dev` (creates `.venv`, installs editable package + all deps). Run via `uv run ...`. A committed `uv.lock` pins the resolution.
- `.gitignore`: append `data/`, `*.tif`, `*.ntf`, downloaded `*.json`, `dems/`, `.venv/`. Keep `CLAUDE.md` ignored.

### 2. Levels enum — `src/capella/levels.py`
```python
class Level(str, Enum):
    L1 = "L1"        # slant, complex, single-look            [download: SLC]
    L2 = "L2"        # ground, complex, single-look           [derive from L1]
    L2LCR = "L2LCR"  # ground, registered across looks         [derive from L1]
    L2ML = "L2ML"    # ground, multi-looked, detected          [derive from L2LCR]

DOWNLOAD_PRODUCTS = {Level.L1: ("slc",)}        # only SLC is fetched from Capella
DERIVED_LEVELS = {Level.L2, Level.L2LCR, Level.L2ML}
# direct predecessor in the chain (for CLI hints / dependency checks)
LEVEL_INPUT = {Level.L2: Level.L1, Level.L2LCR: Level.L1, Level.L2ML: Level.L2LCR}
```
Helpers `products_for(level)`, `is_derived(level)`, `input_level(level)`. `download_level` refuses derived levels and points the user at the matching transform CLI command.

### 3. Catalog client — `src/capella/catalog.py`
Thin wrapper over `pystac.Catalog.from_file(root_url)`:
- `CATALOG_ROOT` constant; two SLC source URLs: `SLC_COLLECTION_URL` (`…/capella-open-data-slc/collection.json`) and `IEEE_CONTEST_COLLECTION_URL` (`…/capella-open-data-ieee-data-contest/collection.json`).
- `find_slc_items(*, bbox=None, datetime=None, instrument_mode=None, max_items=None, include_ieee_contest=True)` — by default walks **both** the `slc` product-type collection **and** the IEEE Data Contest 2026 collection. The contest collection ships paired SLC+GEO items, so filter to SLC only (`capella:product_type == "slc"` / item id contains `_SLC_`). Dedupe across the two sources by `item.id` (the same acquisition can appear in both). Filters by `item.bbox` (shapely) and `item.datetime` (ISO str or `start/end` range), returns a lazy generator. `include_ieee_contest=False` skips the contest collection (CLI `--no-ieee-contest`).
- `item_assets(item)` resolves the asset dict by `roles`/media-type (never hardcode keys): `data`/`image/tiff` → complex COG; JSON media-types → STAC + extended metadata. Used by the downloader.

### 4. Downloader (SLC → L1) — `src/capella/download.py`
- `download_item(item, dest_dir)`: stream the 3-file bundle via `requests.get(stream=True)` to `dest_dir/{item.id}/{basename}`, with `tqdm` progress and **idempotent skip** when the file exists and `Content-Length` matches (resume-friendly).
- `download_l1(*filters, dest_root)`: search SLC + download, writing under `dest_root/L1/<item_id>/`.
- All network I/O isolated here so tests monkeypatch `requests.get` / `pystac.Catalog.from_file`.

### 5. SAR processing backend — `src/capella/sar/backend.py`
Abstraction over the geocoding/look-decomposition primitives the transforms need, so the backend can be swapped (sarsen ↔ sarpy) without touching transforms:
- `geocode_slc(slc_cog_path, extended_metadata_path, dem_path, *, crs, res) -> (complex_array[H,W], geotransform, crs)` — coherent slant→ground, complex preserved.
- `subaperture_looks(slc_cog_path, extended_metadata_path, n_looks) -> list[complex_array]` — split azimuth spectrum into N sub-aperture looks (still slant, complex).
- `detect(complex_array) -> amplitude` — `np.abs`.
- **Primary backend: `sarsen`** (`sarsen.geocoding` for terrain geocoding of complex SLC; sub-aperture split via azimuth bandpass on the SLC samples). **Fallback: `sarpy`** (reads Capella's SICD variant from open data, `sarpy.geometry` for geocoding/sub-aperture). Implementation begins with a **short spike** to confirm which backend ingests Capella's SLC COG + extended-metadata JSON cleanly.
- **Honesty/risk note** (module docstring + README): Capella's complex products are not a first-class input to most open SAR-geocoding tools (which target Sentinel-1 / ICEYE). L2 (geocoding) is moderately risky; L2LCR (sub-aperture decomposition) may need custom azimuth bandpass on top of the backend. The spike result is recorded in the README; if neither backend ingests Capella SLC cleanly, the transforms fall back to a GCP-based `gdalwarp -gcp` geocoding using the extended-metadata ground-control points (approximate, flagged **non-coherent**; sub-aperture then unavailable).

### 6. Transforms — `src/capella/transforms/`
- **`l2.py` — `build_l2(l1_scene_dir, dem_path, out_path, *, crs, res)`**: call `backend.geocode_slc`; write a **2-band float32 GeoTIFF** (real, imag) in UTM with a geotransform + companion JSON (DEM id, backend, L1 source id). No detection, no averaging — pixels stay complex.
- **`l2lcr.py` — `build_l2lcr(l1_scene_dir, dem_path, out_path, *, n_looks, crs, res, master_index=0)`**:
  1. `backend.subaperture_looks(..., n_looks)` → N complex slant-plane looks.
  2. `backend.geocode_slc` each look to the **same** UTM grid (master look's geotransform).
  3. Refine alignment with FFT phase-correlation (scipy) on the look amplitudes (sub-pixel), then `rasterio.warp.reproject` slaves onto master grid.
  4. Stack → multi-band GeoTIFF (one band per registered look) + companion JSON (per-look shifts, source id, n_looks). Stays **complex** (2 bands per look, or complex interleaved — documented in writer).
  - Alternative input mode (documented): co-register ≥2 already-derived L2 scenes of the same area (multi-temporal stack) — same registration code path.
- **`l2ml.py` — `build_l2ml(l2lcr_path, out_path)`**: open the L2LCR look-stack, **average the co-registered looks in the complex domain** (complex mean across the look axis), then `backend.detect` → single-band float32 amplitude GeoTIFF (Sigma-nought-calibrated per the extended-metadata `scale_factor`) + companion JSON (n_looks, source L2LCR id). This is the multi-look step built on L2LCR, per the required ordering.

### 7. CLI — `src/capella/cli.py` (Click)
```
capella search   --bbox minX,minY,maxX,maxY [--datetime 2024-01] [--mode spotlight] [--limit 10] [--no-ieee-contest]
capella download [--bbox ...] [--datetime ...] [--limit N] [--no-ieee-contest] --dest ./data       # SLC -> data/L1/<id>/
capella l2       <l1_scene_dir> --dem <dem.tif> --out l2.tif [--crs EPSG:..] [--res 5]
capella l2lcr    <l1_scene_dir> --dem <dem.tif> --out l2lcr.tif [--n-looks 4] [--master 0]
capella l2ml     <l2lcr_stack.tif> --out l2ml.tif
```
- `download` only ever fetches SLC (L1); calling it with a derived level prints the matching transform command instead.
- `l2` / `l2lcr` require `--dem`; backend chosen via `--backend sarsen|sarpy|gcp` (default per spike). `l2ml` takes the **L2LCR** stack (no DEM — looks are already geocoded).
- The CLI documents the chain order (L1 → L2 → L2LCR → L2ML) in `--help`.

### 8. Tests — `tests/`
- `tests/test_levels.py`: `DOWNLOAD_PRODUCTS` contains only SLC; `DERIVED_LEVELS` and `LEVEL_INPUT` correct (L2ML ← L2LCR); `download_level` refuses L2/L2LCR/L2ML.
- `tests/test_catalog.py`: monkeypatch `pystac.Catalog.from_file` with in-memory fixture catalogs (an `slc` product-type collection + an IEEE contest collection with paired SLC/GEO items) → assert SLC-only filtering by bbox/datetime, IEEE SLC items included by default, GEO pairs skipped, cross-source dedupe by `item.id`, and `include_ieee_contest=False` drops contest items; asset resolution by role.
- `tests/test_download.py`: monkeypatch `requests.get` → canned bytes; assert 3-file bundle written + idempotent skip-on-size-match.
- `tests/test_sar_backend.py`: monkeypatch the backend primitives to return known arrays; assert `detect` returns real amplitude; `subaperture_looks` returns `n_looks` arrays; `geocode_slc` shape matches requested grid.
- `tests/test_transforms.py`: orchestration tests with the backend monkeypatched — `build_l2` writes a 2-band complex GeoTIFF + JSON; `build_l2lcr` writes `n_looks`-band stack + JSON with shifts (sub-pixel co-registration validated on synthetic shifted arrays, ±0.5 px); `build_l2ml` averages the L2LCR stack + detects → 1-band amplitude of the expected shape. Real geocoding validated only in the network smoke test (`@pytest.mark.network`).
- `tests/conftest.py`: synthetic complex SLC-like array + tiny DEM GeoTIFF + sample STAC item JSON fixtures (generated in-test via rasterio).

## Files to create
```
pyproject.toml                  (deps incl. sarsen/sarpy, dev extra, capella script)
uv.lock                          (committed; from `uv sync`)
README.md                        (uv setup, quickstart, levels table, SLC-only download,
                                 processing chain L1->L2->L2LCR->L2ML, SAR backend + DEM,
                                 spike result, L2LCR limitation)
src/capella/__init__.py
src/capella/levels.py
src/capella/catalog.py
src/capella/download.py
src/capella/sar/__init__.py
src/capella/sar/backend.py       (geocode / subaperture / detect; sarsen/sarpy/gcp)
src/capella/transforms/__init__.py
src/capella/transforms/l2.py
src/capella/transforms/l2lcr.py
src/capella/transforms/l2ml.py
src/capella/cli.py
tests/test_levels.py
tests/test_catalog.py
tests/test_download.py
tests/test_sar_backend.py
tests/test_transforms.py
tests/conftest.py                (synthetic complex SLC + DEM + STAC fixtures)
.gitignore                       (append data/, *.tif, *.ntf, downloaded *.json, dems/, .venv/)
```
Existing empty `src/`, `scripts/`, `tests/` are consumed as-is. No pre-existing code to preserve.

## Verification
1. `uv sync --extra dev` succeeds in repo root (rasterio wheel provides GDAL on Windows; sarsen/sarpy install via wheel).
2. **Backend spike**: run a one-off `uv run python scripts/spike_capella_geocode.py` against a downloaded SLC + Copernicus DEM to confirm sarsen (or sarpy) ingests Capella SLC + extended metadata; record the working backend + any GCP fallback in the README before finalizing transforms.
3. `uv run pytest -q` → unit tests green (SLC-only catalog filter, IEEE contest SLC default-include / GEO-skip / cross-source dedupe, download idempotency, backend primitives with mocks, transform orchestration incl. L2ML-from-L2LCR, sub-pixel co-registration on synthetic arrays). Network tests skipped offline.
4. Live smoke test (network; optional — auto-skip if offline):
   - `uv run capella search --limit 3` (no area filter) lists SLC items including IEEE Data Contest 2026 SLC items by default; `--no-ieee-contest` drops them.
   - `uv run capella search --bbox ... --limit 3` lists 3 SLC items.
   - `uv run capella download --bbox ... --limit 1 --dest ./data` writes the SLC bundle under `data/L1/<id>/`; idempotent on rerun.
   - `uv run capella l2 ./data/L1/<id> --dem <copernicus_dem.tif> --out ./data/L2/<id>.tif` → 2-band complex GeoTIFF, UTM CRS, finite values.
   - `uv run capella l2lcr ./data/L1/<id> --dem <dem.tif> --n-looks 4 --out ./data/L2LCR/<id>.tif` → 4-band (×2 complex) registered stack + JSON.
   - `uv run capella l2ml ./data/L2LCR/<id>.tif --out ./data/L2ML/<id>.tif` → 1-band amplitude GeoTIFF (look-averaged + detected).
5. `uv run ruff check .` clean.