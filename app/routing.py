"""Keep the server responsive: async route handlers run their work in worker threads.

Most handlers are ``async def`` only so they can ``await request.form()`` /
``request.json()``; the rest of their body is ordinary blocking work (SQLite,
PDF/Excel parsing, zipping). On the single event loop that work froze every
other screen until it finished. ``OffloadRoute`` reads the request body on the
event loop (non-blocking), then runs the handler in a worker thread where those
awaits return the already-parsed body instantly.
"""
from __future__ import annotations

import asyncio
import functools
import inspect
import os
import threading
from typing import Any, Callable

import anyio.to_thread
from fastapi.routing import APIRoute
from starlette.requests import Request

_local = threading.local()


async def _preload(request: Request) -> None:
    ctype = request.headers.get("content-type", "")
    try:
        if "application/json" in ctype:
            await request.json()
        elif "multipart/form-data" in ctype or "application/x-www-form-urlencoded" in ctype:
            await request.form()
        else:
            await request.body()
    except Exception:
        # malformed bodies raise the same error again inside the handler, where it is handled
        pass


def _run(fn: Callable, values: dict) -> Any:
    loop = getattr(_local, "loop", None)
    if loop is None or loop.is_closed():
        loop = _local.loop = asyncio.new_event_loop()
    return loop.run_until_complete(fn(**values))


def offload(fn: Callable) -> Callable:
    @functools.wraps(fn)
    async def runner(**values: Any) -> Any:
        for value in values.values():
            if isinstance(value, Request):
                await _preload(value)
        return await anyio.to_thread.run_sync(_run, fn, values)

    return runner


class OffloadRoute(APIRoute):
    """APIRoute whose async endpoint runs in a worker thread (see module docstring)."""

    def __init__(self, path: str, endpoint: Callable[..., Any], **kwargs: Any) -> None:
        if inspect.iscoroutinefunction(endpoint) and not os.environ.get("PHARMACY_NO_OFFLOAD"):
            endpoint = offload(endpoint)
        super().__init__(path, endpoint, **kwargs)
