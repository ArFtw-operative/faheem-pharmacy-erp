"""A slow handler (PDF import, export, big save) must never freeze the other screens."""
from __future__ import annotations

import asyncio
import time

import httpx
from fastapi import FastAPI, Request
from fastapi.routing import APIRouter

from app.routing import OffloadRoute


def _app() -> FastAPI:
    router = APIRouter(route_class=OffloadRoute)

    @router.post("/slow")
    async def slow(request: Request):
        form = await request.form()
        time.sleep(0.6)  # blocking work, like parsing a PDF
        return {"got": form.get("x")}

    @router.post("/json")
    async def echo(request: Request):
        return await request.json()

    @router.get("/fast")
    async def fast():
        return {"ok": True}

    app = FastAPI()
    app.include_router(router)
    return app


def test_slow_handler_does_not_block_others():
    async def scenario():
        transport = httpx.ASGITransport(app=_app())
        async with httpx.AsyncClient(transport=transport, base_url="http://t") as c:
            t = time.perf_counter()
            slow = asyncio.create_task(c.post("/slow", data={"x": "1"}))
            await asyncio.sleep(0)
            r = await c.get("/fast")
            fast_ms = (time.perf_counter() - t) * 1000
            assert r.json() == {"ok": True}
            assert (await slow).json() == {"got": "1"}
            assert (await c.post("/json", json={"a": 2})).json() == {"a": 2}
            return fast_ms

    assert asyncio.run(scenario()) < 300


def test_all_app_routes_are_offloaded():
    from app.main import app

    import inspect

    import app.routers as pkg

    routers = [getattr(__import__(f"app.routers.{m}", fromlist=["router"]), "router", None) for m in (
        "sales", "inventory", "purchases", "stock_history", "sales_history", "customers", "erp", "reports", "adjustments")]
    async_routes = [r for rt in routers for r in rt.routes if inspect.iscoroutinefunction(getattr(r, "endpoint", None))]
    assert len(async_routes) > 25
    assert all(isinstance(r, OffloadRoute) and hasattr(r.endpoint, "__wrapped__") for r in async_routes)
