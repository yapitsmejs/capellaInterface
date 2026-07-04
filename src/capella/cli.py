"""Capella CLI (Click).

Two commands for the SLC download milestone:

* ``capella search``  — discovery (no files written).
* ``capella download`` — guided fetch of the SLC bundle (L1).

Both search the `slc` product-type collection **and** the IEEE Data Contest 2026
collection by default (contest SLC items only, GEO pairs skipped, deduped by `item.id`).
Pass ``--no-ieee-contest`` to restrict to the `slc` product-type collection.

``download`` only ever fetches SLC (L1). Calling it with ``--level L2|L2LCR|L2ML`` prints
the matching local transform command instead of downloading.
"""

from __future__ import annotations

import json
import sys

import click

from . import catalog, download
from .levels import Level, is_downloadable, transform_command_hint


def _is_interactive() -> bool:
    """True when stdin is a TTY (interactive mode). Monkeypatched in tests; CliRunner
    swaps sys.stdin so a direct sys.stdin.isatty() check isn't testable."""
    return sys.stdin.isatty()


def _parse_bbox(s: str) -> list[float]:
    parts = [p.strip() for p in s.split(",")]
    if len(parts) != 4:
        raise click.UsageError(f"--bbox expects 4 values, got {s!r}")
    try:
        return [float(p) for p in parts]
    except ValueError as e:
        raise click.UsageError(f"--bbox not numeric: {s!r}") from e


def _parse_point(s: str) -> tuple[float, float]:
    parts = [p.strip() for p in s.split(",")]
    if len(parts) != 2:
        raise click.UsageError(f"--point expects lon,lat, got {s!r}")
    try:
        return float(parts[0]), float(parts[1])
    except ValueError as e:
        raise click.UsageError(f"--point not numeric: {s!r}") from e


def _strip_quotes(s: str) -> str:
    """Strip one matched pair of surrounding quotes from a path the user typed.

    At the interactive prompt (and sometimes via shell quoting on Windows) a path
    containing spaces is entered wrapped in quotes, e.g.
    ``"C:\\Users\\yjs\\Desktop\\data\\capellaL1\\San Jose"``. Click does not strip
    those for us, so they'd become part of the directory name. Only a balanced
    matched pair is removed — a single leading/trailing quote is preserved.
    """
    s = s.strip()
    if len(s) >= 2 and s[0] == s[-1] and s[0] in ('"', "'"):
        return s[1:-1]
    return s


def _parse_area(raw: str) -> tuple[list[float] | None, tuple[float, float] | None]:
    parts = [p.strip() for p in raw.split(",")]
    try:
        nums = [float(p) for p in parts]
    except ValueError as e:
        raise click.UsageError(f"unparseable area: {raw!r}") from e
    if len(nums) == 4:
        return nums, None
    if len(nums) == 2:
        return None, (nums[0], nums[1])
    raise click.UsageError(f"area must be bbox (4 values) or point (2 values): {raw!r}")


@click.group()
@click.version_option(package_name="capella-interface", message="%(version)s")
def main() -> None:
    """Capella SLC download + level transformations.

    SLC (L1) is downloaded from Capella open data; L2/L2LCR/L2ML are derived locally
    from L1. Pipeline order: L1 -> L2 -> L2LCR -> L2ML.

    SLC search includes the IEEE Data Contest 2026 collection by default (its SLC items
    only; GEO pairs are skipped, cross-source duplicates removed). Use --no-ieee-contest
    on search/download to restrict to the capella-open-data-slc collection.
    """


def _row(item) -> dict:  # type: ignore[no-untyped-def]
    return {
        "id": item.id,
        "datetime": item.datetime.isoformat() if item.datetime else None,
        "mode": catalog.item_mode(item),
        "pol": catalog.item_polarization(item),
        "size": catalog.item_download_size(item),
        "bbox": list(item.bbox) if item.bbox is not None else None,
    }


