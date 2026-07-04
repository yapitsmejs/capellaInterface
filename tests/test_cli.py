from __future__ import annotations

import json as jsonlib

import pytest
from click.testing import CliRunner
from conftest import SAN_JOSE_BBOX, dt, make_item

import capella.catalog as catalog_mod
import capella.download as download_mod
from capella.cli import main


@pytest.fixture
def items():
    return [
        make_item("S1", SAN_JOSE_BBOX, dt(2025, 8, 1), mode="spotlight", pol="HH"),
        make_item("S2", SAN_JOSE_BBOX, dt(2025, 9, 1), mode="stripmap", pol="VV"),
    ]


@pytest.fixture
def patched(monkeypatch, items):
    """Stub catalog + download so the CLI runs fully offline."""
    calls: dict = {"find": [], "download": [], "dest": []}

    def fake_find(**kwargs):
        calls["find"].append(kwargs)
        return items

    def fake_download(item, dest, **kwargs):
        calls["download"].append(item.id)
        calls["dest"].append(dest)
        return f"{dest}/{item.id}", [("data.tif", "downloaded")]

    monkeypatch.setattr(catalog_mod, "find_slc_items", fake_find)
    monkeypatch.setattr(download_mod, "download_item", fake_download)
    monkeypatch.setattr(download_mod, "format_item_table", lambda its: "TABLE")
    monkeypatch.setattr(download_mod, "total_size", lambda its: 0)
    return calls


def _invoke(patched, args, input_=None):
    runner = CliRunner()
    return runner.invoke(main, args, input=input_, catch_exceptions=False)


# --- search --------------------------------------------------------------


def test_search_prints_table(patched):
    res = _invoke(patched, ["search", "--limit", "2"])
    assert res.exit_code == 0
    assert "TABLE" in res.output
    assert patched["find"][0]["max_items"] == 2
    assert patched["find"][0]["include_ieee_contest"] is True


def test_search_json(patched):
    res = _invoke(patched, ["search", "--point", "-121.87,37.31", "--json"])
    assert res.exit_code == 0
    rows = jsonlib.loads(res.output)
    assert rows[0]["id"] == "S1"


def test_search_no_ieee_contest(patched):
    res = _invoke(patched, ["search", "--no-ieee-contest", "--limit", "1"])
    assert res.exit_code == 0
    assert patched["find"][0]["include_ieee_contest"] is False


def test_search_zero_matches_exits_nonzero(monkeypatch):
    monkeypatch.setattr(catalog_mod, "find_slc_items", lambda **k: [])
    monkeypatch.setattr(download_mod, "format_item_table", lambda its: "x")
    res = _invoke(None, ["search"])
    assert res.exit_code != 0
    assert "0 SLC items found" in res.output


# --- download ------------------------------------------------------------


def test_download_interactive_prompts_range(patched):
    res = _invoke(
        patched, ["download", "--point", "-121.87,37.31", "--dest", "out"], input_="all\n"
    )
    assert res.exit_code == 0
    assert "Enter range" in res.output
    assert "TABLE" in res.output
    # both items downloaded
    assert patched["download"] == ["S1", "S2"]
    assert patched["find"][0]["point"] == (-121.87, 37.31)


def test_download_interactive_prompts_dest(patched, monkeypatch):
    # Simulate interactive mode so the dest prompt fires when --dest is omitted.
    monkeypatch.setattr("capella.cli._is_interactive", lambda: True)
    res = _invoke(
        patched,
        ["download", "--point", "-121.87,37.31", "--yes"],
        input_="all\nmydest\n",
    )
    assert res.exit_code == 0
    assert "Download destination directory" in res.output
    # the prompted dest is used for every item
    assert patched["download"] == ["S1", "S2"]
    assert patched["dest"] == ["mydest", "mydest"]


def test_download_noninteractive_no_dest_defaults_to_data(patched):
    # Non-TTY, no --dest -> ./data default, no prompt.
    res = _invoke(
        patched,
        ["download", "--point", "-121.87,37.31", "--range", "all", "--yes"],
    )
    assert res.exit_code == 0
    assert "Download destination directory" not in res.output
    assert patched["dest"] == ["./data", "./data"]


def test_download_noninteractive_yes(patched):
    res = _invoke(
        patched,
        ["download", "--point", "-121.87,37.31", "--range", "all", "--yes", "--dest", "out"],
    )
    assert res.exit_code == 0
    assert patched["download"] == ["S1", "S2"]
    # no range prompt
    assert "Enter range" not in res.output


def test_download_single_range(patched):
    res = _invoke(
        patched, ["download", "--point", "-121.87,37.31", "--range", "2", "--yes", "--dest", "out"]
    )
    assert res.exit_code == 0
    assert patched["download"] == ["S2"]


def test_download_zero_matches_exits_nonzero(monkeypatch):
    monkeypatch.setattr(catalog_mod, "find_slc_items", lambda **k: [])
    monkeypatch.setattr(download_mod, "format_item_table", lambda its: "x")
    res = _invoke(None, ["download", "--point", "0,0", "--range", "all", "--yes"])
    assert res.exit_code != 0
    assert "0 SLC items found" in res.output


def test_download_level_l2_prints_hint(patched):
    res = _invoke(patched, ["download", "--level", "L2", "--dest", "out"])
    assert res.exit_code == 0
    assert "derived locally" in res.output
    assert "capella l2" in res.output
    assert patched["find"] == []  # not searched
    assert patched["download"] == []  # nothing fetched


def test_download_no_ieee_contest_passthrough(patched):
    res = _invoke(
        patched,
        [
            "download",
            "--point",
            "-121.87,37.31",
            "--range",
            "all",
            "--yes",
            "--no-ieee-contest",
            "--dest",
            "out",
        ],
    )
    assert res.exit_code == 0
    assert patched["find"][0]["include_ieee_contest"] is False


def test_download_interactive_dest_strips_quotes(patched, monkeypatch):
    # A quoted path typed at the dest prompt (path with a space) must have the
    # surrounding quotes stripped before being passed to download_item.
    monkeypatch.setattr("capella.cli._is_interactive", lambda: True)
    res = _invoke(
        patched,
        ["download", "--point", "-121.87,37.31", "--yes"],
        input_='all\n"C:\\Users\\yjs\\Desktop\\data\\capellaL1\\San Jose"\n',
    )
    assert res.exit_code == 0
    assert patched["dest"] == [
        "C:\\Users\\yjs\\Desktop\\data\\capellaL1\\San Jose",
        "C:\\Users\\yjs\\Desktop\\data\\capellaL1\\San Jose",
    ]


def test_download_dest_flag_strips_quotes(patched):
    # A quoted path passed via --dest is also stripped (Windows shell quoting edge cases).
    res = _invoke(
        patched,
        [
            "download",
            "--point",
            "-121.87,37.31",
            "--range",
            "all",
            "--yes",
            "--dest",
            "'C:\\data\\San Jose'",
        ],
    )
    assert res.exit_code == 0
    assert patched["dest"] == ["C:\\data\\San Jose", "C:\\data\\San Jose"]


def test_download_noninteractive_requires_area(monkeypatch):
    monkeypatch.setattr(catalog_mod, "find_slc_items", lambda **k: [])
    monkeypatch.setattr(download_mod, "format_item_table", lambda its: "x")
    res = _invoke(None, ["download", "--range", "all", "--yes", "--dest", "out"])
    # no --bbox/--point and stdin not a TTY -> UsageError
    assert res.exit_code != 0
    assert "required" in res.output.lower()
