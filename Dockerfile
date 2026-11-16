FROM python:3.11-alpine@sha256:7fbad8e4652eae0186429a6f26cf5b23e27e8c6fd7ac4d0cfd7a6988cdeea1e1
LABEL org.opencontainers.image.title="wb2hub"       org.opencontainers.image.source="https://github.com/brucelau1987cn/wb2hub"
ENV PYTHONUNBUFFERED=1 HOST=0.0.0.0 PORT=7863 TZ=Asia/Shanghai     WB_UPDATE_REPO=brucelau1987cn/wb2hub
WORKDIR /app
RUN apk add --no-cache tzdata ca-certificates
COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt
COPY wb_*.py dashboard.html ./
COPY bin/ ./bin/
COPY catalog/ ./catalog/
COPY licenses/ ./licenses/
RUN chmod 0755 /app/bin/wb-tls-bridge &&     mkdir -p /app/accounts /app/usage /app/subbridge-data &&     python -m compileall -q /app
VOLUME ["/app/accounts", "/app/usage", "/app/subbridge-data"]
EXPOSE 7863
HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3     CMD python -c "import os,urllib.request; urllib.request.urlopen('http://127.0.0.1:%s/health' % os.environ.get('PORT','7863'), timeout=3)" || exit 1
CMD ["python", "-m", "wb_proxy", "--host", "0.0.0.0", "--lan"]
