FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    FASTEMBED_CACHE_PATH=/srv/.fastembed_cache

WORKDIR /srv

# postgresql-client provides pg_dump, which scripts/backup.py shells out to
RUN apt-get update && apt-get install -y --no-install-recommends \
    build-essential curl postgresql-client && rm -rf /var/lib/apt/lists/*

COPY pyproject.toml README.md ./
COPY app ./app
RUN pip install --upgrade pip && pip install -e .

EXPOSE 8000
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
