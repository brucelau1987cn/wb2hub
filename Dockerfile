# Build on the native builder; cross-compile without requiring target emulation.
FROM --platform=$BUILDPLATFORM golang:1.26.11-bookworm@sha256:dc40a13b61304647ed7e5d581d246b86413dfc29d63c6d018310feb93cd98fc0 AS tls-bridge-build
WORKDIR /src
ENV CGO_ENABLED=0 GOTOOLCHAIN=local
COPY tools/wb-tls-bridge/go.mod tools/wb-tls-bridge/go.sum ./
RUN go mod download && go mod verify
COPY tools/wb-tls-bridge/ ./
ARG TARGETOS
ARG TARGETARCH
RUN test "$TARGETOS" = linux && \
    case "$TARGETARCH" in amd64|arm64) ;; *) echo "unsupported TARGETARCH: $TARGETARCH" >&2; exit 1 ;; esac && \
    GOOS="$TARGETOS" GOARCH="$TARGETARCH" go build -mod=readonly -trimpath -buildvcs=false \
      -ldflags="-s -w" -o /out/wb-tls-bridge . && \
    chmod 0755 /out/wb-tls-bridge

# Optional local export: docker buildx build --target tls-bridge-artifact \
#   --platform linux/amd64 --output type=local,dest=./dist/tls-bridge .
FROM scratch AS tls-bridge-artifact
COPY --from=tls-bridge-build /out/wb-tls-bridge /wb-tls-bridge
COPY --from=tls-bridge-build /src/licenses/ /licenses/wb-tls-bridge/
COPY --from=tls-bridge-build /src/README.md /licenses/wb-tls-bridge/README.md

FROM python:3.11-alpine@sha256:7fbad8e4652eae0186429a6f26cf5b23e27e8c6fd7ac4d0cfd7a6988cdeea1e1
LABEL org.opencontainers.image.title="wb2hub"       org.opencontainers.image.source="https://github.com/brucelau1987cn/wb2hub"
ENV PYTHONUNBUFFERED=1 HOST=0.0.0.0 PORT=7863 TZ=Asia/Shanghai     WB_UPDATE_REPO=brucelau1987cn/wb2hub
WORKDIR /app
RUN apk add --no-cache tzdata ca-certificates
COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt
COPY wb_*.py dashboard.html ./
COPY --from=tls-bridge-artifact /wb-tls-bridge ./bin/wb-tls-bridge
COPY catalog/ ./catalog/
COPY licenses/ ./licenses/
COPY LICENSE NOTICE.md ./
COPY --from=tls-bridge-artifact /licenses/ ./licenses/
RUN chmod 0755 /app/bin/wb-tls-bridge &&     mkdir -p /app/accounts /app/usage /app/subbridge-data &&     python -m compileall -q /app
VOLUME ["/app/accounts", "/app/usage", "/app/subbridge-data"]
EXPOSE 7863
HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3     CMD python -c "import os,urllib.request; urllib.request.urlopen('http://127.0.0.1:%s/health' % os.environ.get('PORT','7863'), timeout=3)" || exit 1
CMD ["python", "-m", "wb_proxy", "--host", "0.0.0.0", "--lan"]
