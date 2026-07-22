ARG PYTHON_IMAGE=python:3.13-slim
FROM ${PYTHON_IMAGE} AS base

ARG APT_DEBIAN_MIRROR=http://deb.debian.org/debian
ARG APT_SECURITY_MIRROR=http://deb.debian.org/debian-security
ARG PIP_INDEX_URL=https://pypi.org/simple
ARG UV_DEFAULT_INDEX=https://pypi.org/simple

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    UV_LINK_MODE=copy \
    UV_DEFAULT_INDEX=${UV_DEFAULT_INDEX} \
    PIP_INDEX_URL=${PIP_INDEX_URL} \
    HOME=/home/app

RUN sed -i \
        -e "s|http://deb.debian.org/debian-security|${APT_SECURITY_MIRROR}|g" \
        -e "s|http://deb.debian.org/debian|${APT_DEBIAN_MIRROR}|g" \
        /etc/apt/sources.list.d/debian.sources \
    && useradd --create-home --uid 10001 app \
    && apt-get update \
    && apt-get install --no-install-recommends -y openssh-client curl \
    && rm -rf /var/lib/apt/lists/* \
    && pip install --no-cache-dir "uv>=0.6.0"

WORKDIR /app
COPY pyproject.toml uv.lock README.md ./
RUN uv venv .venv \
    && uv export \
        --frozen \
        --no-dev \
        --no-emit-project \
        --format requirements.txt \
        --output-file /tmp/requirements.txt >/dev/null \
    && uv pip sync \
        --python .venv/bin/python \
        --require-hashes \
        /tmp/requirements.txt \
    && rm -f /tmp/requirements.txt

FROM base AS development
ARG APP_VERSION=local
ENV APP_VERSION=${APP_VERSION}
LABEL org.opencontainers.image.revision="${APP_VERSION}"
COPY . .
RUN uv sync --frozen --all-groups
RUN chown -R app:app /app /home/app

USER app
EXPOSE 8000
CMD ["uv", "run", "uvicorn", "iot_ops_agent.web.api:app", "--host", "0.0.0.0", "--port", "8000"]

FROM base AS runtime
ARG APP_VERSION=local
ENV APP_VERSION=${APP_VERSION} \
    UV_NO_DEV=1 \
    UV_NO_SYNC=1
LABEL org.opencontainers.image.revision="${APP_VERSION}"
COPY alembic.ini ./
COPY configs ./configs
COPY evals ./evals
COPY migrations ./migrations
COPY samples ./samples
COPY src ./src
RUN uv sync --frozen --no-dev
RUN chown -R app:app /app /home/app

USER app
EXPOSE 8000
CMD ["uv", "run", "uvicorn", "iot_ops_agent.web.api:app", "--host", "0.0.0.0", "--port", "8000", "--proxy-headers", "--forwarded-allow-ips=*"]
