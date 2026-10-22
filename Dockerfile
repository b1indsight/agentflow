# syntax=docker/dockerfile:1
ARG PYTHON_IMAGE=python:3.12-slim-bookworm@sha256:782412e85d0f0984994c290652577d4018aff08145c85b262bb63dc0c7522254
FROM node:24-bookworm-slim AS console-build
WORKDIR /opt/console
COPY app/controler/web/package*.json ./
RUN npm ci
COPY app/controler/web/ ./
RUN npm run build

FROM ${PYTHON_IMAGE} AS build
WORKDIR /opt/flowtool
RUN pip install --no-cache-dir uv==0.11.3
COPY pyproject.toml uv.lock README.md ./
COPY app ./app
COPY --from=console-build /opt/console/dist ./app/controler/console_static
RUN uv sync --locked --no-dev --no-editable

FROM ${PYTHON_IMAGE} AS runtime
RUN apt-get update && apt-get install -y --no-install-recommends ca-certificates tini \
    && rm -rf /var/lib/apt/lists/* \
    && useradd --uid 10001 --create-home --home-dir /home/flowtool flowtool \
    && mkdir -p /var/lib/flowtool /home/flowtool/.codex \
    && chown -R 10001:10001 /var/lib/flowtool /home/flowtool
COPY --from=build /opt/flowtool/.venv /opt/flowtool/.venv
ENV PATH="/opt/flowtool/.venv/bin:$PATH" HOME=/home/flowtool PYTHONUNBUFFERED=1
WORKDIR /opt/flowtool
ENTRYPOINT ["/usr/bin/tini", "--", "flowtool"]

FROM runtime AS serve
USER 10001:10001
EXPOSE 8000
CMD ["serve"]

FROM runtime AS worker
ARG CODEX_VERSION=0.153.4
ARG JJ_VERSION=0.45.1
ARG JJ_SHA256=f35438350b5d61963aac5dd74ede510b31d6b9690769d1a6268cf058cc825f72
ARG AMP_VERSION=0.0.1792555249-g702cd1
ARG AMP_SHA256=41a2e3680c63091d7c0293e2ad702af95772f9f35d977b74c7794fa40e45b5c9
RUN test "$(dpkg --print-architecture)" = amd64 \
    && apt-get update && apt-get install -y --no-install-recommends git gh curl gzip procps nodejs npm \
    && gh --version \
    && rm -rf /var/lib/apt/lists/* \
    && npm install --global --ignore-scripts --no-audit --no-fund "@openai/codex@${CODEX_VERSION}" \
    && npm cache clean --force \
    && curl -fsSL "https://github.com/jj-vcs/jj/releases/download/v${JJ_VERSION}/jj-v${JJ_VERSION}-x86_64-unknown-linux-musl.tar.gz" -o /tmp/jj.tar.gz \
    && echo "${JJ_SHA256}  /tmp/jj.tar.gz" | sha256sum -c - \
    && mkdir /tmp/jj && tar -xzf /tmp/jj.tar.gz -C /tmp/jj \
    && install -m 0755 /tmp/jj/jj /usr/local/bin/jj \
    && rm -rf /tmp/jj /tmp/jj.tar.gz \
    && curl -fsSL "https://static.ampcode.com/cli/${AMP_VERSION}/amp-linux-x64.gz" -o /tmp/amp.gz \
    && gzip -dc /tmp/amp.gz > /tmp/amp \
    && echo "${AMP_SHA256}  /tmp/amp" | sha256sum -c - \
    && install -m 0755 /tmp/amp /usr/local/bin/amp \
    && rm -f /tmp/amp /tmp/amp.gz \
    && mkdir -p /home/flowtool/.amp \
    && chown -R 10001:10001 /home/flowtool/.amp
USER 10001:10001
ENV FLOWTOOL_WORKER_ROOT=/var/lib/flowtool FLOWTOOL_EXECUTION_ISOLATION=container
CMD ["worker"]
