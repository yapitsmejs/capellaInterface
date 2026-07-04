from __future__ import annotations

from datetime import UTC, datetime

import pytest
from conftest import (
    SAN_JOSE_BBOX,
    FakeLinkCollection,
    make_full_slc_item,
    make_ieee_contest_collection,
    make_item,
    make_slc_collection,
)

import capella.catalog as catalog_mod
from capella.catalog import (
    IEEE_CONTEST_COLLECTION_URL,
    SLC_COLLECTION_URL,
    _id_prefilter,
    _parse_item_id,
    find_slc_items,
    item_assets,
    item_download_size,
)


@pytest.fixture(autouse=True)
def patch_collections(monkeypatch):
    """Route the two collection URLs to in-memory fixtures."""
    fixtures = {
        SLC_COLLECTION_URL: make_slc_collection(),
        IEEE_CONTEST_COLLECTION_URL: make_ieee_contest_collection(),
    }
    monkeypatch.setattr(catalog_mod, "_get_collection", lambda url: fixtures[url])


def _ids(items):
    return [i.id for i in items]


def test_default_includes_ieee_contest_slc():
    items = find_slc_items()
    ids = _ids(items)
    # SLC product-type items + the contest SLC (C1); GEO pair skipped; S1 deduped.
    assert "C1" in ids
    assert "C1_GEO" not in ids
    assert ids.count("S1") == 1  # dedupe across sources


def test_datetime_ascending_sort():
    items = find_slc_items()
    dts = [i.datetime for i in items if i.datetime is not None]
    assert dts == sorted(dts)


def test_geo_pairs_skipped():
    items = find_slc_items()
    for it in items:
        assert it.properties.get("sar:product_type") == "SLC"


def test_no_ieee_contest_drops_contest_items():
    items = find_slc_items(include_ieee_contest=False)
    ids = _ids(items)
    assert "C1" not in ids
    assert "C1_GEO" not in ids
    # slc collection only: S1, S2, S3
    assert set(ids) == {"S1", "S2", "S3"}


def test_bbox_filter_san_jose():
    items = find_slc_items(bbox=list(SAN_JOSE_BBOX))
    ids = _ids(items)
    # San Jose bbox intersects S1, S2 (and the dedupe copy of S1). S3 (Africa) and
    # C1 (Hawaiʻi) do not intersect.
    assert set(ids) == {"S1", "S2"}
    assert "S3" not in ids
    assert "C1" not in ids


def test_point_filter_inside_san_jose():
    items = find_slc_items(point=(-121.87, 37.31))
    ids = _ids(items)
    assert "S1" in ids and "S2" in ids
    assert "S3" not in ids


def test_datetime_filter_month():
    items = find_slc_items(datetime="2025-08")
    ids = _ids(items)
    # August items: S1 (08-01), S3 (08-15), C1 (08-05). S2 (Sep) excluded.
    assert set(ids) == {"S1", "S3", "C1"}
    assert "S2" not in ids


def test_datetime_filter_range():
    items = find_slc_items(datetime="2025-08-01/2025-08-10")
    ids = _ids(items)
    assert set(ids) == {"S1", "C1"}  # S3 is 08-15, outside range; S2 is Sep


def test_instrument_mode_filter():
    items = find_slc_items(instrument_mode="stripmap")
    ids = _ids(items)
    assert ids == ["S2"]


def test_max_items_limit():
    items = find_slc_items(max_items=2)
    assert len(items) == 2
    # still sorted ascending
    dts = [i.datetime for i in items]
    assert dts == sorted(dts)


def test_bbox_with_no_ieee_contest():
    items = find_slc_items(bbox=list(SAN_JOSE_BBOX), include_ieee_contest=False)
    assert set(_ids(items)) == {"S1", "S2"}


def test_item_assets_resolves_bundle_by_role():
    item = make_full_slc_item()
    assets = item_assets(item)
    # bundle = data COG + metadata JSON only (preview/thumbnail excluded).
    assert len(assets) == 2
    assert "preview" not in assets and "thumbnail" not in assets
    media = [a.media_type for a in assets.values()]
    assert any("tiff" in (m or "") for m in media)
    assert sum(1 for m in media if "json" in (m or "")) == 1


def test_item_download_size():
    item = make_full_slc_item()
    assert item_download_size(item) == 250  # 200 (COG) + 50 (metadata)


# --- id-encoded pre-filter internals ----------------------------------------


def test_parse_item_id_valid():
    parsed = _parse_item_id("CAPELLA_C13_SP_SLC_HH_20260427104538_20260427104546")
    assert parsed is not None
    assert parsed["mode_code"] == "SP"
    assert parsed["mode"] == "spotlight"
    assert parsed["product_type"] == "SLC"
    assert parsed["start_dt"].strftime("%Y%m%d%H%M%S") == "20260427104538"
    assert parsed["end_dt"].strftime("%Y%m%d%H%M%S") == "20260427104546"


def test_parse_item_id_from_href():
    parsed = _parse_item_id(
        "../../by-datetime/2026/2026-04/2026-04-27/"
        "CAPELLA_C15_SM_SLC_HH_20260207204328_20260207204337/"
        "CAPELLA_C15_SM_SLC_HH_20260207204328_20260207204337.json"
    )
    assert parsed is not None
    assert parsed["mode_code"] == "SM"
    assert parsed["mode"] == "stripmap"


def test_parse_item_id_non_capella_returns_none():
    assert _parse_item_id("S1") is None
    assert _parse_item_id("C1_GEO") is None
    assert _parse_item_id("not-an-id") is None


