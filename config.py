from pathlib import Path

from pydantic import field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
    )

    @field_validator("data_dir", mode="after")
    @classmethod
    def _expand_data_dir(cls, v: Path) -> Path:
        return v.expanduser()

    # Portal
    portal_url: str = "https://transparencia.sede.gob.es"
    portal_ctbg: str = "https://sede.consejodetransparencia.gob.es/info.0"
    portal_dehu: str = "https://dehu.redsara.es"
    portal_redsara: str = "https://reg.redsara.es"

    # PideInfo. Default es producción: los binarios publicados no traen .env,
    # por lo que el valor en código es el que ven los usuarios finales. Para
    # desarrollo local, exportar PIDEINFO_BASE_URL=http://localhost:8000 o
    # ponerlo en el .env (ver .env.example).
    pideinfo_base_url: str = "https://pideinfo.es"

    # Agent
    auth_timeout_seconds: int = 120
    sync_interval_minutes: int = 30
    data_dir: Path = Path.home() / ".pideinfo-agent"
    debug: bool = False
    headless_disabled: bool = False

    # Sentry — DSN is baked into the build at release time. Empty disables.
    # Named *_agent so it doesn't collide with the PHP web app's SENTRY_DSN.
    sentry_dsn_agent: str = ""
    sentry_environment: str = "production"
    # When True, the CTBG expediente crawl walks every expediente in the list
    # (including long-closed ones); otherwise it stops at the first all-closed
    # batch. The list is sorted with most-recent activity first.
    ctbg_full_crawl: bool = False

    @property
    def portal_ctbg_base(self) -> str:
        """Base URL for CTBG sede (strip page path like /info.0)."""
        from urllib.parse import urlparse
        parsed = urlparse(self.portal_ctbg)
        return f"{parsed.scheme}://{parsed.netloc}"

    @property
    def cookies_file(self) -> Path:
        return self.data_dir / "cookies.json"

    @property
    def preferences_file(self) -> Path:
        return self.data_dir / "preferences.json"

    @property
    def cookies_ctbg_file(self) -> Path:
        return self.data_dir / "cookies_ctbg.json"

    @property
    def cookies_dehu_file(self) -> Path:
        return self.data_dir / "cookies_dehu.json"

    @property
    def cookies_redsara_file(self) -> Path:
        return self.data_dir / "cookies_redsara.json"

    @property
    def state_file(self) -> Path:
        return self.data_dir / "sync_state.json"

    @property
    def downloads_dir(self) -> Path:
        return self.data_dir / "downloads"

    @property
    def playwright_browsers_dir(self) -> Path:
        """Platform-appropriate directory for Playwright browser binaries."""
        from runtime import get_playwright_browsers_dir
        return get_playwright_browsers_dir()

    @property
    def firefox_profile_master(self) -> Path:
        """Master persistent Firefox profile.

        The first portal to authenticate trains this profile (headed window,
        user picks the cert from Firefox's native picker). Per-portal
        profiles are seeded from here so the user only picks the cert once.
        """
        return self.data_dir / "firefox-profile-master"

    @property
    def firefox_profile_legacy(self) -> Path:
        """Pre-split single profile, kept for one-time migration."""
        return self.data_dir / "firefox-profile"

    def firefox_profile_for(self, portal_id: str) -> Path:
        """Per-portal Firefox profile dir.

        Each portal gets its own profile so Playwright/Firefox can run
        against multiple portals in parallel without fighting over the
        single-instance profile lock.
        """
        return self.data_dir / f"firefox-profile-{portal_id}"

    @property
    def firefox_profile_dir(self) -> Path:
        """Backward-compatible alias for the master profile.

        Deprecated: callers should pick a portal-specific profile via
        `firefox_profile_for(portal_id)`.
        """
        return self.firefox_profile_master
