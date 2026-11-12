"""Import seeds/translations_seed.json (new keys only, never overwrites)."""
import asyncio
import json
from pathlib import Path

from app.database import SessionLocal
from app.routers.v14 import ImportIn, import_translations


async def main() -> None:
    items = json.loads((Path(__file__).resolve().parent.parent / "seeds" / "translations_seed.json").read_text("utf-8"))
    async with SessionLocal() as db:
        print(await import_translations(ImportIn(items=items, overwrite=False), None, db))


asyncio.run(main())
