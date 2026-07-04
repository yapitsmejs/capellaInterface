"""Capella open-data STAC catalog client.

Thin wrapper over the **static** Capella open-data STAC catalog (not a STAC API).
Searches SLC items across two collections:

* the ``capella-open-data-slc`` product-type collection, and
* the ``capella-open-data-ieee-data-contest`` IEEE Data Contest 2026 collection.

The contest collection ships paired **SLC + GEO** Spotlight items, so its items are
filtered to SLC only (GEO pairs skipped). Acquisitions can appear in both collections, so
results are deduped by ``item.id``. Spatial filtering is client-side (bounding-box / point
intersection) — there is no spatial query endpoint.

All network I/O is isolated behind :func:`_get_collection` (loads a collection) and
:func:`_fetch_item` (resolves one item link), so tests monkeypatch them with in-memory
fixtures — :func:`_fetch_items_concurrent` fans the item fetches out across worker threads
over a shared keep-alive session, and the item id is pre-filtered (product type / mode /
acquisition window) to skip fetches that cannot match.
"""

from __future__ import annotations

import calendar
import os
import re
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import UTC, datetime, timedelta

import pystac
import requests

CATALOG_ROOT = "https://capella-open-data.s3.us-west-2.amazonaws.com/stac/catalog.json"
# The SLC product-type collection lives under the by-product-type sub-catalog
# (not at the STAC root). Resolved from the live root catalog's child links.
SLC_COLLECTION_URL = (
    "https://capella-open-data.s3.us-west-2.amazonaws.com/stac/"
    "capella-open-data-by-product-type/capella-open-data-slc/collection.json"
)
IEEE_CONTEST_COLLECTION_URL = (
    "https://capella-open-data.s3.us-west-2.amazonaws.com/stac/"
    "capella-open-data-ieee-data-contest/collection.json"
)

# Instrument-mode code -> sar:instrument_mode name, as encoded in Capella item ids
# (CAPELLA_C<NN>_<MODE>_<PT>_<POL>_<start14>_<end14>). Verified against the live catalog.
MODE_CODE_TO_NAME = {"SP": "spotlight", "SS": "sliding_spotlight", "SM": "stripmap"}

# Capella item id: CAPELLA_C13_SP_SLC_HH_20260427104538_20260427104546
_ITEM_ID_RE = re.compile(
    r"^CAPELLA_C\d+_(SP|SS|SM)_(SLC|GEO)_(HH|VV|HV|VH)_(\d{14})_(\d{14})$"
)

# Capella item ids encode the acquisition start/end as YYYYMMDDHHMMSS.
_ID_TS_FMT = "%Y%m%d%H%M%S"

# Concurrent item-JSON fetches. I/O-bound HTTP, so threads are sufficient; the S3 host
# comfortably handles a few dozen parallel anonymous GETs.
_FETCH_WORKERS = 64


def _get_collection(url: str) -> pystac.Collection:
    """Load a STAC collection by URL. Tests monkeypatch this with fixture collections."""
    return pystac.Collection.from_file(url)


def _shared_session() -> requests.Session:
    """A process-wide requests.Session with a connection pool sized for the concurrent
    fetch fan-out, so the 32 worker threads reuse keep-alive TCP connections instead of
    opening a fresh one per item (pystac's default does one ``requests.get`` per file)."""
    sess = requests.Session()
    adapter = requests.adapters.HTTPAdapter(
        pool_connections=_FETCH_WORKERS, pool_maxsize=_FETCH_WORKERS
    )
    sess.mount("https://", adapter)
    sess.mount("http://", adapter)
    return sess


_SESSION: requests.Session | None = None


def _fetch_item(href: str) -> pystac.Item:
    """Resolve a single item link href to a pystac Item via the shared keep-alive session.
    Tests monkeypatch this (new seam) to return fixture items by id, so the concurrent path
    is unit-testable without network. Capella item JSON ships absolute asset hrefs, so
    ``Item.from_dict`` preserves download-ready asset hrefs without root resolution."""
    global _SESSION
    if _SESSION is None:
        _SESSION = _shared_session()
    resp = _SESSION.get(href, timeout=30)
    resp.raise_for_status()
    return pystac.Item.from_dict(resp.json())


