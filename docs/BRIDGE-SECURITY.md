# TLS bridge compression security update

## Scope and version choice

This is a source/build-chain change from maintenance baseline `665123f`, not a
production update. No Python implementation, Python test, archived release
workflow, preserved binary, account data, or deployment configuration is changed.

- `github.com/klauspost/compress`: `v1.17.4` → `v1.20.4`.
  The [official Go advisory](https://vuln.go.dev/ID/GO-2026-6865.json)
  (CVE-2026-116591) fixes the FSE/Huffman panic in `v1.20.2`.
  The Go module proxy reported `v1.20.4` as the current stable patch release
  (`2026-11-09T12:35:15Z`); choose that patched release, not an invented version.
- Module minimum: Go `1.24` → `1.25`, required by compress's `go.mod`.
  It becomes a direct requirement because the regression test imports FSE.
- Docker and CI: Go `1.24.13` → `1.26.11`. The official Go downloads feed
  listed stable `1.25.14`, `1.26.11`, and `1.27.4`. Use the supported previous
  stable series rather than the newest major, and verify compatibility on the
  minimum Go 1.25 series separately. Go 1.24 with `GOTOOLCHAIN=local` is no longer
  supported; no automatic toolchain download is allowed in the builder/CI.
- Official Docker image index, inspected and pulled from `docker.io/library/golang`:
  `golang:1.26.11-bookworm@sha256:dc40a13b61304647ed7e5d581d246b86413dfc29d63c6d018310feb93cd98fc0`.
  The index contains Linux amd64 and arm64 manifests. The native amd64 manifest is
  `sha256:79ecd0a7157c7a4cf3b281ced8465350591bf54fedab540e75374a003e1fde7e`;
  the arm64 manifest is
  `sha256:34769fb0ebf77b1a0baf783278c16d20c7970c946a0eff81b9b567c060c46760`.
  Registry metadata points to official docker-library/golang revision
  `f66ba4e64dfbab452b77f8ae045428aacadc24a2`.
- Both copies of the compress license are refreshed from the verified module's
  complete LICENSE: the imported old copies contained literal truncation markers.

Sources queried: [Go downloads JSON](https://go.dev/dl/?mode=json&include=all),
[module release metadata](https://proxy.golang.org/github.com/klauspost/compress/@latest),
[fixed version minimum](https://proxy.golang.org/github.com/klauspost/compress/@v/v1.20.2.mod),
[Docker official image](https://hub.docker.com/_/golang).

## Vulnerability versus actual bridge reachability

Real `govulncheck v1.9.0`, source/symbol scan on Linux amd64 using Go 1.26.11,
was run against an untouched copy of the original helper, then the updated helper.
The database reported last modified `2026-11-11T02:51:11Z`.

Before updating, GO-2026-6865 was a **module, imported-package, and static-symbol
finding**, not merely a vulnerable version string. The reported symbol traces
were:

```text
main → fmt.Fprintf → fmt.newPrinter → sync.Pool.Get → zstd.init$3
     → zstd.NewReader → Decoder.Reset → startStreamDecoder
     → blockDec.decodeLiterals → huff0.ReadTable → fse.Decompress
```

That trace mixes unrelated `sync.Pool` users; it is not evidence that printing
`LISTEN` decompresses hostile input. The legitimate uTLS certificate-decompression
route is guarded in `utls@v1.8.2/u_handshake_client.go:24-47` by both a
`UtlsCompressCertExtension` and advertised compression algorithms. The bridge's
`HelloCustom`/`officialHelloSpec` contains neither that extension nor ALPN. Its
existing captured-ClientHello regression passes with exactly the original ten
extensions. **No reachable remote exploit of this bridge has been demonstrated.**
Static symbol reachability is conservative and cannot establish those runtime
conditions. Do not label the old deployed bridge remotely exploitable from this
scan alone.

A dependency-level regression using the official fix's two FSE inputs was run
first with `v1.17.4`: it panicked with index `33077` outside table length `32768`.
With `v1.20.4` it rejects the malformed count and passes. This reproduces the
library bug, not a bridge/network exploit. Input attribution and the BSD license
are retained in `security_test.go` and `licenses/compress.txt`.

After updating, there are **no compress findings** (GO-2026-6865 and the older
GO-2026-5841 module finding are both removed). The full scan is **not clean**:

- GO-2026-6888 remains in `andybalholm/brotli v1.0.6` (fixed in `v1.2.7`).
  The scanner reports `serveClient → UConn.Handshake → ... → brotli.Reader.Read`,
  plus another conservative `fmt` trace. The same absent certificate-compression
  extension guards the real uTLS route. This is a separately tracked dependency
  finding, not proof of a reachable bridge exploit; Brotli was not upgraded in
  this compression-module/toolchain-only change.
- Unchanged `x/crypto` and `x/sys` dependencies have module-only findings (SSH,
  OpenPGP, Windows). No affected package/symbol reachability was reported for
  them in this Linux scan. The complete updated report has 22 unique advisory
  IDs, versus 24 before; only Brotli has remaining symbol-level findings.
- Text-format govulncheck exits `3` before and after due to symbol findings.
  JSON-format exits `0` while emitting findings; an exit-code-only check of the
  JSON scan would therefore be misleading. There is no all-vulnerabilities-fixed
  claim and no misleading green vulnerability CI gate.

The preserved `bin/wb-tls-bridge` remains the original rollback/comparison binary;
users running that file directly do **not** receive this fix. The default Docker
build uses the rebuilt helper, not the preserved binary.

## Verification

Dependencies and the scanner were downloaded in disposable containers first.
Actual Go checks and regressions then ran with `--network none`, capabilities
removed, no-new-privileges, and only this worktree/disposable evidence mounts.
No production accounts, directories, socket, or containers were mounted.

| Check | Result |
| --- | --- |
| Go 1.26.11 `go mod verify`, `go vet ./...`, `go test -v -count=1 ./...` | pass |
| Go 1.25.14 same checks, proving minimum-series compatibility | pass |
| CGO-disabled Linux amd64 + arm64 compile on both toolchains | pass |
| Full amd64 Docker runtime multistage build | pass |
| Docker arm64 `tls-bridge-artifact` target cross-build/export | pass |
| Docker helper versus independent same-toolchain builds, both architectures | byte-identical |
| Offline maintenance Python regressions, unchanged tests | 60 passed, no skips |
| Offline audited upstream allowlist | 9 suites passed, no skips |
| TLS fingerprint suite within allowlist | 21 tests passed, no skips |
| Built runtime standalone helper mode/start/parent-EOF exit | 0755 / LISTEN / exit 0 |
| Built runtime empty-account `/health` without external network | `ok=true`, `accounts=0` |

The empty-account service does not make an upstream request, so its own health
correctly reports `bridge_state=not_started`, not `ready`. Standalone helper
startup is reported separately; no production main-process bridge routing or
live upstream TLS success is claimed. Arm64 was compiled, not executed on ARM
hardware. No release, push, remote CI execution, merge, or deployment is included.

Builds retain pinned image/modules, `GOTOOLCHAIN=local`, `CGO_ENABLED=0`,
`-mod=readonly`, `-trimpath`, `-buildvcs=false`, native-builder cross-compilation,
and explicit executable mode. Runtime base/dependencies remain unchanged.

## Raw evidence

Absolute evidence root on the validation host:
`/root/.hermes/cache/scratch/wb2hub-security-evidence/`.

- `GO-2026-6865.json`, `GO-2026-6888.json`, `compress-security-fix.patch`
- `go-downloads.json`, `compress-latest.json`, `compress-v1.20.2.mod`
- `golang-image-inspect.txt`, `golang-image-manifest.json`, `golang-pull.log`
- `baseline-govulncheck.json`, `baseline-govulncheck.txt`,
  `baseline-govulncheck-text.log` (exit 3)
- `updated-govulncheck.json`, `updated-govulncheck.txt`,
  `updated-govulncheck.log` (JSON exit 0, text exit 3)
- `red-compression-regression.log`, `upgrade.log`, `offline-go-checks.log`,
  `minimum-go125.log`
- `docker-build-amd64-final.log`, `docker-build-arm64-artifact-final.log`,
  `docker-test-image-final.log`, `reproducibility.log`, `smoke.log`
- `vulnerability-summary.json` (parsed raw findings and exact symbol traces)
- `maintenance.log`, `upstream.log`, `results/upstream/summary.json`, and each
  suite's full log in `results/upstream/`

These are local validation artifacts, not links claiming a public/remote CI run.
