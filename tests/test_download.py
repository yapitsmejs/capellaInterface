from __future__ import annotations

import os

import pytest
import requests
from conftest import SAN_JOSE_BBOX, dt, make_full_slc_item, make_item

from capella import download
from capella.catalog import item_assets
from capella.download import (
    download_item,
    format_item_table,
    parse_range,
    total_size,
)


class FakeResponse:
    def __init__(self, data: bytes) -> None:
        self._data = data
        self.headers = {"content-length": str(len(data))}
        self.status_code = 200

    def raise_for_status(self) -> None:
        pass

    def iter_content(self, chunk_size: int = 65536):
        yield self._data


class FakeSession:
    def __init__(self, mapping: dict[str, bytes]) -> None:
        self.mapping = mapping
        self.get_calls = 0
        self.head_calls = 0

    def head(self, url: str, allow_redirects: bool = True):
        self.head_calls += 1
        data = self.mapping[url]
        return type(
            "FakeHead",
            (),
            {"headers": {"content-length": str(len(data))}, "status_code": 200},
        )()

    def get(self, url: str, stream: bool = True, **kwargs):
        self.get_calls += 1
        return FakeResponse(self.mapping[url])


# --- parse_range ---------------------------------------------------------


def test_parse_range_contiguous():
    assert parse_range("3-8", 10) == [3, 4, 5, 6, 7, 8]


def test_parse_range_all():
    assert parse_range("all", 5) == [1, 2, 3, 4, 5]


def test_parse_range_single():
    assert parse_range("5", 10) == [5]


@pytest.mark.parametrize("expr,n", [("0-3", 10), ("11", 10), ("3-1", 10), ("4-12", 10)])
def test_parse_range_out_of_bounds(expr, n):
    with pytest.raises(ValueError):
        parse_range(expr, n)


@pytest.mark.parametrize("expr", ["garbage", "", "1..3", "-3"])
def test_parse_range_invalid(expr):
    with pytest.raises(ValueError):
        parse_range(expr, 10)


def test_parse_range_empty_list():
    with pytest.raises(ValueError):
        parse_range("all", 0)


# --- download_item -------------------------------------------------------


def _session_for(item):
    mapping = {}
    for asset in item.assets.values():
        mapping[asset.href] = b"payload-for-" + os.path.basename(asset.href).encode()
    return FakeSession(mapping)


def test_download_item_flat_layout(tmp_path, monkeypatch):
    item = make_full_slc_item("S1")
    sess = _session_for(item)
    # isolate from real requests
    monkeypatch.setattr(download.requests, "get", sess.get, raising=False)

    out_dir, results = download_item(item, str(tmp_path), session=sess)

    assert out_dir == os.path.join(str(tmp_path), "S1")
    assert os.path.isdir(out_dir)
    # 2 bundle files written (data COG + metadata JSON), all "downloaded"
    assert len(results) == 2
    assert all(s == "downloaded" for _, s in results)
    files = sorted(os.path.basename(p) for p in os.listdir(out_dir))
    written = sorted(
        os.path.basename(asset.href)
        for asset in item.assets.values()
        if "overview" not in (asset.roles or []) and "thumbnail" not in (asset.roles or [])
    )
    assert files == written
    assert sess.get_calls == 2


def test_download_item_content(tmp_path):

    item = make_full_slc_item("S1")
    sess = _session_for(item)
    out_dir, _ = download_item(item, str(tmp_path), session=sess)
    for asset in item_assets(item).values():
        fn = os.path.basename(asset.href)
        with open(os.path.join(out_dir, fn), "rb") as f:
            assert f.read() == b"payload-for-" + fn.encode()


def test_download_item_idempotent_skip(tmp_path):
    item = make_full_slc_item("S1")
    sess = _session_for(item)
    download_item(item, str(tmp_path), session=sess)
    first_gets = sess.get_calls
    assert first_gets == 2

    # second run: sizes match -> all skipped, no new GETs
    _, results = download_item(item, str(tmp_path), session=sess)
    assert all(s == "skipped" for _, s in results)
    assert sess.get_calls == first_gets  # no additional downloads


# --- resume + retry ------------------------------------------------------


