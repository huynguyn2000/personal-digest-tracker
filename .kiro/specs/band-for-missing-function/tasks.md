# Implementation Plan

- [ ] 1. Write bug condition exploration test
  - **Property 1: Bug Condition** - `_band_for` ImportError
  - **CRITICAL**: This test MUST FAIL on unfixed code — failure confirms the bug exists
  - **DO NOT attempt to fix the test or the code when it fails**
  - **NOTE**: This test encodes the expected behavior — it will validate the fix when it passes after implementation
  - **GOAL**: Surface the `ImportError` counterexample that demonstrates the bug exists
  - **Scoped PBT Approach**: The bug is deterministic — scope the property to the concrete failing case: any attempt to import `src.dashboard` raises `ImportError`
  - Create a test file (e.g., `tests/test_band_for_bug.py`) that:
    - Attempts `import src.dashboard` (or `from src.render import _band_for`) and asserts no `ImportError` is raised
    - Asserts `hasattr(src.render, '_band_for')` is `True`
    - Asserts `src.render._band_for("rain_prob", 75)` returns `{"label": "Likely", "color": "#3b82f6"}`
    - Asserts `src.render._band_for(None, 75)` returns `None`
  - Run test on UNFIXED code
  - **EXPECTED OUTCOME**: Test FAILS with `ImportError: cannot import name '_band_for' from 'src.render'` (this is correct — it proves the bug exists)
  - Document the counterexample: `ImportError: cannot import name '_band_for' from 'src.render'` on `import src.dashboard`
  - Mark task complete when test is written, run, and failure is documented
  - _Requirements: 1.1, 1.2_

- [ ] 2. Write preservation property tests (BEFORE implementing fix)
  - **Property 2: Preservation** - `_trackers()` band and alert field behavior
  - **IMPORTANT**: Follow observation-first methodology
  - Observe behavior on UNFIXED code for non-buggy inputs (i.e., call `_trackers()` directly — it does not raise an `ImportError`, only importing `dashboard.py` does)
  - Observe: `_trackers(conn)` returns a band of `{"label": "Likely", "color": "#3b82f6"}` for the `hcmc_rain_prob` tracker when its latest value is ≥ 70
  - Observe: `_trackers(conn)` returns `band=None` for the `hcmc_rain_prob` tracker when its latest value is < 70
  - Observe: `_trackers(conn)` returns `band=None` for all other trackers regardless of value
  - Observe: `_trackers(conn)` sets the `alert` field independently — its value is never affected by the band logic
  - Write property-based tests capturing these observed behavior patterns:
    - For `hcmc_rain_prob` values generated across [0, 150], assert the `band` field from `_trackers()` is `{"label": "Likely", "color": "#3b82f6"}` when value ≥ 70 and `None` when value < 70
    - For all other tracker names, assert `band` is always `None`
    - Assert the `alert` field for `hcmc_rain_prob` matches the original inline alert logic independently of `band`
  - Verify tests PASS on UNFIXED code (calls `_trackers()` directly, not `dashboard.py`)
  - **EXPECTED OUTCOME**: Tests PASS (this confirms baseline behavior to preserve)
  - Mark task complete when tests are written, run, and passing on unfixed code
  - _Requirements: 3.1, 3.2, 3.3_

- [ ] 3. Fix: add `_band_for` to `render.py` and update `_trackers()`

  - [ ] 3.1 Implement the fix in `src/render.py`
    - Add `_band_for(kind: str | None, value: float) -> dict | None` function after the `_wmo` function definition (before `humanize_age`):
      ```python
      def _band_for(kind: str | None, value: float) -> dict | None:
          """Return a band dict for a tracker value, or None if no band applies."""
          if kind == "rain_prob" and value >= 70:
              return {"label": "Likely", "color": "#3b82f6"}
          return None
      ```
    - Replace the existing inline band block in `_trackers()`:
      ```python
      band = None
      if name == "hcmc_rain_prob" and latest >= 70:
          band = {"label": "Likely", "color": "#3b82f6"}
      ```
      with:
      ```python
      kind = spec.get("kind") or ("rain_prob" if name == "hcmc_rain_prob" else None)
      band = _band_for(kind, latest)
      ```
    - Leave the `alert` block immediately following the band block entirely unchanged
    - Make no changes to `dashboard.py` — it already calls `_band_for` correctly
    - _Bug_Condition: isBugCondition(input) where input.module == "src.dashboard" AND "_band_for" NOT IN dir(src.render)_
    - _Expected_Behavior: `_band_for` is importable from `src.render`; `_band_for("rain_prob", value)` returns band dict when value >= 70, None otherwise; `_band_for(None, value)` always returns None_
    - _Preservation: `_trackers()` band field logic is functionally identical post-refactor; alert field logic unchanged; `select_digest()` tracker structure unchanged_
    - _Requirements: 2.1, 2.2, 2.3, 3.1, 3.2, 3.3, 3.4_

  - [ ] 3.2 Verify bug condition exploration test now passes
    - **Property 1: Expected Behavior** - `_band_for` ImportError resolved
    - **IMPORTANT**: Re-run the SAME test from task 1 — do NOT write a new test
    - The test from task 1 encodes the expected behavior: `src.dashboard` imports cleanly and `_band_for` returns correct values
    - Run bug condition exploration test from step 1
    - **EXPECTED OUTCOME**: Test PASSES (confirms the `ImportError` is fixed and `_band_for` behaves correctly)
    - _Requirements: 2.1, 2.2, 2.3_

  - [ ] 3.3 Verify preservation tests still pass
    - **Property 2: Preservation** - `_trackers()` band and alert field behavior
    - **IMPORTANT**: Re-run the SAME tests from task 2 — do NOT write new tests
    - Run preservation property tests from step 2
    - **EXPECTED OUTCOME**: Tests PASS (confirms no regressions in `_trackers()` band/alert logic)
    - Confirm all tests still pass after fix (no regressions)

- [ ] 4. Checkpoint — Ensure all tests pass
  - Run the full test suite and confirm both the bug condition exploration test and preservation tests pass
  - Optionally run `python -m src.dashboard` with a test DB to verify end-to-end the dashboard generates without error
  - Ensure all tests pass; ask the user if questions arise
