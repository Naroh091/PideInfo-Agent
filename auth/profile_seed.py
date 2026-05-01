"""Per-portal Firefox profile seeding.

The agent maintains one persistent Firefox profile per portal so that browser
operations against different portals can run in parallel (Firefox locks the
profile dir to a single process). Each profile remembers — independently —
which client cert was picked for the Cl@ve origin.

To avoid prompting the user for the cert four times (once per portal), we
designate a **master** profile and copy it into each portal's profile the
first time that portal is used. Because all four auth flows redirect to the
same Cl@ve origin (`*.clave.gob.es`), the cert decision recorded in the
master's `cert9.db` is honoured by Firefox in any seeded copy.

Bootstrap:
- The first portal that authenticates trains its OWN profile directly. On
  successful auth, that profile is **promoted** to master.
- Subsequent portals seed from master before launching → silent auth.

Usage at the top of every auth flow:

    seed_from_master(profile_dir, master_dir)
    # ... launch_persistent_context(user_data_dir=profile_dir) ...
    # On successful auth:
    promote_to_master(profile_dir, master_dir)
"""

from __future__ import annotations

import logging
import shutil
from pathlib import Path

logger = logging.getLogger(__name__)


def _is_trained(profile: Path) -> bool:
    """A profile is "trained" once Firefox has written `cert9.db` (the cert
    decision DB) and `prefs.js` (the chosen `security.default_personal_cert`
    preference). Both are required for silent re-auth."""
    return (profile / "cert9.db").exists() and (profile / "prefs.js").exists()


def seed_from_master(profile_dir: Path, master_dir: Path | None) -> None:
    """If `profile_dir` doesn't exist yet AND `master_dir` is trained, copy
    the master into the new profile. No-op otherwise.

    Idempotent: a profile that already has prefs.js is left alone."""
    if profile_dir.exists() and (profile_dir / "prefs.js").exists():
        return
    if master_dir is None or not _is_trained(master_dir):
        # Caller will train this profile from scratch (headed first run).
        profile_dir.mkdir(parents=True, exist_ok=True)
        return
    # Copy the master. dirs_exist_ok handles the case where profile_dir was
    # mkdir'd already (e.g. by an earlier failed seeding attempt).
    profile_dir.mkdir(parents=True, exist_ok=True)
    for child in master_dir.iterdir():
        dest = profile_dir / child.name
        if child.is_dir():
            shutil.copytree(child, dest, dirs_exist_ok=True)
        else:
            shutil.copy2(child, dest)
    logger.info("Seeded Firefox profile %s from master %s", profile_dir, master_dir)


def promote_to_master(profile_dir: Path, master_dir: Path | None) -> None:
    """If `master_dir` is empty/un-trained but `profile_dir` is trained,
    copy `profile_dir` → `master_dir`. Bootstraps the master from whichever
    portal happens to authenticate first."""
    if master_dir is None:
        return
    if _is_trained(master_dir):
        return
    if not _is_trained(profile_dir):
        return
    master_dir.mkdir(parents=True, exist_ok=True)
    for child in profile_dir.iterdir():
        dest = master_dir / child.name
        if child.is_dir():
            shutil.copytree(child, dest, dirs_exist_ok=True)
        else:
            shutil.copy2(child, dest)
    logger.info("Promoted %s to master profile at %s", profile_dir, master_dir)


def migrate_legacy_profile(legacy: Path, master: Path) -> None:
    """One-time migration: if a pre-split single profile exists at `legacy`
    and the new master doesn't, move it to master so users don't have to
    re-pick the cert.

    Safe to call repeatedly. Does nothing if either: master is already
    trained, or legacy doesn't exist / isn't trained."""
    if not _is_trained(legacy):
        return
    if _is_trained(master):
        return
    master.mkdir(parents=True, exist_ok=True)
    for child in legacy.iterdir():
        dest = master / child.name
        if child.is_dir():
            shutil.copytree(child, dest, dirs_exist_ok=True)
        else:
            shutil.copy2(child, dest)
    logger.info("Migrated legacy profile %s → master %s", legacy, master)
