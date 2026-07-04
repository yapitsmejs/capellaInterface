from __future__ import annotations

import pytest

from capella.levels import (
    DERIVED_LEVELS,
    DOWNLOAD_PRODUCTS,
    LEVEL_INPUT,
    Level,
    input_level,
    is_derived,
    is_downloadable,
    products_for,
    transform_command_hint,
)


def test_download_products_only_l1():
    assert set(DOWNLOAD_PRODUCTS) == {Level.L1}
    assert DOWNLOAD_PRODUCTS[Level.L1] == ("slc",)


def test_derived_levels():
    assert DERIVED_LEVELS == {Level.L2, Level.L2LCR, Level.L2ML}


def test_level_input_chain():
    assert LEVEL_INPUT[Level.L2] == Level.L1
    assert LEVEL_INPUT[Level.L2LCR] == Level.L1
    assert LEVEL_INPUT[Level.L2ML] == Level.L2LCR


@pytest.mark.parametrize(
    "level,products,derived,downloadable,predecessor",
    [
        (Level.L1, ("slc",), False, True, None),
        (Level.L2, (), True, False, Level.L1),
        (Level.L2LCR, (), True, False, Level.L1),
        (Level.L2ML, (), True, False, Level.L2LCR),
    ],
)
def test_helpers(level, products, derived, downloadable, predecessor):
    assert products_for(level) == products
    assert is_derived(level) is derived
    assert is_downloadable(level) is downloadable
    assert input_level(level) == predecessor


def test_transform_command_hint():
    assert transform_command_hint(Level.L1) is None
    assert "capella l2" in transform_command_hint(Level.L2)
    assert "capella l2lcr" in transform_command_hint(Level.L2LCR)
    assert "capella l2ml" in transform_command_hint(Level.L2ML)
