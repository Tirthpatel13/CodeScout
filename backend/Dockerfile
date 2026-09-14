FROM python:3.14-slim AS base

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1

# git is a runtime dependency: the ingestion pipeline shells out to it to clone.
RUN apt-get update \
    && apt-get install -y --no-install-recommends git ca-certificates \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /srv

COPY pyproject.toml ./
RUN pip install --upgrade pip && pip install -e ".[dev,otel]"

COPY . .

# Never run as root; a cloned repository is untrusted input.
RUN useradd --create-home --uid 10001 scout && chown -R scout:scout /srv
USER scout

EXPOSE 8000
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
