# Growth review corrections — offline verification

Base: `f72f98e`; branch: `fix/growth-review-corrections`.

Scope: only growth failure propagation and regression tests. No production access,
real accounts/network, dependency/header/payload/client-version changes, push,
merge, or deployment.

## Corrections

- Preserve terminal HTTP/business status through list reads, accept readback,
  Web fallback, and claim/report stages. Accept returns explicit `terminal`;
  runners stop further account writes across chunks, origins and task families.
  Known stop codes: 401, 403, 429, 11140, 14003, 6004, 6005, 10001.
  This is invocation-local stopping, not permanent account-ban classification.
- Keep Web business/HTTP rejection diagnostics, including HTTP 400 bodies
  containing terminal business codes.
- Progress errors return numeric-compatible zero with call-local failure
  metadata. `run_growth_tasks` returns structured failure, preserving previously
  verified earnings. Real API route and dashboard runner tests verify failure
  propagation without changing those consumers or using global error state.

## RED → GREEN evidence

Logs outside the worktree:
`/root/.hermes/cache/scratch/wb2hub-growth-review-logs/`

- `red-accept.log` → `green-accept.log`: later chunks continued after terminal
  initial/Web/readback failures; Web business diagnostics disappeared.
- `red-runner.log` → `green-runner.log`: later origins/families continued after
  terminal acceptance/discovery/post-accept read failure.
- `red-progress.log` → `green-progress.log`: progress errors lost structured
  status and allowed later family writes.
- `red-stages.log` → `green-stages.log`: report/claim/Web claim/zero-receipt
  readback terminal failures permitted later writes.
- `red-final-boundaries.log`: HTTP 400 terminal business body and outer MP
  terminal propagation gaps, corrected and covered by final maintenance run.
- `red-baseline-progress-status.log`: exact untouched-base reproduction of
  `run_growth_tasks(ok=True)` after a nonterminal progress read error.
- `red-web-diagnostic.log`: Web HTTP business body diagnostic lost; corrected
  and covered by final maintenance run.

## Final results

All executions used `wb2hub:integration-hardening-tests`, `--network none`,
`--cap-drop ALL`, read-only container filesystem and read-only `/src` source
mount (not `/app`). Writable data was disposable tmpfs or the log directory;
no account/usage production mounts.

- `maintenance-final.log`: **121 tests passed**, no skips.
  Command: `python -m unittest discover -s tests -p 'test_*.py'`.
- `archived/summary.json`, `archived-final-run.log`: **15/17 suites passed**,
  no skips/errors. Includes the nine reviewed baseline allowlist suites plus
  growth prerequisite, task cache, participation, streak, daily fallback,
  manual claim, task sections and task toggle suites.
- Two archived suites retain pre-existing failures:
  `_test_daily_chat_activity_fallback.py` (two old fallback assertions lacking
  heatmap evidence) and `_test_manual_claim.js` (old manual-button markup
  assertion). Both were rerun on untouched `f72f98e`; extracted failure lines
  and messages match exactly. See `archived-baseline/` and
  `archived-baseline-run.log`. No unrelated test/runtime changes made to hide
  these failures.
- `git diff --check` passed. AST comparison against base confirms unchanged
  header/version/spec constants and event/header builders.
