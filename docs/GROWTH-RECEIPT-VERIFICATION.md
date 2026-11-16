# Growth receipt verification

Scope: selective Python growth-center fixes on `665123f`; no deploy, credentials,
client-version changes, Dockerfile changes, or Go dependency changes.

## Contract

- `fetch_growth_tasks(account, mp=False)` returns only a successfully read list.
  Transport, JSON, business-envelope and missing-list failures raise `RuntimeError`.
  Missing acceptance status is `unknown`, not `not_accepted`.
- `discover_growth_tasks` queries default and MP, merges by exact task code,
  prefers default duplicates, marks MP-only rows `mp=True`, and returns partial
  errors explicitly. Partial results are not cached as successful snapshots.
- `accept_tasks` confirms enrollment from the same-origin list, not the receipt.
  Only code-zero + explicitly unchanged rows permit one Web accept attempt per
  invocation (even across chunks). Missing/failed/unknown reads and terminal
  401/403/429 do not authorize Web retries. The outer growth runner no longer
  repeats the entire accept sequence.
- `claim_task` requires a positive, finite, nonnegative numeric credit/energy
  receipt, without `already_claimed`, for `ok=True, reward_verified=True`.
  Zero/invalid receipts read back the originating list and distinguish `claimed`,
  `pending`, `unknown`, and `read_error`. All retain legacy `ok=False` and zero
  earned credit. `claimed` proves task state only, not a new credit grant.
- All Python claim consumers and the dashboard require explicit verification.
  Night earned-credit accounting reads the structured result, never log text.
  The manual claim route rediscovers origin server-side; it does not trust a
  client-supplied MP flag. Default/MP dedup prevents double execution; the existing
  PC `Sequential_Tasks_7` playbook handler remains the owner of that task.
- Existing event builders/payloads are unchanged. Library_read reuses the existing
  library event; only list/accept/progress/claim origin routing changes.
- CN/enterprise/task-participation/banned gates are retained. Manual growth and
  claim also check the existing realm hold before upstream reads/writes; scheduler
  realm-hold governance is unchanged.

## References and attribution

The receipt-readback/Web-fallback and MP-discovery approach was compared with
`jin66god/workbuddy2api-panel` v1.27.7 (`a71c9fc`), especially
`internal/panel/tasks.go`, `internal/panel/autotask.go`, and
`internal/upstream/tasks.go` (MIT). Python implementation and AST-extracted tests
were written for this repository; no Go source was copied. Existing upstream
attributions in `wb_tasks.py` remain intact.

## Offline evidence

New maintenance suite: `tests/test_growth_receipts.py` (real functions extracted
with AST, injected HTTP responses, no runtime import). It includes Node execution
of the real dashboard claim helper, both false-positive reward handling and a
positive receipt. Archived growth/cache suites have updated stub signatures and
explicit positive-reward evidence to match the new contract.

TDD RED/GREEN and verification logs live outside the repository at:
`/root/.hermes/cache/scratch/wb2hub-growth-logs/`.

Verified results:
- Maintenance discovery: 108 tests passed, including 48 growth receipt cases and
  executed dashboard helper checks (`35-maintenance-final.log`).
- Archived growth prerequisites: 17 tests passed (`30-upstream-growth-final.log`).
- Archived task-cache invalidation: 13 tests passed (`_test_tasks_cache.py.log`).
- Archived task participation: 14 tests passed (`33-participation-final.log`).
- Nickname privacy and dashboard handlers passed; the reviewed nine-suite upstream
  allowlist passed with zero failed/error/skipped suites (`allowlist/summary.json`).
- `git diff --check` passed. AST comparison against the base confirmed existing
  event builders and report helper bodies unchanged. Client versions, Dockerfile
  and Go module files have no diff against the base.

Container checks use the existing `wb2hub-eval:node` image with `--network none`,
`--cap-drop ALL`, read-only source at `/src`, no production mounts. Mounting the
worktree at `/app:ro` initially failed because that image declares an
`/app/accounts` volume; using `/src:ro` avoids that mount conflict.

These tests verify response classification, bounded writes, preserved payloads and
routing, not current upstream endpoint availability or real credit-pack grants.
There were no real account requests and no production changes.
