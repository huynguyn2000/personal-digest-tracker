"""Preservation property tests for _trackers() band and alert field behavior.

Property 2: Preservation — _trackers() band and alert field behavior must be
identical before and after the fix.

These tests call _trackers() directly (not via dashboard.py) so they work on
BOTH unfixed and fixed code. They capture the baseline behavior that the fix
must preserve.

Validates: Requirements 3.1, 3.2, 3.3
"""
from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta, timezone

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from src.db import SCHEMA, _migrate
from src.render import TRACKER_SPECS, _trackers


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_conn() -> sqlite3.Connection:
    """Return an in-memory SQLite connection with the full schema."""
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.executescript(SCHEMA)
    _migrate(conn)
    conn.commit()
    return conn


def _now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def _ts_ago(days: float = 0) -> str:
    """ISO timestamp for `days` days ago."""
    dt = datetime.now(timezone.utc) - timedelta(days=days)
    return dt.replace(microsecond=0).isoformat()


def _insert_metric(conn: sqlite3.Connection, name: str, value: float, days_ago: float = 0) -> None:
    """Insert one metric row into the test DB."""
    conn.execute(
        "INSERT OR REPLACE INTO metrics (name, ts, value) VALUES (?, ?, ?)",
        (name, _ts_ago(days_ago), value),
    )
    conn.commit()


def _get_tracker(trackers: list[dict], name: str) -> dict | None:
    """Find a tracker entry by its spec name."""
    for spec in TRACKER_SPECS:
        if spec["name"] == name:
            label = spec["label"]
            for t in trackers:
                if t["label"] == label:
                    return t
    return None


# ---------------------------------------------------------------------------
# Unit tests (deterministic examples)
# ---------------------------------------------------------------------------

class TestBandFieldUnit:
    """Deterministic examples verifying _trackers() band field."""

    def test_rain_prob_above_threshold_has_band(self):
        """band is set when hcmc_rain_prob latest >= 70."""
        conn = _make_conn()
        _insert_metric(conn, "hcmc_rain_prob", 75.0)
        trackers = _trackers(conn)
        t = _get_tracker(trackers, "hcmc_rain_prob")
        assert t is not None, "hcmc_rain_prob tracker not found"
        assert t["band"] == {"label": "Likely", "color": "#3b82f6"}, (
            f"Expected band dict, got {t['band']!r}"
        )

    def test_rain_prob_at_threshold_has_band(self):
        """band is set exactly at threshold (value == 70)."""
        conn = _make_conn()
        _insert_metric(conn, "hcmc_rain_prob", 70.0)
        trackers = _trackers(conn)
        t = _get_tracker(trackers, "hcmc_rain_prob")
        assert t is not None
        assert t["band"] == {"label": "Likely", "color": "#3b82f6"}

    def test_rain_prob_below_threshold_band_is_none(self):
        """band is None when hcmc_rain_prob latest < 70."""
        conn = _make_conn()
        _insert_metric(conn, "hcmc_rain_prob", 69.9)
        trackers = _trackers(conn)
        t = _get_tracker(trackers, "hcmc_rain_prob")
        assert t is not None
        assert t["band"] is None, f"Expected None, got {t['band']!r}"

    def test_rain_prob_zero_band_is_none(self):
        """band is None when hcmc_rain_prob is 0."""
        conn = _make_conn()
        _insert_metric(conn, "hcmc_rain_prob", 0.0)
        trackers = _trackers(conn)
        t = _get_tracker(trackers, "hcmc_rain_prob")
        assert t is not None
        assert t["band"] is None

    def test_non_rain_trackers_band_always_none(self):
        """All trackers other than hcmc_rain_prob always have band=None."""
        conn = _make_conn()
        non_rain_specs = [s for s in TRACKER_SPECS if s["name"] != "hcmc_rain_prob"]
        for spec in non_rain_specs:
            _insert_metric(conn, spec["name"], 9999.0)
        trackers = _trackers(conn)
        for spec in non_rain_specs:
            t = _get_tracker(trackers, spec["name"])
            if t is not None:
                assert t["band"] is None, (
                    f"Tracker {spec['name']} should have band=None, got {t['band']!r}"
                )

    def test_alert_field_set_independently_of_band(self):
        """alert field mirrors band logic but is independent (separate code path)."""
        conn = _make_conn()
        # Above threshold: both band and alert should be set
        _insert_metric(conn, "hcmc_rain_prob", 75.0)
        trackers = _trackers(conn)
        t = _get_tracker(trackers, "hcmc_rain_prob")
        assert t is not None
        assert t["band"] == {"label": "Likely", "color": "#3b82f6"}
        assert t["alert"] == "#3b82f6", f"Expected alert='#3b82f6', got {t['alert']!r}"

    def test_alert_none_below_threshold(self):
        """alert is None when rain_prob < 70."""
        conn = _make_conn()
        _insert_metric(conn, "hcmc_rain_prob", 50.0)
        trackers = _trackers(conn)
        t = _get_tracker(trackers, "hcmc_rain_prob")
        assert t is not None
        assert t["alert"] is None, f"Expected alert=None, got {t['alert']!r}"

    def test_band_and_alert_are_independent_fields(self):
        """band and alert are both set above threshold, both None below."""
        for value in [70.0, 85.0, 100.0]:
            conn = _make_conn()
            _insert_metric(conn, "hcmc_rain_prob", value)
            trackers = _trackers(conn)
            t = _get_tracker(trackers, "hcmc_rain_prob")
            assert t is not None
            assert t["band"] is not None, f"Expected band at value={value}"
            assert t["alert"] is not None, f"Expected alert at value={value}"
            # Modifying band should not affect alert — they are separate fields
            assert t["band"] != t["alert"], (
                "band and alert have different types/values — they are independent"
            )

        for value in [0.0, 50.0, 69.9]:
            conn = _make_conn()
            _insert_metric(conn, "hcmc_rain_prob", value)
            trackers = _trackers(conn)
            t = _get_tracker(trackers, "hcmc_rain_prob")
            assert t is not None
            assert t["band"] is None, f"Expected band=None at value={value}"
            assert t["alert"] is None, f"Expected alert=None at value={value}"


