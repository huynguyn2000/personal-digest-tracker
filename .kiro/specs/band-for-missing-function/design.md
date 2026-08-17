# band-for-missing-function Bugfix Design

## Overview

`dashboard.py` imports `_band_for` from `render.py` at module load time, but `render.py` never defines that function. The fix is to extract the inline band-detection logic from `_trackers()` in `render.py` into a new standalone function `_band_for(kind, value)`, then call it from `_trackers()` so both `render.py` and `dashboard.py` share the same implementation. The change is minimal: one new function added, one inline block replaced with a call.

## Glossary

- **Bug_Condition (C)**: The condition that triggers the bug — `_band_for` is absent from `src.render`, so any import of `dashboard.py` (or `run.py`) raises `ImportError`.
- **Property (P)**: The desired behavior once fixed — `_band_for(kind, value)` is importable from `src.render` and returns the correct band dict or `None`.
- **Preservation**: The existing inline band logic inside `_trackers()` and the behavior of `select_digest()` must continue to produce the same tracker data as before.
- **`_band_for(kind, value)`**: The new function to add in `src/render.py` that accepts a tracker `kind` string (e.g. `"rain_prob"`) and a numeric `value`, and returns a band dict `{"label": ..., "color": ...}` or `None`.
- **`_trackers(conn, links)`**: The function in `src/render.py` that builds per-tracker dicts; contains the inline band logic that `_band_for` will replace.
- **`_collect(conn)`**: The function in `src/dashboard.py` that assembles dashboard data; calls `_band_for` to populate each metric's `band` field.
- **`spec["kind"]`**: The `kind` key in a `TRACKER_SPECS` entry (e.g. `"rain_prob"`). Currently only `hcmc_rain_prob` triggers a band, matched by its `name`. The fix unifies matching under `kind`.

## Bug Details

### Bug Condition

The bug manifests when any module that imports `src.dashboard` is loaded (e.g. `python -m src.run`). Python executes the `from .render import TRACKER_SPECS, _band_for, humanize_age, select_digest` statement at module load time. Because `_band_for` does not exist in `render.py`, Python immediately raises an `ImportError` and halts the process before any pipeline logic runs.

**Formal Specification:**
```
FUNCTION isBugCondition(input)
  INPUT: input of type ModuleImportEvent
  OUTPUT: boolean

  RETURN input.module == "src.dashboard"
         AND "_band_for" NOT IN dir(src.render)
END FUNCTION
```

### Examples

- **ImportError on `python -m src.run`**: `run.py` imports `dashboard.py`, which triggers `ImportError: cannot import name '_band_for' from 'src.render'`. The pipeline never starts.
- **ImportError on `python -m src.dashboard`**: Same failure when running the dashboard directly.
- **`_collect()` call to `_band_for("rain_prob", 75)` (expected, post-fix)**: Should return `{"label": "Likely", "color": "#3b82f6"}`.
- **`_collect()` call to `_band_for(None, 50)` (edge case, expected post-fix)**: `kind` is `None` (most specs lack a `kind` key), should return `None`.
- **`_collect()` call to `_band_for("rain_prob", 60)` (below threshold, expected post-fix)**: Value is below 70, should return `None`.

## Expected Behavior

### Preservation Requirements

**Unchanged Behaviors:**
- `_trackers()` in `render.py` must continue to set each tracker's `band` field to `{"label": "Likely", "color": "#3b82f6"}` when the tracker name is `hcmc_rain_prob` and `latest >= 70`, and `None` otherwise.
- `_trackers()` must continue to set each tracker's `alert` field using its own existing inline logic, independently of `band`.
- `select_digest()` must continue to return a digest payload with tracker data structured identically to before the fix.
- All other functions in `render.py` (`humanize_age`, `_spark`, `render_html`, `run`, etc.) must remain unchanged in behavior.