@main.command("search")
@click.option("--bbox", "bbox", default=None, help="minLon,minLat,maxLon,maxLat.")
@click.option("--point", "point", default=None, help="lon,lat.")
@click.option(
    "--datetime",
    "datetime_",
    default=None,
    help="YYYY-MM or start/end (e.g. 2025-08-01/2025-08-31).",
)
@click.option(
    "--mode", "mode", default=None, help="Instrument mode: spotlight|sliding_spotlight|stripmap."
)
@click.option("--limit", "limit", type=int, default=None, help="Max items to list.")
@click.option("--json", "as_json", is_flag=True, help="Emit machine-readable rows.")
@click.option("--no-ieee-contest", is_flag=True, help="Exclude IEEE Data Contest 2026 SLC items.")
def search(bbox, point, datetime_, mode, limit, as_json, no_ieee_contest) -> None:
    """Discover SLC scenes (no files written)."""
    bbox_v = _parse_bbox(bbox) if bbox is not None else None
    point_v = _parse_point(point) if point is not None else None
    items = catalog.find_slc_items(
        bbox=bbox_v,
        point=point_v,
        datetime=datetime_,
        instrument_mode=mode,
        max_items=limit,
        include_ieee_contest=not no_ieee_contest,
    )
    if not items:
        click.echo("0 SLC items found")
        sys.exit(1)
    if as_json:
        click.echo(json.dumps([_row(i) for i in items], indent=2))
    else:
        click.echo(download.format_item_table(items))


@main.command("download")
@click.option("--bbox", "bbox", default=None, help="minLon,minLat,maxLon,maxLat.")
@click.option("--point", "point", default=None, help="lon,lat.")
@click.option("--datetime", "datetime_", default=None, help="YYYY-MM or start/end range.")
@click.option("--mode", "mode", default=None, help="spotlight|sliding_spotlight|stripmap.")
@click.option("--range", "range_", default=None, help="3-8 | all | 5 (1-indexed).")
@click.option("--dest", "dest", default=None, help="Output directory (default ./data).")
@click.option("--yes", "yes", is_flag=True, help="Skip confirmation prompt.")
@click.option("--no-ieee-contest", is_flag=True, help="Exclude IEEE Data Contest 2026 SLC items.")
@click.option(
    "--level",
    "level",
    type=click.Choice([lv.value for lv in Level], case_sensitive=False),
    default="L1",
    help="Processing level to fetch (only L1 is downloadable).",
)
def download_cmd(bbox, point, datetime_, mode, range_, dest, yes, no_ieee_contest, level) -> None:
    """Guided fetch of the SLC bundle to <dest>/<item_id>/.

    Interactive when required input is missing and stdin is a TTY; flag-driven otherwise.
    """
    lvl = Level(level)
    if not is_downloadable(lvl):
        hint = transform_command_hint(lvl)
        click.echo(f"{lvl.value} is derived locally, not downloaded from Capella. Use: {hint}")
        return

    # 1. Area entry.
    bbox_v = _parse_bbox(bbox) if bbox is not None else None
    point_v = _parse_point(point) if point is not None else None
    if bbox_v is None and point_v is None:
        if _is_interactive():
            raw = click.prompt(
                "Enter area as bbox (minLon,minLat,maxLon,maxLat) or point (lon,lat)"
            )
            bbox_v, point_v = _parse_area(raw)
        else:
            raise click.UsageError("--bbox or --point is required in non-interactive mode")

    # 2. Search.
    items = catalog.find_slc_items(
        bbox=bbox_v,
        point=point_v,
        datetime=datetime_,
        instrument_mode=mode,
        include_ieee_contest=not no_ieee_contest,
    )
    if not items:
        click.echo("0 SLC items found")
        sys.exit(1)

    # 3. List.
    click.echo(download.format_item_table(items))
    n = len(items)

    # 4. Range.
    if range_ is None:
        range_ = click.prompt(
            "Enter range to download (e.g. 3-8, 'all', or a single N)", default="all"
        )
    try:
        idxs = download.parse_range(range_, n)
    except ValueError as e:
        raise click.UsageError(str(e)) from e
    selected = [items[i - 1] for i in idxs]

    # 4b. Download destination (prompted interactively when --dest omitted).
    if dest is None:
        if _is_interactive():
            dest = click.prompt("Download destination directory", default="./data")
        else:
            dest = "./data"
    dest = _strip_quotes(dest)

    # 5. Confirm.
    total = download.total_size(selected)
    size_str = (
        download._human_size(total) if total > 0 else "size unknown"
    )
    if not yes:
        if _is_interactive():
            if not click.confirm(
                f"Download {len(selected)} scene(s) (~{size_str}) to {dest}/<id>/?"
            ):
                click.echo("aborted")
                return
        # non-interactive without --yes: proceed (the user invoked `download` explicitly).

    # 6. Download.
    progress = sys.stderr.isatty()
    for item in selected:
        out_dir, results = download.download_item(item, dest, show_progress=progress)
        summary = ", ".join(f"{name}({status})" for name, status in results)
        click.echo(f"{item.id}: {summary} -> {out_dir}")


if __name__ == "__main__":  # pragma: no cover
    main()
