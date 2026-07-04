"""Shared pytest fixtures.

Provides helpers to build in-memory pystac Items and a FakeCollection that mimics the
slice of ``pystac.Collection`` the catalog client uses (``.get_all_items()``). Tests
monkeypatch ``capella.catalog._get_collection`` to return these, avoiding network I/O.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from datetime import UTC, datetime

import pystac


@dataclass
class FakeLink:
    """Minimal stand-in for a pystac item link. The catalog client reads .rel/.href and
    .absolute_href (already-absolute in tests)."""

    rel: str
    href: str

    @property
    def absolute_href(self) -> str:
        return self.href


def make_item(
    item_id: str,
    bbox: list[float],
    dt: datetime,
    *,
    mode: str = "spotlight",
    pol: str = "HH",
    product_type: str = "slc",
    assets: dict[str, dict] | None = None,
) -> pystac.Item:
    """Build a minimal pystac Item with Capella-like properties and optional assets."""
    min_lon, min_lat, max_lon, max_lat = bbox
    geometry = {
        "type": "Polygon",
        "coordinates": [
            [
                [min_lon, min_lat],
                [max_lon, min_lat],
                [max_lon, max_lat],
                [min_lon, max_lat],
                [min_lon, min_lat],
            ]
        ],
    }
    properties: dict = {
        "sar:instrument_mode": mode,
        "sar:polarizations": [pol],
        "sar:product_type": product_type.upper(),
    }
    item = pystac.Item(id=item_id, geometry=geometry, bbox=bbox, datetime=dt, properties=properties)
    for key, spec in (assets or {}).items():
        item.add_asset(
            key,
            pystac.Asset(
                href=spec["href"],
                media_type=spec.get("media_type"),
                roles=spec.get("roles"),
                extra_fields=spec.get("extra_fields", {}),
            ),
        )
    return item


class FakeCollection:
    """In-memory stand-in for a pystac Collection with a fixed item list."""

    def __init__(self, cid: str, items: Iterable[pystac.Item]) -> None:
        self.id = cid
        self._items = list(items)

    def get_all_items(self) -> Iterable[pystac.Item]:
        return iter(self._items)


class FakeLinkCollection:
    """Stand-in for a pystac Collection that exposes flat ``item`` links (the real Capella
    catalog shape) instead of materialized items. ``href`` is derived from each item's id;
    tests monkeypatch ``capella.catalog._fetch_item`` to map an href back to its item.

    This exercises the concurrent, link-based path in ``find_slc_items`` without network.
    """

    def __init__(self, cid: str, items: Iterable[pystac.Item]) -> None:
        self.id = cid
        self._items = list(items)
        self.links = [
            FakeLink(rel="item", href=f"https://example.test/stac/{it.id}/{it.id}.json")
            for it in self._items
        ]

    @property
    def item_by_href(self) -> dict[str, pystac.Item]:
        return {f"https://example.test/stac/{it.id}/{it.id}.json": it for it in self._items}


# Bounding boxes used across fixtures.
SAN_JOSE_BBOX = [-121.9, 37.2, -121.7, 37.4]
HAWAII_BBOX = [-155.4, 19.3, -155.1, 19.5]
AFRICA_BBOX = [-10.0, -5.0, -9.0, -4.0]


def dt(year: int, month: int, day: int) -> datetime:
    return datetime(year, month, day, tzinfo=UTC)


def make_slc_collection() -> FakeCollection:
    """capella-open-data-slc fixture: 3 items (S1 spotlight, S2 stripmap, S3 far away).

    Deliberately shuffled so datetime-ascending sort is exercised.
    """
    items = [
        make_item("S2", SAN_JOSE_BBOX, dt(2025, 9, 1), mode="stripmap", pol="VV"),
        make_item("S1", SAN_JOSE_BBOX, dt(2025, 8, 1), mode="spotlight", pol="HH"),
        make_item("S3", AFRICA_BBOX, dt(2025, 8, 15), mode="spotlight", pol="HH"),
    ]
    return FakeCollection("capella-open-data-slc", items)


def make_ieee_contest_collection() -> FakeCollection:
    """IEEE contest fixture: paired SLC+GEO + a dedupe copy of S1 + a fresh contest SLC.

    - S1: same id as the slc collection's S1 -> dedupe (kept from slc source).
    - C1: contest SLC over Hawaiʻi -> included by default.
    - C1_GEO: the GEO pair of C1 -> skipped (product_type != slc).
    """
    items = [
        make_item("C1_GEO", HAWAII_BBOX, dt(2025, 8, 5), product_type="geo"),
        make_item("C1", HAWAII_BBOX, dt(2025, 8, 5), product_type="slc", pol="VV"),
        make_item("S1", SAN_JOSE_BBOX, dt(2025, 8, 1), product_type="slc", pol="HH"),
    ]
    return FakeCollection("capella-open-data-ieee-data-contest", items)


def make_full_slc_item(item_id: str = "S1") -> pystac.Item:
    """A single SLC item with the real Capella asset set: data COG + extended metadata
    JSON bundle, plus preview/thumbnail assets that must be excluded from downloads."""
    return make_item(
        item_id,
        SAN_JOSE_BBOX,
        dt(2025, 8, 1),
        assets={
            "HH": {
                "href": "https://example.com/data/CAPELLA_C01_SLC_20250801.tif",
                "media_type": "image/tiff; application=geotiff",
                "roles": ["data"],
                "extra_fields": {"file:size": 200},
            },
            "metadata": {
                "href": "https://example.com/data/CAPELLA_C01_SLC_20250801_extended.json",
                "media_type": "application/json",
                "roles": ["metadata"],
                "extra_fields": {"file:size": 50},
            },
            "preview": {
                "href": "https://example.com/data/CAPELLA_C01_GEO_20250801_preview.tif",
                "media_type": "image/tiff; application=geotiff; profile=cloud-optimized",
                "roles": ["overview"],
            },
            "thumbnail": {
                "href": "https://example.com/data/CAPELLA_C01_GEO_20250801_thumb.png",
                "media_type": "image/png",
                "roles": ["thumbnail"],
            },
        },
    )