def _fetch_items_concurrent(
    hrefs: list[str],
    *,
    max_workers: int = _FETCH_WORKERS,
    should_stop: Callable[[], bool] | None = None,
    on_item: Callable[[pystac.Item], None] | None = None,
) -> None:
    """Resolve item hrefs concurrently, handing each surviving item to ``on_item``.

    Per-item errors are swallowed (a single missing or malformed item link must not abort the
    whole search). After each ``on_item`` call, if ``should_stop`` returns True, pending
    (not-yet-started) futures are cancelled and the loop exits — a best-effort early
    termination that keeps small ``--limit`` searches from fetching the whole catalog.
    """
    if not hrefs or on_item is None:
        return
    with ThreadPoolExecutor(max_workers=min(max_workers, len(hrefs))) as ex:
        futures = {ex.submit(_fetch_item, h): h for h in hrefs}
        for fut in as_completed(futures):
            try:
                item = fut.result()
            except Exception:
                continue
            if item is not None:
                on_item(item)
                if should_stop is not None and should_stop():
                    for f in futures:
                        f.cancel()
                    return


def _collection_item_links(coll) -> list[pystac.Link] | None:  # type: ignore[no-untyped-def]
    """Item links from a collection, or None if the collection is not link-based.

    The real pystac Collection exposes ``.links`` (flat item links for these collections);
    the test ``FakeCollection`` does not, returning ``None`` so the legacy
    ``get_all_items()`` path is used instead.
    """
    links = getattr(coll, "links", None)
    if links is None:
        return None  # not a link-based collection (e.g. test FakeCollection)
    return [link for link in links if getattr(link, "rel", None) == "item"]


def _parse_item_id(item_id_or_href: str) -> dict | None:
    """Parse a Capella item id (or an item-link href) to its encoded fields, or None if the
    id is not Capella-shaped.

    Returns ``{"mode_code", "product_type", "mode", "start_dt", "end_dt"}`` with datetimes in
    UTC. Returning ``None`` is the safe fall-through: non-Capella ids (e.g. test fixtures
    ``S1``/``C1``) skip pre-filtering and flow through the authoritative filter path.
    """
    iid = os.path.basename(item_id_or_href.rstrip("/"))
    if iid.endswith(".json"):
        iid = iid[:-5]
    m = _ITEM_ID_RE.match(iid)
    if m is None:
        return None
    mode_code, product_type, _pol, start_s, end_s = m.groups()
    start_dt = datetime.strptime(start_s, _ID_TS_FMT).replace(tzinfo=UTC)
    end_dt = datetime.strptime(end_s, _ID_TS_FMT).replace(tzinfo=UTC)
    return {
        "mode_code": mode_code,
        "product_type": product_type,
        "mode": MODE_CODE_TO_NAME.get(mode_code),
        "start_dt": start_dt,
        "end_dt": end_dt,
    }


def _id_prefilter(
    parsed: dict | None,
    *,
    dt_filter: tuple[datetime, datetime] | None,
    instrument_mode: str | None,
    slc_only: bool,
) -> bool:
    """True if an item whose id parsed to `parsed` *could still match* the filters (so it
    should be fetched for the authoritative spatial/datetime check). False means the encoded
    id already rules it out and the fetch can be skipped entirely.

    A ``None`` parsed id (non-Capella shape) always returns True — fall through to the real
    filters rather than dropping the item.
    """
    if parsed is None:
        return True
    if slc_only and parsed["product_type"] != "SLC":
        return False
    if instrument_mode is not None and parsed["mode"] != instrument_mode:
        return False
    if dt_filter is not None:
        f_start, f_end = dt_filter
        # acquisition [start_dt, end_dt] overlaps [f_start, f_end]?
        if parsed["end_dt"] < f_start or parsed["start_dt"] > f_end:
            return False
    return True


