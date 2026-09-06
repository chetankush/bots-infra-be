"""Back the database up to object storage, then prune old copies.

    python scripts/backup.py            # dump + upload + prune
    python scripts/backup.py --prune    # prune only

Run it from cron or a systemd timer on the Oracle box.
"""

from __future__ import annotations

import asyncio
import sys

from app.logging import configure
from app.services.backup import prune, run_backup

configure()


async def main() -> None:
    if "--prune" not in sys.argv:
        result = await run_backup()
        print(f"  uploaded  {result['key']}  ({result['bytes'] / 1024:.1f} KB)")
    removed = await prune()
    print(f"  pruned    {removed} old backup(s)")


if __name__ == "__main__":
    asyncio.run(main())
