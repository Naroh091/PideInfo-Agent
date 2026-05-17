"""Memoised wrapper around `keyring` to minimise macOS Keychain prompts.

macOS prompts on every Keychain access from a binary whose code signature
doesn't match the ACL recorded on "Always Allow". Even with a stable signed
build the prompts are merely *infrequent*, not absent — and we hit the
Keychain at least 6 times per sync cycle (JWT + 4 cookie blobs + DEHú JWT).

This wrapper keeps successful reads in process memory for the lifetime of
the agent. Writes go through to the real keyring AND update the cache so
subsequent reads stay consistent. Single-instance enforcement
(`protocol/single_instance`) guarantees no other agent process is racing
us on the same Keychain items, so the cache cannot diverge from reality.

Drop-in replacement: `from storage import keyring_cache as keyring` and
existing call sites work unchanged.
"""

from __future__ import annotations

import keyring as _real_keyring
import logging
import threading
from typing import Any

logger = logging.getLogger(__name__)

# Re-export the errors submodule so callers that do
# `keyring.errors.PasswordDeleteError` keep working.
errors = _real_keyring.errors

_cache: dict[tuple[str, str], str | None] = {}
_lock = threading.Lock()


def get_password(service: str, username: str) -> str | None:
    key = (service, username)
    with _lock:
        if key in _cache:
            return _cache[key]
    # Release the lock during the (potentially blocking) Keychain call so
    # callers on other threads aren't serialised behind the prompt.
    value = _real_keyring.get_password(service, username)
    with _lock:
        _cache[key] = value
    return value


def set_password(service: str, username: str, password: str) -> None:
    # macOS error -25244 (errSecMissingEntitlement) — when a stale Keychain
    # item carries an ACL bound to a previous Python binary signature, the
    # current binary can read it (with prompts) but can't overwrite it. We
    # don't want that to crash the pipeline: keep the value in the process
    # cache so the rest of this run keeps working, and surface a warning so
    # the user can clear the stale item with `security delete-generic-password`.
    try:
        _real_keyring.set_password(service, username, password)
    except _real_keyring.errors.KeyringError as e:
        logger.warning(
            "Keychain write failed for %s/%s (%s) — using in-memory cache for this run. "
            "Run: security delete-generic-password -s %s -a %s",
            service, username, e, service, username,
        )
    with _lock:
        _cache[(service, username)] = password


def delete_password(service: str, username: str) -> None:
    try:
        _real_keyring.delete_password(service, username)
    finally:
        with _lock:
            _cache.pop((service, username), None)


def clear_cache() -> None:
    """Drop the in-memory cache. Intended for tests."""
    with _lock:
        _cache.clear()


# Forward any attribute access we don't override (e.g. backends, util) to
# the real keyring module so callers using less-common APIs still work.
def __getattr__(name: str) -> Any:
    return getattr(_real_keyring, name)
