"""Processing-level taxonomy.

A single processing chain, each level built on the previous:

    L1 (SLC, download) -> L2 (geocode) -> L2LCR (look-register) -> L2ML (multi-look)

Only L1 is fetched from Capella. L2/L2LCR/L2ML are derived locally from SLC, so the
downloader refuses them and points the user at the matching transform command.
"""

from __future__ import annotations

from enum import StrEnum


class Level(StrEnum):
    """Capella processing levels handled by this tool."""

    L1 = "L1"  # slant, complex, single-look            [download: SLC]
    L2 = "L2"  # ground, complex, single-look           [derive from L1]
    L2LCR = "L2LCR"  # ground, registered across looks         [derive from L1]
    L2ML = "L2ML"  # ground, multi-looked, detected          [derive from L2LCR]


# Capella products fetched per level. Only L1 (= SLC) is downloaded.
DOWNLOAD_PRODUCTS: dict[Level, tuple[str, ...]] = {Level.L1: ("slc",)}

# Levels built locally from SLC (never downloaded).
DERIVED_LEVELS: frozenset[Level] = frozenset({Level.L2, Level.L2LCR, Level.L2ML})

# Direct predecessor in the chain (for CLI hints / dependency checks).
LEVEL_INPUT: dict[Level, Level] = {
    Level.L2: Level.L1,
    Level.L2LCR: Level.L1,
    Level.L2ML: Level.L2LCR,
}


def products_for(level: Level) -> tuple[str, ...]:
    """Capella product types downloaded for `level` (empty for derived levels)."""
    return DOWNLOAD_PRODUCTS.get(level, ())


def is_derived(level: Level) -> bool:
    """True if `level` is built locally rather than downloaded."""
    return level in DERIVED_LEVELS


def is_downloadable(level: Level) -> bool:
    """True if `level` is fetched directly from Capella (only L1)."""
    return level in DOWNLOAD_PRODUCTS


def input_level(level: Level) -> Level | None:
    """Direct predecessor of `level` in the chain, or None for L1."""
    return LEVEL_INPUT.get(level)


def transform_command_hint(level: Level) -> str | None:
    """The CLI command that derives `level` locally, or None for L1."""
    if level == Level.L1:
        return None
    if level == Level.L2:
        return "capella l2 <l1_scene_dir> --dem <dem.tif> --out l2.tif"
    if level == Level.L2LCR:
        return "capella l2lcr <l1_scene_dir> --dem <dem.tif> --out l2lcr.tif --n-looks 4"
    if level == Level.L2ML:
        return "capella l2ml <l2lcr_stack.tif> --out l2ml.tif"
    return None
