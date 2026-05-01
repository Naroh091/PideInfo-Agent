"""Per-portal mutexes for browser-using work.

Each portal has its own Firefox profile directory; Firefox locks the profile
to a single process. To allow sync, drain, and incoming `pideinfo://` tasks
to run in parallel across DIFFERENT portals while still serialising
operations on the SAME portal, every code path that drives Playwright wraps
its work in `async with portal_locks.lock_for(portal_id):`.

Backed by a `threading.Lock` rather than `asyncio.Lock` because callers
live in **multiple asyncio loops** — the tray's daemon-thread loop AND the
short-lived `asyncio.run(...)` loops that `tasks.present_complaint.handle`
spins up on the IPC thread. An asyncio.Lock is bound to the loop that
created it; a threading.Lock isn't.

`lock_for` is an async context manager: it acquires the underlying
threading.Lock via `asyncio.to_thread` so awaiting it yields control to
other tasks on the current loop while we wait.
"""

from __future__ import annotations

import asyncio
import threading
from contextlib import asynccontextmanager

_LOCKS: dict[str, threading.Lock] = {}
_REGISTRY_LOCK = threading.Lock()


def _get(portal_id: str) -> threading.Lock:
    with _REGISTRY_LOCK:
        lock = _LOCKS.get(portal_id)
        if lock is None:
            lock = threading.Lock()
            _LOCKS[portal_id] = lock
        return lock


@asynccontextmanager
async def lock_for(portal_id: str):
    """Acquire the lock for `portal_id`. Releases automatically on exit.

    Awaiting the acquire yields the current asyncio loop, so other tasks
    keep running while we wait for the portal to free up.
    """
    lock = _get(portal_id)
    await asyncio.to_thread(lock.acquire)
    try:
        yield
    finally:
        lock.release()


def known_portals() -> tuple[str, ...]:
    return ("transparencia", "ctbg", "dehu", "redsara")
