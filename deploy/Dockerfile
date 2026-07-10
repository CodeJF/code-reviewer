FROM python:3.13-slim AS runtime

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    UV_LINK_MODE=copy \
    HOME=/home/app

RUN useradd --create-home --uid 10001 app \
    && apt-get update \
    && apt-get install --no-install-recommends -y openssh-client curl \
    && rm -rf /var/lib/apt/lists/* \
    && pip install --no-cache-dir "uv>=0.6.0"

WORKDIR /app
COPY pyproject.toml uv.lock README.md ./
RUN uv sync --frozen --no-dev --no-install-project
COPY . .
RUN uv sync --frozen --no-dev

USER app
EXPOSE 8000
CMD ["uv", "run", "uvicorn", "team_app.api:app", "--host", "0.0.0.0", "--port", "8000", "--proxy-headers"]
