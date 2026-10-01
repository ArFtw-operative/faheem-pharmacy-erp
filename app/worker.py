"""Single durable WhatsApp queue consumer, with a health heartbeat."""
from __future__ import annotations

import asyncio
import os
import time
from pathlib import Path


async def run() -> None:
    from app.main import _whatsapp_worker
    from app.production import schema_state
    if schema_state()["state"] != "current":
        raise RuntimeError("Worker schema is not current")
    worker = asyncio.create_task(_whatsapp_worker())
    heartbeat = Path(os.environ.get("PHARMACY_DATA_DIR", "/var/lib/faheem-erp/application-data")) / "worker-heartbeat"
    while True:
        if worker.done():
            await worker
            raise RuntimeError("Worker stopped")
        from app.database import engine
        from sqlalchemy import text
        with engine.connect() as con:
            con.execute(text("SELECT 1"))
        heartbeat.write_text(str(time.time()))
        await asyncio.sleep(10)


if __name__ == "__main__":
    asyncio.run(run())
