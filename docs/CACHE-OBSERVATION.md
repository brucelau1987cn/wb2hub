# Cache observation semantics

## Reference and adaptation

Design reference: **NextAgentX/qoder-trae-workbuddy-switch**, commit
`febc1b7613847268c1f8b8aa256fefd0c8bb3031` (`febc1b7`), particularly
`crates/buddy-switch-gateway/src/protocol/usage_tap.rs` and that project's
request logging/KPI distinction between `None` and `Some(0)` cache usage.
Source license: **PolyForm Noncommercial 1.0.0**. This personal, noncommercial
adaptation is not a relicensing of that source under MIT. Central license and
notice integration is handled separately by the parent change. Python
implementation and tests here are written for wb2hub's existing consumers;
Rust code is not copied verbatim. Unlike the reference's first-present alias
lookup, wb2hub retains its first-positive-within-frame compatibility behavior.

## Recorded and API contract

- New request/error rows always carry boolean `cache_observed`.
- Missing/invalid cache fields are unknown (`false`), not known misses.
- Explicit valid zero is a known miss (`true`); positive is a known hit.
- `cached_tokens` remains an integer for existing strict clients. Responses and
  Messages numeric usage keep compatible zeros with an additive observation flag.
- `cache_hit_pct` is absent/null when unknown or prompt denominator is zero.
- Legacy numeric `cached_tokens: 0`, including old derived `cache_hit_pct: 0`,
  is **unknown** because earlier writers synthesized both. A valid positive
  count, explicit boolean observation, or preserved non-synthetic raw cache
  alias is evidence. Historical JSONL files are not rewritten.
- Booleans, negative numbers, malformed containers/strings, NaN and infinity
  are not valid token observations. Existing numeric-string compatibility is
  retained; returned counts are integers.

## Stream and multi-round rules

Within a frame: prompt detail, prompt-cache hit, cache-read, numeric cached,
input detail, completion detail order; first positive wins over zero aliases.
Across cumulative frames: carry a missing observation. Zero on the same
previously authoritative field replaces a hit; zero on another compatibility
alias cannot erase it. Cache-only trailing frames still update observation,
independent of the existing cumulative total-token selection rule.

This is applied to Chat stream capture, nonstream SSE aggregation, Responses
stream capture and Messages stream capture. Token estimation never fabricates
a known cache miss and preserves earlier real evidence. Across internal tool
rounds, the complete request is known only when every included round is known;
missing data does not silently become a miss.

## Aggregate consumers and dashboard

Snapshot totals and model/realm/account buckets, per-account totals, analytics
summary/model/account/key buckets and time-series (scan/day/hour/minute paths)
carry additive `cache_observed_requests`, `cache_unknown_requests`, and
`cache_observed_prompt_tokens`. Analytics token-weighted rate uses only observed
prompt tokens. Performance samples use only observed per-row rates; old
synthesized zero rates are excluded. Recent-row API decorates legacy rows using
the same evidence rule.

Dashboard KPI and dimension tables distinguish `—` from `0.0%`, display observed
and unknown request counts, and use the observed prompt denominator. Account
cache cells show `—` without observations. Cache sparklines leave unknown gaps;
period comparisons use observed prompt totals. Matrix performance summary keeps
its existing sample-average metric; its samples now exclude unknown rows.

Aggregate checkpoint schema is **6**. Schema 5 per-kind files are rejected and
rebuilt from JSONL; payload validation also requires observation counters and
updated minute tuples. Old single-file payloads without those counters fail
shape validation. A schema bump changes no source logs, request history,
request/body limits, routing, session helpers, secret redaction or stateful
Responses behavior. Cost estimation remains the existing price-list estimate;
it is not a cache-observation rate and no billing policy is changed here.

## Offline verification

Tests execute in the existing `wb2hub:integration-hardening-tests` image with
`--network none --cap-drop ALL`, read-only source, temporary empty data, and no
production mounts/accounts. Coverage includes real AST functions, SSE frames,
Chat loopback HTTP streaming through the actual handler and persisted rows,
legacy JSONL API/aggregate readers, minute checkpoint roundtrip, and actual
JavaScript KPI/Canvas rendering with isolated DOM nodes under Node.

Evidence logs are kept outside the worktree at
`/root/.hermes/cache/scratch/cache-observation-logs/` (red/green cycles,
maintenance run, reviewed upstream allowlist, and targeted usage regressions).

Verified results:
- Maintenance discovery: **137 tests passed**, no skips.
- Reviewed upstream allowlist: **9 suites passed**, no skips.
- Targeted usage regressions: **8 suites passed** (aliases, aggregate fold,
  range, time series, minute ring, day/hour buckets and per-kind checkpoints).
- `git diff --check` passed. No push, merge, deployment or live account calls.

The first targeted archive run exposed fixed schema-5 expectations in the day
bucket suite (including its formerly "future schema 6" case). Those expectations
now pin schema 6, test rejection of schema 5, and derive the future version from
the current schema; the rerun passed. Intentional write-failure injection in the
per-kind checkpoint suite emits expected error logs but the suite passes.
