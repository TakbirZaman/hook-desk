FROM python:3.12-slim
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
WORKDIR /app
COPY --from=ghcr.io/astral-sh/uv:latest /uv /bin/uv
COPY pyproject.toml uv.lock* README.md ./
COPY src ./src
RUN uv sync --no-dev
RUN useradd -m app && mkdir -p /data && chown -R app /app /data
USER app
ENV HOOKDESK_DB_PATH=/data/hookdesk.db
VOLUME /data
CMD ["uv", "run", "uvicorn", "hookdesk.main:create_app", "--factory", "--host", "0.0.0.0", "--port", "8000"]