def _period_bounds(s: str) -> tuple[datetime, datetime]:
    """Parse a single datetime token to (start, end) inclusive UTC bounds.

    Accepts a year (``2025``), year-month (``2025-08``), date (``2025-08-01``), or a full
    ISO timestamp (``2025-08-01T00:00:00Z``). Bare dates expand to the end of their period.
    """
    s = s.strip()
    if "T" in s or (":") in s:  # full timestamp
        dt = datetime.fromisoformat(s.replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=UTC)
        return dt, dt
    parts = s.split("-")
    if len(parts) == 1:  # year
        y = int(parts[0])
        start = datetime(y, 1, 1, tzinfo=UTC)
        end = datetime(y + 1, 1, 1, tzinfo=UTC) - timedelta(seconds=1)
    elif len(parts) == 2:  # year-month
        y, m = int(parts[0]), int(parts[1])
        start = datetime(y, m, 1, tzinfo=UTC)
        last_day = calendar.monthrange(y, m)[1]
        end = datetime(y, m, last_day, 23, 59, 59, tzinfo=UTC)
    else:  # year-month-day
        y, m, d = int(parts[0]), int(parts[1]), int(parts[2])
        start = datetime(y, m, d, tzinfo=UTC)
        end = datetime(y, m, d, 23, 59, 59, tzinfo=UTC)
    return start, end


def _parse_datetime_filter(expr: str | None) -> tuple[datetime, datetime] | None:
    """Parse a datetime filter expr to (start, end) inclusive UTC bounds, or None.

    Accepts a single period (``2025-08``) or a ``start/end`` range.
    """
    if expr is None:
        return None
    expr = expr.strip()
    if "/" in expr:
        a, b = expr.split("/", 1)
        start, _ = _period_bounds(a)
        _, end = _period_bounds(b)
        return start, end
    start, end = _period_bounds(expr)
    return start, end


def _item_datetime(item: pystac.Item) -> datetime | None:
    dt = item.datetime
    if dt is not None:
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=UTC)
        return dt
    start = item.properties.get("start_datetime")
    if start:
        return _item_datetime_from_iso(start)
    return None


def _item_datetime_from_iso(s: str) -> datetime:
    dt = datetime.fromisoformat(s.replace("Z", "+00:00"))
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    return dt


def _is_slc_item(item: pystac.Item) -> bool:
    """True if `item` is an SLC product. Resolved by ``sar:product_type`` when present
    (Capella open data uses ``SLC``/``GEO``), falling back to an ``_SLC_`` marker in the
    item id (never by hardcoded asset key)."""
    pt = item.properties.get("sar:product_type") or item.properties.get(
        "capella:product_type"
    )
    if pt is not None:
        return str(pt).lower() == "slc"
    return "_SLC_" in item.id.upper()


def item_mode(item: pystac.Item) -> str | None:
    """Instrument mode (``sar:instrument_mode`` with ``capella:instrument_mode`` fallback)."""
    return item.properties.get("sar:instrument_mode") or item.properties.get(
        "capella:instrument_mode"
    )


def item_polarization(item: pystac.Item) -> str | None:
    """Polarization (``capella:polarization`` scalar or ``sar:polarizations`` list[0])."""
    pol = item.properties.get("capella:polarization")
    if pol:
        return str(pol)
    pols = item.properties.get("sar:polarizations")
    if isinstance(pols, list) and pols:
        return str(pols[0])
    return None


def _bbox_intersects(item_bbox: list[float], filt: list[float]) -> bool:
    """Bounding-box intersection (no shapely). filt = [minLon, minLat, maxLon, maxLat]."""
    return (
        item_bbox[0] <= filt[2]
        and item_bbox[2] >= filt[0]
        and item_bbox[1] <= filt[3]
        and item_bbox[3] >= filt[1]
    )


def _bbox_contains(item_bbox: list[float], lon: float, lat: float) -> bool:
    return item_bbox[0] <= lon <= item_bbox[2] and item_bbox[1] <= lat <= item_bbox[3]


def _spatial_match(
    item: pystac.Item, bbox: list[float] | None, point: tuple[float, float] | None
) -> bool:
    if bbox is None and point is None:
        return True
    ib = item.bbox
    if ib is None:
        return False
    if bbox is not None:
        return _bbox_intersects(list(ib), bbox)
    assert point is not None
    return _bbox_contains(list(ib), point[0], point[1])


