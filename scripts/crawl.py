"""Crawl a site into a tenant's knowledge base.

python scripts/crawl.py <tenant_key> <url> [max_pages]
"""

from __future__ import annotations

import asyncio
import sys

from sqlalchemy import select

from app.db.models import Tenant
from app.db.session import SessionLocal, engine
from app.logging import configure
from app.rag.ingest import ingest_site

configure()


async def main() -> None:
    if len(sys.argv) < 3:
        print(__doc__)
        raise SystemExit(1)

    tenant_key, url = sys.argv[1], sys.argv[2]
    max_pages = int(sys.argv[3]) if len(sys.argv) > 3 else 25

    async with SessionLocal() as session:
        tenant = (
            await session.execute(select(Tenant).where(Tenant.key == tenant_key))
        ).scalar_one_or_none()
        if not tenant:
            print(f"no tenant '{tenant_key}' - run scripts/seed.py first")
            raise SystemExit(1)

        print(f"crawling {url} for {tenant.name} (max {max_pages} pages)...")
        result = await ingest_site(session, tenant_id=tenant.id, start_url=url, max_pages=max_pages)
        for key, value in result.items():
            print(f"  {key:20} {value}")

    await engine.dispose()


if __name__ == "__main__":
    asyncio.run(main())
