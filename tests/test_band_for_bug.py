"""Bug condition exploration test for the missing _band_for function.

Property 1: Bug Condition — _band_for is importable and returns correct band.

This test FAILS on unfixed code with:
    ImportError: cannot import name '_band_for' from 'src.render'

That failure is the expected counterexample confirming the bug exists.
After the fix is applied (Task 3), this same test should PASS.

Validates: Requirements 1.1, 1.2, 2.1, 2.2, 2.3
"""
from __future__ import annotations

import importlib
import sys


def test_src_dashboard_importable():
    """Importing src.dashboard must not raise ImportError.

    On unfixed code, this fails immediately with:
        ImportError: cannot import name '_band_for' from 'src.render'
    """
    # Remove cached modules to ensure a fresh import attempt.
    for mod in list(sys.modules.keys()):
        if mod.startswith("src."):
            del sys.modules[mod]

    # This will raise ImportError on unfixed code.
    import src.dashboard  # noqa: F401


def test_band_for_exists_in_render():
    """src.render must export _band_for."""
    import src.render as render
    assert hasattr(render, "_band_for"), (
        "_band_for is not defined in src.render — the bug is present"
    )


def test_band_for_returns_band_above_threshold():
    """_band_for('rain_prob', 75) must return the band dict."""
    from src.render import _band_for
    result = _band_for("rain_prob", 75)
    assert result == {"label": "Likely", "color": "#3b82f6"}, (
        f"Expected band dict, got {result!r}"
    )


def test_band_for_returns_band_at_threshold():
    """_band_for('rain_prob', 70) must return the band dict (boundary)."""
    from src.render import _band_for
    result = _band_for("rain_prob", 70)
    assert result == {"label": "Likely", "color": "#3b82f6"}, (
        f"Expected band dict at threshold=70, got {result!r}"
    )


def test_band_for_returns_none_below_threshold():
    """_band_for('rain_prob', 69.9) must return None."""
    from src.render import _band_for
    result = _band_for("rain_prob", 69.9)
    assert result is None, f"Expected None below threshold, got {result!r}"


def test_band_for_returns_none_for_none_kind():
    """_band_for(None, 75) must return None — handles missing kind gracefully."""
    from src.render import _band_for
    result = _band_for(None, 75)
    assert result is None, f"Expected None for kind=None, got {result!r}"


def test_band_for_returns_none_for_unknown_kind():
    """_band_for('unknown_kind', 99) must return None."""
    from src.render import _band_for
    result = _band_for("unknown_kind", 99)
    assert result is None, f"Expected None for unknown kind, got {result!r}"