class FakeRangeResponse:
    """A streaming response that honors a start offset and can drop mid-stream."""

    def __init__(self, body, status_code=200, drop_after=None, exc=None):
        self._body = body
        self.status_code = status_code
        self.headers = {"content-length": str(len(body))}
        self._drop_after = drop_after
        self._exc = exc

    def raise_for_status(self):
        if self.status_code >= 400:
            raise requests.HTTPError(f"status {self.status_code}")

    def iter_content(self, chunk_size=65536):
        body = self._body
        if self._drop_after is not None:
            body = body[: self._drop_after]
        emitted = 0
        while emitted < len(body):
            chunk = body[emitted : emitted + chunk_size]
            emitted += len(chunk)
            yield chunk
        if self._exc is not None:
            raise self._exc


class FakeRangeSession:
    """Session backed by a single full payload; honors ``Range: bytes=N-`` (206)."""

    def __init__(self, full, drop_on_first=False, drop_after=100):
        self.full = full
        self.get_calls = 0
        self.head_calls = 0
        self.range_starts: list[int] = []
        self.drop_on_first = drop_on_first
        self.drop_after = drop_after

    def head(self, url, allow_redirects=True):
        self.head_calls += 1
        return type(
            "FakeHead",
            (),
            {"headers": {"content-length": str(len(self.full))}, "status_code": 200},
        )()

    def get(self, url, stream=True, headers=None, **kwargs):
        self.get_calls += 1
        rng = (headers or {}).get("Range")
        start = 0
        if rng and rng.startswith("bytes="):
            start = int(rng.split("=", 1)[1].split("-", 1)[0])
        self.range_starts.append(start)
        body = self.full[start:]
        sc = 206 if start > 0 else 200
        if self.drop_on_first and self.get_calls == 1:
            return FakeRangeResponse(
                self.full[start:],
                status_code=sc,
                drop_after=self.drop_after - start,
                exc=requests.exceptions.ChunkedEncodingError("connection broken"),
            )
        return FakeRangeResponse(body, status_code=sc)


def test_download_resumes_partial_file(tmp_path):
    full = b"PAYLOAD-" + bytes(range(256))  # 264 bytes

    one = make_item(
        "S1", SAN_JOSE_BBOX, dt(2025, 8, 1),
        assets={
            "HH": {
                "href": "https://example.com/data/slc.tif",
                "media_type": "image/tiff",
                "roles": ["data"],
            },
        },
    )
    assert len(item_assets(one)) == 1

    sess = FakeRangeSession(full)
    out_dir = os.path.join(str(tmp_path), "S1")
    os.makedirs(out_dir)
    # pre-write the first 100 bytes (a partial file from a prior interrupted run)
    with open(os.path.join(out_dir, "slc.tif"), "wb") as f:
        f.write(full[:100])

    _, results = download_item(one, str(tmp_path), session=sess)
    assert results == [("slc.tif", "resumed")]
    assert sess.get_calls == 1
    assert sess.range_starts == [100]  # resumed from byte 100
    with open(os.path.join(out_dir, "slc.tif"), "rb") as f:
        assert f.read() == full


def test_download_retries_after_broken_connection(tmp_path):
    full = b"PAYLOAD-" + bytes(range(256))

    one = make_item(
        "S1", SAN_JOSE_BBOX, dt(2025, 8, 1),
        assets={
            "HH": {
                "href": "https://example.com/data/slc.tif",
                "media_type": "image/tiff",
                "roles": ["data"],
            },
        },
    )

    sess = FakeRangeSession(full, drop_on_first=True, drop_after=100)
    _, results = download_item(one, str(tmp_path), session=sess)
    # first GET dropped at 100 bytes; second GET resumed from 100 and completed.
    assert results == [("slc.tif", "resumed")]
    assert sess.get_calls == 2
    assert sess.range_starts == [0, 100]
    with open(os.path.join(str(tmp_path), "S1", "slc.tif"), "rb") as f:
        assert f.read() == full


# --- listing helpers -----------------------------------------------------


def test_format_item_table_lists_numbered():
    item = make_full_slc_item("S1")
    table = format_item_table([item])
    assert "S1" in table
    assert "spotlight" in table
    # numbered row starting with 1
    assert any(line.lstrip().startswith("1") for line in table.splitlines())


def test_total_size():
    item = make_full_slc_item("S1")
    assert total_size([item]) == 250
