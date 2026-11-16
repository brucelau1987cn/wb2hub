# Diagnostic output redaction boundaries

## Source, attribution and license

Required Notice: Copyright (c) 2026 NextAgentX (https://github.com/NextAgentX/qoder-trae-workbuddy-switch)

Reference revision: `febc1b7613847268c1f8b8aa256fefd0c8bb3031` of
[NextAgentX/qoder-trae-workbuddy-switch](https://github.com/NextAgentX/qoder-trae-workbuddy-switch):

- `crates/buddy-switch-gateway/src/logging.rs`: `request_failure_detail`,
  Unicode 4096-character bound and JWT-crossing-boundary regression.
- `crates/buddy-switch-core/src/modules/runtime_log.rs`: explicit secret-name,
  bearer and JWT output filtering; avoid blanket masking of `code` or `key`.

These are **PolyForm Noncommercial License 1.0.0** references, not MIT code:
<https://polyformproject.org/licenses/noncommercial/1.0.0>.
The original root `LICENSE` at the pinned revision contains the Required Notice
above and those terms. The user authorized this adaptation for personal,
noncommercial use; this is not a commercial-use permission or relicensing.
Keep the notice and license URL with redistributed adaptations. Root
LICENSE/NOTICE integration is handled separately by the parent change; this
branch deliberately does not edit central LICENSE, NOTICE or README.

`wb_diagnostics.py` and `tests/test_diagnostic_redaction.py` adapt the above
algorithm and boundary-test approach to Python. Existing caller modules retain
their existing provenance; their new diagnostic filtering calls use this module.
No Rust runtime, rotating log implementation, session logic or usage/cache
accounting was ported.

## Contract and adaptations

- `diagnostic_detail(value, limit=4096)` has no app import/startup, I/O, account,
  thread, environment or network side effects. It operates on an output copy.
- Full diagnostic text is filtered **before** slicing. Python Unicode code
  points are counted, not UTF-8 bytes; the ellipsis is included in the bound.
  Exact 4096-character strings are unchanged. `None` becomes an empty string.
- Recognize case-insensitive bearer/basic credentials, bare `eyJ` JWTs,
  `sk-` API keys, explicit token/password/API-key/client-secret fields in
  snake/camel/hyphen forms, quoted values with spaces and escapes, auth headers,
  and complete cookie/header lines. Explicit opaque prompt/body/messages/header
  assignments are omitted, not sanitized into a supposed safe request dump.
- JSON error objects (including objects embedded in an error prefix) are
  projected onto `code`, `error_code`, `status`, `statusCode`, `message`, `msg`,
  `displayMsg`, `reason`, `type`, `requestId`,  `request_id`, `error`, `detail`, `model`, `endpoint`, `method`.
  Only these diagnostic keys survive recursively; arrays are not copied.
  Unknown object fields, payloads and header objects are not logged. Malformed
  object suffixes are replaced with an explicit omission marker, not a fake
  upstream error. Filter strings before serialization so repeated sinks do not
  corrupt JSON or discard useful request IDs.
- Keep HTTP/business codes, model IDs, request IDs, numeric token counts and
  ordinary `key=pool` / status text. Do not guess that arbitrary long hex or
  base64 identifiers are secrets. Existing phone masking remains independent.
- Header lines (`:` or `=`), bearer/basic schemes, and explicit payload
  assignments are opaque through the end of their line. Unquoted credential
  assignments also consume the rest of the line: commas, semicolons, ampersands,
  spaces and braces are not trustworthy credential delimiters. Explicitly quoted
  values retain their quote boundary around the redaction marker, so repeated
  sinks cannot swallow a following diagnostic envelope. Useful metadata should
  precede opaque spans. Only the gateway's exact metric-only
  `messages: model=… stream=… msgs=… effort=… tools=… key=…` shape is exempted
  from the payload-label rule; arbitrary Messages content remains omitted.
- Object scanning processes opaque spans in source order before looking for a
  brace inside them. Actual JSON objects are decoded before any string filtering;
  `message: "body=…"` cannot break JSON or remove sibling code/request ID fields.
- Long projected envelopes are bounded by shortening string values before JSON
  serialization (including escaping overhead), never by slicing JSON syntax.
  Repeated filtering preserves the bounded representation, scalar codes, request
  IDs and reason prefixes when they fit. If the envelope's fixed syntax alone
  exceeds an unusually tiny limit, a valid empty object is used where it fits;
  metadata preservation is not possible under every caller-selected limit.
- This is defense in depth, **not** a guarantee that arbitrary free text is
  safe. Do not pass prompts, request bodies or HTTP headers to logging.

## Connected output boundaries

- `wb_proxy.log` console queue and `add_log_entry` memory/SQLite input;
  existing asynchronous writer and log classification are retained.
- `record_error` request/usage error message and console summary now use a full
  filtered detail instead of independent raw 200/180-character prefixes.
  Its direct handler callers no longer pre-slice exception details. Status,
  outcome, model/key/account/realm attribution and usage extraction are untouched.
- `exc_detail` filters before its existing caller-selected smaller bound.
- Account `last_error` / `last_error_detail` setters and notes store only a
  filtered copy. Account fault classification/cooldown logic still sees the
  original inputs; client responses and parser bodies are not rewritten.
- `get_logs` applies a copy-only runtime-entry allowlist to memory and legacy
  SQLite results, filtering strings on read. `/logs` and plaintext
  `/logs/export` therefore share this boundary, including old unsanitized rows.
  Existing log timestamps, totals, IDs and selection rules remain.
- HTTP access log drops the entire query (not merely known secret parameters),
  preserving method/path/version/status. No request/header/body capture is added.
- Scheduler's private ring uses the helper, not just its gateway-forwarded copy.
- Activity history calls the shared helper first, then retains its pre-existing
  stricter historical masking and 200-character single-line schema.

## Tests and isolation

`tests/test_diagnostic_redaction.py` uses real AST-extracted gateway functions
and account/scheduler methods, the real console writer loop, and a real isolated
SQLite LogStore. It does not import the gateway application or start a listener.
Synthetic values are explicitly named FAKE/PLACEHOLDER/NOTASECRET. Coverage
includes secrets crossing old short bounds and the new Unicode 4096 bound,
Chinese text, JSON payload/header exclusion, read-only legacy export, repeated
filtering, cookie/auth suffixes, and useful metadata/non-secret preservation.

Runs use the existing offline `wb2hub:integration-hardening-tests` image,
`--network none --cap-drop ALL`, read-only `/src`, and disposable data directories.
No production accounts, mounts, network probes, pushes, merges or deployment.
TDD red/green transcripts and broader suite logs are recorded outside the tree
under `/root/.hermes/cache/scratch/wb2hub-logs-evidence/`.

Original implementation evidence (before this independent-review follow-up):
- New redaction suite: **19 tests passed** (`28-green.log`).
- Maintenance discovery: **143 tests passed**, no skips (`29-maintenance-final.log`).
- Archived nickname/privacy: **38 tests passed** (`30-privacy-final.log`).
- Reviewed upstream allowlist: **9 suites passed**, no skips/errors/failures
  (`31-upstream-final.log`, `upstream-verified/` machine-readable report).
- Archived activity history: **40 tests run, 1 failure**; the same assertion
  fails on unchanged baseline `782f438` (`_test_activity_history.py.log` versus
  `16-baseline-activity.log`). This is not counted as passing.
- `git diff --check` passed. AST comparison confirms no session/affinity or
  cache extraction/aggregation function was edited.

## Independent-review follow-up verification

Baseline `d2d8b1c`, branch `fix/buddy-log-review`. New regressions exercise real
console output, the in-memory buffer, real SQLite writes, and both memory/SQLite
read filtering. RED/GREEN pairs reproduce and fix brace-bearing credentials,
ambiguous unquoted credential boundaries and equals-delimited headers, JSON
payload-label mentions/metric summaries, and repeated structured truncation.
Quoted markers followed by envelopes have an additional RED/GREEN pair.

Evidence: `/root/.hermes/cache/scratch/wb2hub-log-review-evidence/`:

- `01-high1-red.log` / `02-high1-green.log`
- `03-high2-red.log` / `04-high2-green.log`
- `06-med3-red.log` / `07-med3-green.log`
- `08-med4-red.log` / `09-med4-green.log`
- `14-marker-red.log` / `15-marker-green.log`
- `17-maintenance-final.log`: **174 tests passed**, including **26 diagnostic
  regressions**, no skips.
- `18-allowlist-final.log` and `upstream-final/summary.json`: **9 suites passed**,
  no skips/errors/failures.
- `19-privacy-final.log`: **38 tests passed**.

All ran in existing `wb2hub:buddy-upgrade-tests` with `--network none`,
`--cap-drop ALL`, `no-new-privileges`, read-only `/src`, disposable output,
no production accounts/mounts or external network. Only the standalone diagnostic
helper, its tests and this document changed; parser/client bodies, account fault
classification, cache/session functions and central licenses are untouched.

## Explicit limits / not claimed covered

- Existing `http_error_detail` / `read_http_error` may already read/truncate raw
  upstream bodies to 600 bytes (or another caller-selected bound) before a
  diagnostic arrives here. This change does not alter parser/client-body
  contracts; a bare JWT already cut before its second dot cannot be reliably
  recognized. Complete text passed to this helper is filtered before truncation.
- Caller audit on this review baseline also found `str(exc)[:120]` passed to
  `charge_account` and `account.note_error` in the dispatch unknown-error path,
  and `str(exc)[:80]` passed to `account.note_error` in `test_account`. These are
  deliberately **not** changed: `note_error` derives `token_expired` from its
  original argument, so simply removing the slice or passing sanitized text
  changes fault classification. `test_account` also passes the existing
  80-character `http_error_message` label to the setter with separately bounded
  raw detail. Already-cut unlabelled/JWT fragments can therefore remain
  unrecognizable in those account-state paths. The complete-input setter tests
  do not prove completeness of these callers. A future split between a bounded
  classification label and full diagnostic input needs its own behavior tests.
- Existing client-facing `/v1` errors are unchanged; this is not a global error
  rewrite, and must not be described as making client payloads secret-free.
- Existing explicitly opted-in `WB_DUMP_11_128=full` evidence files can contain
  raw prompts/bodies. The separate dump's summary/header capture, old usage
  JSONL files and on-disk legacy SQLite rows are not migrated or erased here.
  Ordinary runtime log reads/exports are filtered; historical usage exports are
  not comprehensively audited in this branch.
- Pricing/credits/update/CodeArts private rings and status fields, task-returned
  text and independent module stderr/CLI output are not comprehensively wired
  through the new helper. Gateway-forwarded runtime logs are covered, but do
  not infer their separate private/status outputs are covered.
- Existing activity-history hex/base64 masking is deliberately retained for
  compatibility; it is stricter than the new helper and may mask identifiers.
- No secret detector can identify arbitrary unlabelled passwords, novel opaque
  credentials, arbitrary prose echoing a prompt, or unknown encodings. Do not
  expand diagnostic metadata with request content on that assumption.
- The archived `_test_activity_history.py` has one pre-existing intl daily-chat
  assertion failure (its failure stub falls through to activity fallback).
  Baseline `782f438` reproduces the same failure. No business behavior or old
  assertion was changed to make that archive green.