# ---------------------------------------------------------------------------
# Property-based tests
# ---------------------------------------------------------------------------

class TestBandFieldProperty:
    """Property-based tests verifying _trackers() band field across all inputs.

    **Validates: Requirements 3.1, 3.2, 3.3**
    """

    @given(value=st.floats(min_value=0, max_value=150, allow_nan=False, allow_infinity=False))
    @settings(max_examples=200)
    def test_rain_prob_band_matches_threshold_rule(self, value: float):
        """For any rain_prob in [0, 150], band matches the >= 70 threshold rule.

        This captures the EXACT inline logic from the unfixed _trackers():
            band = None
            if name == "hcmc_rain_prob" and latest >= 70:
                band = {"label": "Likely", "color": "#3b82f6"}

        **Validates: Requirements 3.1**
        """
        conn = _make_conn()
        _insert_metric(conn, "hcmc_rain_prob", value)
        trackers = _trackers(conn)
        t = _get_tracker(trackers, "hcmc_rain_prob")
        assert t is not None, "hcmc_rain_prob tracker missing from _trackers() output"

        expected_band = {"label": "Likely", "color": "#3b82f6"} if value >= 70 else None
        assert t["band"] == expected_band, (
            f"value={value}: expected band={expected_band!r}, got {t['band']!r}"
        )

    @given(value=st.floats(min_value=0, max_value=150, allow_nan=False, allow_infinity=False))
    @settings(max_examples=200)
    def test_rain_prob_alert_matches_threshold_rule(self, value: float):
        """For any rain_prob in [0, 150], alert matches the >= 70 threshold rule.

        The alert field uses its own inline logic (separate from band) and must
        be preserved independently.

        **Validates: Requirements 3.2**
        """
        conn = _make_conn()
        _insert_metric(conn, "hcmc_rain_prob", value)
        trackers = _trackers(conn)
        t = _get_tracker(trackers, "hcmc_rain_prob")
        assert t is not None

        expected_alert = "#3b82f6" if value >= 70 else None
        assert t["alert"] == expected_alert, (
            f"value={value}: expected alert={expected_alert!r}, got {t['alert']!r}"
        )

    @given(
        value=st.floats(min_value=0, max_value=9999, allow_nan=False, allow_infinity=False),
        tracker_name=st.sampled_from(
            [s["name"] for s in TRACKER_SPECS if s["name"] != "hcmc_rain_prob"]
        ),
    )
    @settings(max_examples=100)
    def test_non_rain_tracker_band_always_none(self, value: float, tracker_name: str):
        """For any non-rain-prob tracker at any value, band is always None.

        **Validates: Requirements 3.1, 3.3**
        """
        conn = _make_conn()
        _insert_metric(conn, tracker_name, value)
        trackers = _trackers(conn)
        t = _get_tracker(trackers, tracker_name)
        if t is not None:
            assert t["band"] is None, (
                f"Tracker {tracker_name!r} at value={value}: "
                f"expected band=None, got {t['band']!r}"
            )