def test_id_prefilter_none_falls_through():
    # Non-Capella ids must not be dropped by the pre-filter.
    assert _id_prefilter(None, dt_filter=None, instrument_mode="spotlight", slc_only=True)


def test_id_prefilter_slc_only_drops_geo():
    geo = _parse_item_id("CAPELLA_C13_SP_GEO_HH_20251112022441_20251112022453")
    assert _id_prefilter(geo, dt_filter=None, instrument_mode=None, slc_only=True) is False
    slc = _parse_item_id("CAPELLA_C13_SP_SLC_HH_20251112022441_20251112022453")
    assert _id_prefilter(slc, dt_filter=None, instrument_mode=None, slc_only=True) is True


def test_id_prefilter_mode_mismatch():
    sp = _parse_item_id("CAPELLA_C13_SP_SLC_HH_20260427104538_20260427104546")
    assert _id_prefilter(sp, dt_filter=None, instrument_mode="stripmap", slc_only=True) is False
    assert _id_prefilter(sp, dt_filter=None, instrument_mode="spotlight", slc_only=True) is True


def test_id_prefilter_datetime_out_of_range():
    parsed = _parse_item_id("CAPELLA_C13_SP_SLC_HH_20250815000000_20250815000100")
    aug = (datetime(2025, 8, 1, tzinfo=UTC), datetime(2025, 8, 31, 23, 59, 59, tzinfo=UTC))
    assert _id_prefilter(parsed, dt_filter=aug, instrument_mode=None, slc_only=True) is True
    sep = (datetime(2025, 9, 1, tzinfo=UTC), datetime(2025, 9, 30, 23, 59, 59, tzinfo=UTC))
    assert _id_prefilter(parsed, dt_filter=sep, instrument_mode=None, slc_only=True) is False


def test_id_prefilter_datetime_overlap_boundary_kept():
    # Acquisition window 2025-07-31T23:59 -> 2025-08-01T00:01 overlaps an August filter, so
    # the pre-filter must keep it (the authoritative center-time filter decides later).
    parsed = _parse_item_id("CAPELLA_C13_SP_SLC_HH_20250731235900_20250801000100")
    aug = (datetime(2025, 8, 1, tzinfo=UTC), datetime(2025, 8, 31, 23, 59, 59, tzinfo=UTC))
    assert _id_prefilter(parsed, dt_filter=aug, instrument_mode=None, slc_only=True) is True


# --- concurrent, link-based path --------------------------------------------


@pytest.fixture
def patch_link_collections(monkeypatch):
    """Route the two collection URLs to link-bearing fakes and resolve hrefs to fixture items
    via ``_fetch_item``. Exercises the real (concurrent) path in ``find_slc_items``."""
    slc = FakeLinkCollection("capella-open-data-slc", make_slc_collection()._items)
    contest = FakeLinkCollection(
        "capella-open-data-ieee-data-contest", make_ieee_contest_collection()._items
    )
    fixtures = {
        SLC_COLLECTION_URL: slc,
        IEEE_CONTEST_COLLECTION_URL: contest,
    }
    monkeypatch.setattr(catalog_mod, "_get_collection", lambda url: fixtures[url])

    lookup = {**slc.item_by_href, **contest.item_by_href}

    def fake_fetch(href):
        return lookup[href]

    monkeypatch.setattr(catalog_mod, "_fetch_item", fake_fetch)
    return slc, contest


def test_concurrent_path_default_includes_contest_slc_dedupes(patch_link_collections):
    items = find_slc_items()
    ids = _ids(items)
    assert "C1" in ids
    assert "C1_GEO" not in ids
    assert ids.count("S1") == 1


def test_concurrent_path_bbox_filter(patch_link_collections):
    items = find_slc_items(bbox=list(SAN_JOSE_BBOX))
    assert set(_ids(items)) == {"S1", "S2"}


def test_concurrent_path_geo_pairs_never_fetched(monkeypatch):
    # With Capella-shaped ids, the GEO pair's href is skipped by the id pre-filter, so
    # _fetch_item is never called with it.
    geo_id = "CAPELLA_C13_SP_GEO_HH_20250805000000_20250805000100"
    slc_id = "CAPELLA_C13_SP_SLC_HH_20250805000000_20250805000100"
    geo = make_item(geo_id, [-155.4, 19.3, -155.1, 19.5], datetime(2025, 8, 5, tzinfo=UTC), product_type="geo")
    slc = make_item(slc_id, [-155.4, 19.3, -155.1, 19.5], datetime(2025, 8, 5, tzinfo=UTC), product_type="slc")

    contest = FakeLinkCollection("capella-open-data-ieee-data-contest", [geo, slc])
    empty = FakeLinkCollection("capella-open-data-slc", [])
    monkeypatch.setattr(
        catalog_mod,
        "_get_collection",
        lambda url: {SLC_COLLECTION_URL: empty, IEEE_CONTEST_COLLECTION_URL: contest}[url],
    )
    lookup = contest.item_by_href
    fetched: list[str] = []

    def spy_fetch(href):
        fetched.append(href)
        return lookup[href]

    monkeypatch.setattr(catalog_mod, "_fetch_item", spy_fetch)
    items = find_slc_items()
    assert _ids(items) == [slc_id]
    assert not any("GEO" in h for h in fetched)


def test_concurrent_path_max_items_truncates(patch_link_collections):
    items = find_slc_items(max_items=2)
    assert len(items) == 2
    dts = [i.datetime for i in items if i.datetime is not None]
    assert dts == sorted(dts)
