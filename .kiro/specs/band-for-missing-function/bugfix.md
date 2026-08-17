# Bugfix Requirements Document

## Introduction

`dashboard.py` imports `_band_for` from `render.py` at module load time, but `render.py` never defines that function. This causes an `ImportError` whenever the pipeline is started, making the entire application unusable. The fix is to extract the existing inline band-detection logic from `render.py`'s `_trackers()` into a standalone `_band_for(kind, value)` function in `render.py`, making it importable by `dashboard.py`.

## Bug Analysis

### Current Behavior (Defect)

1.1 WHEN the pipeline is started (e.g., `python -m src.run`) THEN the system raises `ImportError: cannot import name '_band_for' from 'src.render'` and halts immediately.

1.2 WHEN `src.dashboard` is imported by any module THEN the system fails at the `from .render import TRACKER_SPECS, _band_for, humanize_age, select_digest` statement, preventing any further execution.

### Expected Behavior (Correct)

2.1 WHEN the pipeline is started THEN the system SHALL successfully import `_band_for` from `src.render` without raising an `ImportError`.

2.2 WHEN `_band_for(kind, value)` is called with `kind="rain_prob"` and `value >= 70` THEN the system SHALL return a band dict `{"label": "Likely", "color": "#3b82f6"}`.

2.3 WHEN `_band_for(kind, value)` is called with any `kind` that has no matching band rule, or with a value below the threshold THEN the system SHALL return `None`.

### Unchanged Behavior (Regression Prevention)

3.1 WHEN `render.py`'s `_trackers()` builds the tracker list THEN the system SHALL CONTINUE TO populate each tracker's `band` field with the same value as before (a band dict for rain probability ≥ 70, `None` otherwise).

3.2 WHEN `render.py`'s `_trackers()` builds the tracker list THEN the system SHALL CONTINUE TO populate each tracker's `alert` field independently of the `band` field, using its own existing inline logic.

3.3 WHEN `select_digest()` is called THEN the system SHALL CONTINUE TO return a digest payload with tracker data structured identically to before the fix.

3.4 WHEN `dashboard.py`'s `_collect()` builds the metrics list THEN the system SHALL CONTINUE TO populate each metric's `band` field using the same band logic as `_trackers()` in `render.py`.
