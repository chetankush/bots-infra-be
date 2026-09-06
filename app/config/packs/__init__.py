from app.config.packs.automotive import PACK as AUTOMOTIVE
from app.config.packs.generic import PACK as GENERIC

PACKS: dict[str, dict] = {
    "automotive": AUTOMOTIVE,
    "generic": GENERIC,
}

__all__ = ["PACKS", "AUTOMOTIVE", "GENERIC"]
