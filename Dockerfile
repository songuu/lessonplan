FROM node:22-bookworm-slim AS frontend
WORKDIR /build
COPY frontend/package*.json ./
RUN npm ci
COPY frontend/ ./
RUN npm run build

FROM python:3.13-slim-bookworm
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
WORKDIR /app
RUN apt-get update && apt-get install -y --no-install-recommends fonts-noto-cjk fonts-wqy-microhei fonts-dejavu-core && rm -rf /var/lib/apt/lists/*
RUN pip install --no-cache-dir uv==0.9.21
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev
COPY app/ app/
COPY main.py alembic.ini ./
COPY migrations/ migrations/
COPY config/ config/
COPY schemas/ schemas/
COPY policies/ policies/
COPY prompts/ prompts/
COPY scripts/ scripts/
COPY --from=frontend /build/dist/ frontend/dist/
RUN useradd --uid 10001 --create-home app && mkdir -p /app/var/storage && chown -R app:app /app/var
USER 10001
EXPOSE 8000
HEALTHCHECK --interval=20s --timeout=5s CMD ["/app/.venv/bin/python", "-c", "import urllib.request;urllib.request.urlopen('http://127.0.0.1:8000/live',timeout=3)"]
CMD ["/app/.venv/bin/uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8000"]
