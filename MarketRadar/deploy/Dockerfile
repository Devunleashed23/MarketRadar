# Build from the repository root. Linux image execution requires the CI smoke gate.
FROM node:22-bookworm-slim AS node
FROM python:3.12-slim-bookworm

ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1 \
    PLAYWRIGHT_BROWSERS_PATH=/ms-playwright \
    MARKETRADAR_DEPLOYMENT=cloud MARKETRADAR_PROVIDER=alternate_clouds \
    MARKETRADAR_BROWSER_MODE=headed MARKETRADAR_BROWSER_WORK_DIR=/data/browser \
    MARKETRADAR_BROWSER_PROFILE=/data/profile MARKETRADAR_DATA_DIR=/data/runs \
    MARKETRADAR_HOST=127.0.0.1 DISPLAY=:99 HOME=/home/marketradar

COPY --from=node /usr/local/bin/node /usr/local/bin/node
COPY --from=node /usr/local/lib/node_modules/npm /usr/local/lib/node_modules/npm
RUN ln -s /usr/local/lib/node_modules/npm/bin/npm-cli.js /usr/local/bin/npm \
    && ln -s /usr/local/lib/node_modules/npm/bin/npx-cli.js /usr/local/bin/npx \
    && apt-get update \
    && apt-get install -y --no-install-recommends ca-certificates libstdc++6 \
       xvfb openbox x11vnc websockify novnc nginx tini fonts-liberation \
    && nginx -V 2>&1 | grep -q -- --with-http_auth_request_module \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY MarketRadar/deploy/package.json MarketRadar/deploy/package-lock.json ./
RUN npm ci --omit=optional --ignore-scripts --no-audit --no-fund \
    && node node_modules/playwright/cli.js install --with-deps chromium \
    && chmod -R a+rX /ms-playwright \
    && rm -rf /var/lib/apt/lists/* /root/.npm

COPY MarketRadar/requirements.txt /app/MarketRadar/requirements.txt
RUN pip install --no-cache-dir -r /app/MarketRadar/requirements.txt \
    && groupadd --gid 10001 marketradar \
    && useradd --uid 10001 --gid 10001 --create-home --shell /usr/sbin/nologin marketradar \
    && mkdir -p /data/browser /data/profile /data/runs /data/runtime \
    && chown -R 10001:10001 /data /home/marketradar

# Explicit inputs: never COPY the repository, .env, browser profile, or .data.
COPY MarketRadar/*.py /app/MarketRadar/
COPY MarketRadar/amazon_research.js /app/MarketRadar/
COPY MarketRadar/static/ /app/MarketRadar/static/
COPY MarketRadar/tests/ /app/MarketRadar/tests/
COPY MarketRadar/deploy/ /app/MarketRadar/deploy/
COPY subagents/__init__.py subagents/amazon_market_agent_v2.py subagents/listing_agent_v2.py /app/subagents/
COPY models.py /app/models.py

USER 10001:10001
VOLUME ["/data"]
EXPOSE 8080
HEALTHCHECK --interval=20s --timeout=3s --start-period=100s --retries=3 \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8080/healthz', timeout=2).read()"
ENTRYPOINT ["/usr/bin/tini", "--", "python", "-m", "MarketRadar.deploy.bootstrap"]
