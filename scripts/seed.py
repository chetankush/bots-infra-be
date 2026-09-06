"""Create the database schema and one automotive tenant to talk to.

python scripts/seed.py
"""

from __future__ import annotations

import asyncio
import secrets

from sqlalchemy import select, text

from app.db.models import Base, Location, Tenant
from app.db.session import SessionLocal, engine
from app.logging import configure, get_logger

configure()
log = get_logger("seed")

TENANT_KEY = "northside-motors"


async def main() -> None:
    async with engine.begin() as conn:
        await conn.execute(text("CREATE EXTENSION IF NOT EXISTS vector"))
        await conn.run_sync(Base.metadata.create_all)
    print("schema ready")

    async with SessionLocal() as session:
        existing = (
            await session.execute(select(Tenant).where(Tenant.key == TENANT_KEY))
        ).scalar_one_or_none()

        if existing:
            tenant = existing
            print("tenant already exists")
        else:
            tenant = Tenant(
                key=TENANT_KEY,
                name="Northside Motors",
                public_key="pk_" + secrets.token_urlsafe(24),
                pack_key="automotive",
                allowed_origins=[],  # empty = allow all (dev only). Lock down before going live.
                region="us",
                monthly_token_budget=2_000_000,
                config_overrides={
                    "agent_name": "Riya",
                    "greeting": (
                        "Hi, I'm Riya from Northside Motors. I can book you a service "
                        "visit or a test drive - what do you need?"
                    ),
                },
            )
            session.add(tenant)
            await session.flush()
            session.add(
                Location(
                    tenant_id=tenant.id,
                    key="main",
                    name="Northside Motors - Main Branch",
                    timezone="Asia/Kolkata",
                )
            )
            await session.commit()

        print("\n" + "=" * 66)
        print(f"  tenant      {tenant.key}")
        print(f"  pack        {tenant.pack_key}")
        print(f"  public_key  {tenant.public_key}")
        print("=" * 66)
        print("\nNext:")
        print(f"  1. crawl a site:  python scripts/crawl.py {tenant.key} https://example.com")
        print(f"  2. chat:          python scripts/chat.py {tenant.public_key}")
        print("  3. widget:        put the public_key into widget/demo.html, open /demo\n")

    await engine.dispose()


if __name__ == "__main__":
    asyncio.run(main())
