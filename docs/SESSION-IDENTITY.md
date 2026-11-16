# Client session identity and routing isolation

## Compatibility contract

`wb_proxy.extract_session_key` accepts only nonblank strings (outer whitespace
trimmed; embedded ASCII control characters rejected). Malformed values are
ignored, not stringified. HTTP header names are case-insensitive, including
plain-dict callers used by tests. Non-object `metadata` is ignored safely.

Existing valid identifiers keep their relative and absolute precedence over the
new fallbacks:

1. `X-Conversation-Id`
2. `Conversation-Id`
3. `X-Session-Id`
4. `Session-Id`
5. body `conversation_id`
6. body `session_id`
7. `metadata.conversation_id`
8. `X-Claude-Code-Session-Id` (new)
9. `metadata.conversationId` (new)
10. `metadata.session_id` (new)
11. `metadata.sessionId` (new)

`metadata.user_id` and `installID` do not identify a conversation. Messages
extracts from the original request, before its Chat translation loses optional
identity fields. Chat, Messages, Responses, compact Responses, their unversioned
aliases, and the existing completions aliases use the same extraction contract.

## Routing is not client identity

`wb_session` is an independently implemented Python routing namespace. An
already authenticated key entry establishes a request-local `ContextVar`; it
is reset in `finally` after the entire dispatch, including streaming and tool
follow-ups. No unauthenticated header establishes the namespace. The key
settings ID and credential are hashed together so key rotation cannot inherit
old bindings; the raw credential is not retained by the context. Panel/keyless
requests share the deliberate anonymous namespace.

Account affinity is keyed by a domain-separated hash of authenticated principal,
**effective upstream realm**, and session. This is computed inside each actual
open, so cross-realm fallback obtains a different binding. WorkBuddy prefix
fallback and long-conversation page calculation remain unchanged; the resulting
prefix key is scoped before account selection. An absent fallback still means
no binding. CodeArts scopes only its existing explicit session affinity.

The raw session remains separate: it still feeds the existing account headers,
WorkBuddy conversation-ID derivation, and CodeArts chat-ID/body derivation. The
internal `route-v1-...` key is used only for pool selection/bind/demote, never as
an upstream client session ID. This is binding isolation, not a promise that
different clients cannot legitimately select the same upstream account or that
upstream raw session IDs are tenant-namespaced. Account/model allowlists and
pinned-account permissions are unchanged. This adds no transcript/statestore,
`previous_response_id` support, instruction-role rewriting, or model selection
changes. Cache and logging functions are unchanged.

## Source attribution and license boundary

Required Notice: Copyright (c) 2026 NextAgentX (https://github.com/NextAgentX/qoder-trae-workbuddy-switch)

Pinned source: `NextAgentX/qoder-trae-workbuddy-switch`, commit
`febc1b7613847268c1f8b8aa256fefd0c8bb3031`, file
`crates/buddy-switch-gateway/src/session_headers.rs`.

The session-header and string-only metadata compatibility logic in
`wb_proxy.extract_session_key` (including its nested validation helper) is
adapted from the Rust functions `session_key_of`, `header_text`, and
`body_session_id_of`. Its local precedence deliberately differs to preserve
wb2hub clients' existing identifiers. These adapted functions are covered by
**PolyForm Noncommercial License 1.0.0**, not MIT:
<https://polyformproject.org/licenses/noncommercial/1.0.0>.
The source repository's `LICENSE` at the pinned commit is the controlling
source license. Personal noncommercial reuse was approved for this integration;
this is not permission for commercial reuse or MIT relicensing.

`wb_session.py`, routing call-site changes, and regression tests are new local
implementations, not translations of Rust routing code. Central LICENSE/NOTICE
integration and copying the complete pinned source license are handled by the
parent integration task, not this isolated change.

## Verification

`tests/test_session_identity.py` exercises real loopback HTTP authentication,
dispatch, translations, pool binding, and synthetic upstream SSE completion
without accounts or external network access. It covers string/metadata safety,
all routing aliases, key/realm/session binding separation, streaming, CodeArts,
upstream raw identity, credential rotation/context cleanup, account permission
changes, stateless Responses rejection, and prefix/page compatibility.

Run only in the credential-free offline image, with source read-only at `/src`:

```sh
docker run --rm --network none --cap-drop ALL \
  -v "$PWD:/src:ro" -v "$EVIDENCE:/results" -w /src \
  -e TMPDIR=/results -e PYTHONPATH=/src \
  -e ACCOUNTS_DIR=/results/accounts -e WB_PROXY_USAGE_DIR=/results/usage \
  -e WB_TLS_FINGERPRINT=python \
  wb2hub:integration-hardening-tests \
  python -m unittest discover -s tests -p test_session_identity.py -v
```

No runtime modules are imported on the host. The archived tests and hand-made
pool stubs retain their existing signatures: principal scoping is request-local,
not a new required kwarg. Focused archived affinity/page, realm fallback,
CodeArts, identity, account permission, model-alias and upstream tests were also
exercised. Two archived failures reproduce unchanged on baseline `782f438`:
`_test_pinned_routes.py::test_pinned_403_does_not_rotate` (no usable account)
and `_test_restricted_403.py::test_real_restriction_body_matches` (historical
string-only `11140` expectation). They are not silently redefined here.