**Scope:**
All code paths that do NOT involve importing `src.dashboard` or calling `_band_for` are completely unaffected by this fix. The only observable changes are:
- A new `_band_for` function becomes available in `src.render`.
- The inline band assignment in `_trackers()` is replaced with a call to `_band_for`.

## Hypothesized Root Cause

Based on the bug description, the root cause is straightforward:

1. **Missing function definition**: `_band_for` was referenced in `dashboard.py` as though it were a shared utility in `render.py`, but was never added to `render.py`. The inline logic exists in `_trackers()` but was never extracted into a named function.

2. **No `kind` field on most `TRACKER_SPECS` entries**: `dashboard.py` calls `_band_for(spec.get("kind"), vals[-1])`, passing `None` for most specs. The function must handle `kind=None` gracefully (return `None`).

3. **Mismatch between `name` and `kind`**: The inline logic in `_trackers()` matches on `name == "hcmc_rain_prob"`, but `dashboard.py` calls `_band_for` with `spec.get("kind")`. The `hcmc_rain_prob` spec entry in `TRACKER_SPECS` has no explicit `kind` key, so `spec.get("kind")` returns `None`. This means `_band_for` as called from `dashboard.py` will always receive `kind=None` and will always return `None` — which is the intended behavior for the dashboard (band display in the dashboard uses the same threshold logic, but the spec entries don't have `kind` set). This is acceptable and matches the existing behavior where `_collect()` shows no band for most metrics.

## Correctness Properties

Property 1: Bug Condition - `_band_for` is importable and returns correct band

_For any_ import of `src.dashboard` (or any module that transitively imports it), the fixed `src.render` module SHALL export `_band_for` without raising `ImportError`, and `_band_for("rain_prob", value)` SHALL return `{"label": "Likely", "color": "#3b82f6"}` when `value >= 70` and `None` when `value < 70`.

**Validates: Requirements 2.1, 2.2, 2.3**

Property 2: Preservation - `_trackers()` band and alert fields unchanged

_For any_ database state, `_trackers()` in the fixed `render.py` SHALL produce exactly the same `band` and `alert` values for each tracker entry as the original `_trackers()` did before the fix, preserving all existing inline band and alert logic.

**Validates: Requirements 3.1, 3.2, 3.3, 3.4**

## Fix Implementation

### Changes Required

**File**: `src/render.py`

**Specific Changes**:

1. **Add `_band_for(kind, value)` function** (new function, placed near the top of the module after `_wmo`):
   ```python
   def _band_for(kind: str | None, value: float) -> dict | None:
       """Return a band dict for a tracker value, or None if no band applies."""
       if kind == "rain_prob" and value >= 70:
           return {"label": "Likely", "color": "#3b82f6"}
       return None
   ```

2. **Replace inline band logic in `_trackers()`**: Replace the existing inline block:
   ```python
   band = None
   if name == "hcmc_rain_prob" and latest >= 70:
       band = {"label": "Likely", "color": "#3b82f6"}
   ```
   with a call to the new function. Because `_trackers()` matches on `name` (not `kind`), derive `kind` from the spec or from the name directly:
   ```python
   kind = spec.get("kind") or ("rain_prob" if name == "hcmc_rain_prob" else None)
   band = _band_for(kind, latest)
   ```
   This preserves the existing behavior of `_trackers()` (band fires for `hcmc_rain_prob >= 70`) while routing through the shared function.

   **Note:** The `alert` block immediately following must remain unchanged.

No changes are required to `dashboard.py` — it already calls `_band_for` correctly once the function exists.

## Testing Strategy

### Validation Approach

The testing strategy follows a two-phase approach: first, surface the counterexample that demonstrates the bug on unfixed code (the `ImportError`), then verify the fix works correctly and preserves existing behavior.

### Exploratory Bug Condition Checking

**Goal**: Surface the `ImportError` counterexample that demonstrates the bug BEFORE implementing the fix. Confirm the root cause.

**Test Plan**: Attempt to import `src.dashboard` in a test environment and assert that no `ImportError` is raised. On unfixed code, this test will fail immediately with `ImportError: cannot import name '_band_for' from 'src.render'`.

**Test Cases**:
1. **Import test**: `import src.dashboard` — will raise `ImportError` on unfixed code.
2. **`_band_for` existence test**: `assert hasattr(src.render, '_band_for')` — will fail on unfixed code.
3. **`_band_for("rain_prob", 75)` returns band**: Call the function with a value above threshold — will fail (function doesn't exist) on unfixed code.
4. **`_band_for(None, 75)` returns None**: Call with `kind=None` (the common case in `dashboard.py`) — will fail on unfixed code.

**Expected Counterexamples**:
- `ImportError: cannot import name '_band_for' from 'src.render'` on any attempt to import `src.dashboard`.

### Fix Checking

**Goal**: Verify that for all inputs where the bug condition holds, the fixed function produces the expected behavior.

**Pseudocode:**
```
FOR ALL input WHERE isBugCondition(input) DO
  result := import(src.dashboard)  -- no ImportError raised
  ASSERT _band_for IS IN dir(src.render)
  ASSERT _band_for("rain_prob", 70) == {"label": "Likely", "color": "#3b82f6"}
  ASSERT _band_for("rain_prob", 75) == {"label": "Likely", "color": "#3b82f6"}
  ASSERT _band_for("rain_prob", 69.9) == None
  ASSERT _band_for(None, 99) == None
  ASSERT _band_for("unknown_kind", 99) == None
END FOR
```

### Preservation Checking

**Goal**: Verify that for all inputs where the bug condition does NOT hold, the fixed function produces the same result as the original function.

**Pseudocode:**
```
FOR ALL tracker_state WHERE NOT isBugCondition DO
  ASSERT _trackers_original(conn) == _trackers_fixed(conn)
  -- specifically: band field for hcmc_rain_prob >= 70 is {"label":"Likely","color":"#3b82f6"}
  -- and band field for all other trackers is None
END FOR
```

**Testing Approach**: Property-based testing is recommended for preservation checking because:
- It generates many tracker value combinations automatically.
- It verifies that the `band` field is unchanged across the full range of `hcmc_rain_prob` values (0–100).
- It confirms `alert` is never affected by the refactor.

**Test Cases**:
1. **`_trackers()` band field preservation**: For `hcmc_rain_prob` values generated across [0, 100], verify the `band` field matches the original inline logic.
2. **`_trackers()` alert field preservation**: Verify `alert` for `hcmc_rain_prob` is set independently of `band` and matches original behavior.
3. **`select_digest()` tracker structure preservation**: Verify the full tracker list structure in the digest payload is identical before and after the fix.

### Unit Tests

- Test `_band_for("rain_prob", 70)` returns `{"label": "Likely", "color": "#3b82f6"}`.
- Test `_band_for("rain_prob", 69.9)` returns `None`.
- Test `_band_for(None, 100)` returns `None` (handles missing `kind` gracefully).
- Test `_band_for("unknown", 100)` returns `None` (unknown kind).
- Test that `src.dashboard` can be imported without error after the fix.

### Property-Based Tests

- Generate random `hcmc_rain_prob` values in [0, 150] and assert `_band_for("rain_prob", v)` returns a band iff `v >= 70`.
- Generate random `kind` strings (excluding `"rain_prob"`) and any `value`, assert `_band_for(kind, value)` always returns `None`.
- For a mock DB with randomly generated `hcmc_rain_prob` values, assert the `band` field from `_trackers()` matches the direct output of `_band_for("rain_prob", value)`.

### Integration Tests

- Run `python -m src.dashboard` end-to-end (with a test DB) and assert it exits without error.
- Run `python -m src.run` (with a test DB) and assert no `ImportError` is raised at import time.
- Verify that the dashboard HTML output contains a band indicator for `hcmc_rain_prob` when its latest value is ≥ 70, and no band otherwise.