def find_slc_items(
    *,
    bbox: list[float] | None = None,
    point: tuple[float, float] | None = None,
    datetime: str | None = None,  # noqa: A002  (shadows datetime class; intentional)
    instrument_mode: str | None = None,
    max_items: int | None = None,
    include_ieee_contest: bool = True,
) -> list[pystac.Item]:
    """Search SLC items across the `slc` product-type collection and (by default) the
    IEEE Data Contest 2026 collection.

    - Contest GEO pairs are skipped (SLC only).
    - Results deduped by ``item.id`` across the two sources.
    - Client-side filtering by bbox/point intersection, datetime, and instrument mode.
    - Returns a list sorted by item datetime ascending (numbering needs materialization).

    Pass ``include_ieee_contest=False`` (CLI ``--no-ieee-contest``) to search the
    `slc` product-type collection only.
    """
    sources = [SLC_COLLECTION_URL]
    if include_ieee_contest:
        sources.append(IEEE_CONTEST_COLLECTION_URL)

    dt_filter = _parse_datetime_filter(datetime)
    seen: set[str] = set()
    results: list[pystac.Item] = []

    def _sort_key(it: pystac.Item) -> datetime:
        dt = _item_datetime(it)
        return dt if dt is not None else datetime.max.replace(tzinfo=UTC)

    def _accept(item: pystac.Item) -> None:
        """Apply the authoritative filters to an already-resolved item; dedupe + append."""
        if item.id in seen:
            return
        if not _is_slc_item(item):
            # skips contest GEO pairs
            return
        if not _spatial_match(item, bbox, point):
            return
        if dt_filter is not None:
            dt = _item_datetime(item)
            if dt is None or not (dt_filter[0] <= dt <= dt_filter[1]):
                return
        if instrument_mode is not None and item_mode(item) != instrument_mode:
            return
        seen.add(item.id)
        results.append(item)

    def _have_enough() -> bool:
        return max_items is not None and len(results) >= max_items

    for url in sources:
        coll = _get_collection(url)
        links = _collection_item_links(coll)
        if links is None:
            # Legacy/test path: items already materialized (FakeCollection.get_all_items).
            for item in coll.get_all_items():
                _accept(item)
                if _have_enough():
                    results.sort(key=_sort_key)
                    return results
            continue

        # Real path: item links carry only {rel, href, type} — no bbox/datetime — so the
        # item JSON must be fetched to filter spatially. First, skip fetches the item id
        # already rules out (contest GEO pairs, wrong mode, out-of-range datetime), then
        # resolve the survivors concurrently.
        candidate_hrefs: list[str] = []
        for link in links:
            # Item links are stored as hrefs relative to the collection; resolve to absolute
            # so _fetch_item can GET them directly via the shared session.
            href = getattr(link, "absolute_href", None) or link.href
            parsed = _parse_item_id(href)
            if not _id_prefilter(
                parsed,
                dt_filter=dt_filter,
                instrument_mode=instrument_mode,
                slc_only=True,
            ):
                continue
            candidate_hrefs.append(href)

        # Fetch candidates concurrently; best-effort early termination once enough are
        # accepted. Without max_items, all candidates are fetched.
        _fetch_items_concurrent(
            candidate_hrefs, should_stop=_have_enough, on_item=_accept
        )
        if _have_enough():
            break  # don't walk the next source

    results.sort(key=_sort_key)
    if max_items is not None:
        return results[:max_items]
    return results


def item_assets(item: pystac.Item) -> dict[str, pystac.Asset]:
    """Resolve the SLC bundle assets to download by role / media-type (never hardcoded
    keys).

    Capella SLC items ship: a ``data`` role complex COG, a ``metadata`` role extended-
    metadata JSON, and ``overview``/``thumbnail`` preview files. Only the data COG and
    metadata JSON are part of the SLC bundle; previews are excluded.
    """
    out: dict[str, pystac.Asset] = {}
    for key, asset in item.assets.items():
        roles = set(asset.roles or [])
        media = (asset.media_type or "").lower()
        if roles & {"overview", "thumbnail", "visual"}:
            continue  # preview / thumbnail — not part of the SLC bundle
        is_data = bool(roles & {"data", "image"}) or "tiff" in media or "cog" in media
        is_meta = bool(roles & {"metadata"}) or "json" in media
        if is_data or is_meta:
            out[key] = asset
    return out


def item_download_size(item: pystac.Item) -> int:
    """Sum the asset file sizes for an item, from each asset's ``file:size`` extra field."""
    total = 0
    for asset in item_assets(item).values():
        size = asset.extra_fields.get("file:size") if asset.extra_fields else None
        if isinstance(size, (int, float)):
            total += int(size)
    return total
