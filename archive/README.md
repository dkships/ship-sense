# Archive

Dead code, kept for the record. Nothing here is imported by live `src/`,
`scripts/`, or the `Makefile`, and `pytest.ini` (`testpaths = tests`) does not
collect `archive/tests/`, so nothing here runs in CI either. Moved with `git mv`
during the 2026-09-22 v4.0 debt cleanup (see `notes/v4.0-2026-09-22/audit/audit_debt.md`
and `notes/v4.0-2026-09-22/SPEC.md` §8).

## What's here and why

- **`semantic_*.py` + `revision_*.py`** (`archive/src/`, tests in `archive/tests/`) —
  the September 2026 automated LLM-judge regrade. Three independent-provider
  screens (`semantic_*`) and its sealed successor (`revision_*`) both completed
  and failed their pre-registered validation gates. History: `AUTOMATED_GRADING.md`,
  `REVISED_GRADING.md`, `REVISION_RESULTS.md`, `SCREENING_RESULTS.md`
  (`docs/history/v3.5/`), `CORRECTIONS.md`.
- **`offline_audit.py`** — the counterfactual/diagnostic tool for the failed
  semantic/revision screens above. Only ever invoked by hand; no Makefile target.
- **`workflow_batch.py`, `workflow_page.py`, `workflow_score.py`** — the v4
  workflow draft (paired evidence checks + cross-provider reference panel).
  Reference qualification failed (28/72 reviews passed) on 2026-09-06; no
  pilot or subject batches were submitted. See `docs/history/v4-workflow-draft/NEXT_VERSION.md`.
- **`release_page.py`, `candidate_page.py`** — the v3.5 release/candidate page
  renderers used for the (also failed) v3.1 validation-candidate publication.
  `candidate_page`'s frozen historical output is still committed at
  `docs/history/v3.5/candidate*`; `archive/tests/test_candidate_page.py` checks
  that renderer output still matches that frozen record.
- **`judge_audit.py`** (`archive/src/`, tests in `archive/tests/`) — a model-jury
  audit (blinded judge requests, flag records) built 2026-07 and never run on a
  real case. Its job was taken by the registered cross-lab reviewer panel
  (`panel_batch.py` / `panel_validate.py`). It never changed a score.
- **`findings.py`** (`archive/src/`) — per-model over-ship / over-refuse tally of
  Restraint misses. It predates list-valued Restraint labels, so it fails on the
  v4.6 bank (`AttributeError` on a list label); no published number depends on it.
- **`archive/scripts/check_workflow_site.py`** — a Playwright smoke-check for
  the workflow-draft site. `playwright` isn't in `requirements*.txt`; never
  wired to a Makefile target.

## Rules

- Do not import from `archive/` in live `src/`, `scripts/`, or tests under `tests/`.
- Do not add `archive/` to `pytest.ini` `testpaths`.
- Any future LLM-judge work needs a new pre-registered plan, not a resurrection
  of `semantic_*`/`revision_*`. See `AGENTS.md`.
