"""Encrypted database backups to S3-compatible object storage.

boto3 is the AWS SDK, but the S3 API is a de-facto standard - the same code runs
against AWS S3, Oracle Object Storage, Cloudflare R2, Backblaze B2, Supabase Storage
and MinIO. Only BACKUP_ENDPOINT_URL changes. That means the free option today
(Oracle Always Free, or MinIO locally) and real S3 later cost the same to support.

Supabase's free tier makes no backup guarantees worth relying on, so this is owned
here rather than assumed.
"""

from __future__ import annotations

import asyncio
import gzip
import os
import subprocess
from datetime import UTC, datetime, timedelta
from urllib.parse import urlparse

from app.logging import get_logger
from app.settings import get_settings

log = get_logger("backup")


def _client():
    import boto3
    from botocore.config import Config

    s = get_settings()
    if not (s.backup_access_key and s.backup_secret_key):
        raise RuntimeError("BACKUP_ACCESS_KEY / BACKUP_SECRET_KEY are not set")
    return boto3.client(
        "s3",
        endpoint_url=s.backup_endpoint_url or None,  # None = real AWS S3
        aws_access_key_id=s.backup_access_key,
        aws_secret_access_key=s.backup_secret_key,
        region_name=s.backup_region,
        config=Config(signature_version="s3v4", retries={"max_attempts": 3}),
    )


def _dump_url() -> str:
    """pg_dump speaks libpq, not SQLAlchemy's asyncpg dialect."""
    return get_settings().database_url.replace("postgresql+asyncpg://", "postgresql://")


def _dump_sync() -> bytes:
    import shutil

    if not shutil.which("pg_dump"):
        raise RuntimeError(
            "pg_dump not found. It ships with postgresql-client:\n"
            "  macOS:  brew install libpq && brew link --force libpq\n"
            "  debian: apt-get install postgresql-client\n"
            "The app image already includes it, so this only affects host runs."
        )

    parsed = urlparse(_dump_url())
    env = {**os.environ}
    if parsed.password:
        env["PGPASSWORD"] = parsed.password

    result = subprocess.run(
        [
            "pg_dump",
            "--no-owner",
            "--no-privileges",
            "-h",
            parsed.hostname or "localhost",
            "-p",
            str(parsed.port or 5432),
            "-U",
            parsed.username or "postgres",
            "-d",
            (parsed.path or "/postgres").lstrip("/"),
        ],
        capture_output=True,
        env=env,
        timeout=600,
    )
    if result.returncode != 0:
        raise RuntimeError(f"pg_dump failed: {result.stderr.decode()[:400]}")
    return gzip.compress(result.stdout, compresslevel=6)


async def run_backup() -> dict:
    settings = get_settings()
    blob = await asyncio.to_thread(_dump_sync)
    stamp = datetime.now(UTC).strftime("%Y-%m-%dT%H-%M-%SZ")
    key = f"backups/engine-{stamp}.sql.gz"

    def _upload():
        client = _client()
        client.put_object(
            Bucket=settings.backup_bucket,
            Key=key,
            Body=blob,
            ContentType="application/gzip",
            # Server-side encryption at rest. Supported by S3 and most compatibles;
            # harmless where it is not honoured.
            ServerSideEncryption="AES256",
        )

    try:
        await asyncio.to_thread(_upload)
    except Exception as exc:
        if "ServerSideEncryption" not in str(exc):
            raise

        def _upload_plain():
            _client().put_object(
                Bucket=settings.backup_bucket,
                Key=key,
                Body=blob,
                ContentType="application/gzip",
            )

        log.warning("backup_sse_unsupported", note="endpoint rejected AES256; uploaded without")
        await asyncio.to_thread(_upload_plain)

    result = {"key": key, "bytes": len(blob)}
    log.info("backup_uploaded", **result)
    return result


async def prune(retain_days: int | None = None) -> int:
    """Drop backups past the retention window."""
    settings = get_settings()
    retain = retain_days or settings.backup_retain_days
    cutoff = datetime.now(UTC) - timedelta(days=retain)

    def _prune() -> int:
        client = _client()
        paginator = client.get_paginator("list_objects_v2")
        removed = 0
        for page in paginator.paginate(Bucket=settings.backup_bucket, Prefix="backups/"):
            stale = [
                {"Key": obj["Key"]}
                for obj in page.get("Contents", [])
                if obj["LastModified"] < cutoff
            ]
            for i in range(0, len(stale), 1000):
                client.delete_objects(
                    Bucket=settings.backup_bucket, Delete={"Objects": stale[i : i + 1000]}
                )
            removed += len(stale)
        return removed

    removed = await asyncio.to_thread(_prune)
    log.info("backup_pruned", removed=removed, retain_days=retain)
    return removed
