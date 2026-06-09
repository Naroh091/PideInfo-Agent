"""
CTBG full-expediente scraper (the reclamación file behind /expediente.7).

This is independent of ``ConsejoScraper`` (which only scrapes the notification
inbox via httpx). The expediente detail page is a Wicket SPA — phase content
is rendered server-side after a click that mutates session state, so we drive
it with a persistent Playwright context.
"""
from __future__ import annotations

import hashlib
import re
from pathlib import Path
from typing import Optional
from urllib.parse import urljoin

from bs4 import BeautifulSoup
from playwright.async_api import (
    BrowserContext,
    Page,
    Playwright,
    TimeoutError as PlaywrightTimeoutError,
    async_playwright,
)
from rich.console import Console

# Government CAs are in the OS trust store but not in certifi.
import truststore
truststore.inject_into_ssl()

from auth.session_manager import SessionExpiredError
from models.consejo_expediente import DocumentoCTBGExpediente, ExpedienteCTBG
from runtime import FIREFOX_LAUNCH_ARGS

console = Console()

# Tolerant to markup like "<span>CSV</span>: XXX" (space before the colon).
_CSV_RE = re.compile(r"CSV\s*:\s*([A-Z0-9]+)")


class ConsejoExpedienteScraper:
    """Crawl /expedientes.6 + /expediente.7 with Playwright.

    Use as an async context manager:

        async with ConsejoExpedienteScraper(portal_url, profile_dir) as sc:
            for exp in await sc.list_expedientes():
                docs = await sc.iter_documents(exp)
                for doc in docs:
                    await sc.download(doc, dest)
    """

    def __init__(
        self,
        portal_url: str,
        firefox_profile_dir: Path,
        force_headed: bool = False,
        firefox_profile_master: "Path | None" = None,
    ):
        self.portal_url = portal_url.rstrip("/")
        self.firefox_profile_dir = firefox_profile_dir
        self.firefox_profile_master = firefox_profile_master
        self.force_headed = force_headed
        self._pw: Optional[Playwright] = None
        self._context: Optional[BrowserContext] = None
        self._page: Optional[Page] = None

    async def __aenter__(self) -> "ConsejoExpedienteScraper":
        from auth.profile_seed import seed_from_master
        from portal_locks import lock_for
        # Hold the CTBG portal lock for the lifetime of this scraper. Released
        # in __aexit__ via the AsyncExitStack.
        from contextlib import AsyncExitStack
        self._stack = AsyncExitStack()
        await self._stack.__aenter__()
        await self._stack.enter_async_context(lock_for("ctbg"))
        seed_from_master(self.firefox_profile_dir, self.firefox_profile_master)
        self._pw = await async_playwright().start()
        profile_ready = (self.firefox_profile_dir / "prefs.js").exists()
        cert_ready = (self.firefox_profile_dir / ".pideinfo-cert-ready").exists()
        headless = profile_ready and not self.force_headed

        firefox_user_prefs = {
            "security.osclientcerts.autoload": True,
            "browser.sessionstore.resume_from_crash": False,
            "browser.startup.page": 0,
        }
        if headless or cert_ready:
            firefox_user_prefs["security.default_personal_cert"] = "Select Automatically"
        self._context = await self._pw.firefox.launch_persistent_context(
            user_data_dir=str(self.firefox_profile_dir),
            headless=headless,
            args=FIREFOX_LAUNCH_ARGS,
            firefox_user_prefs=firefox_user_prefs,
            locale="es-ES",
            ignore_https_errors=True,
        )
        self._page = (
            self._context.pages[0]
            if self._context.pages
            else await self._context.new_page()
        )
        return self

    async def __aexit__(self, exc_type, exc, tb) -> None:
        try:
            try:
                if self._context is not None:
                    await self._context.close()
            finally:
                if self._pw is not None:
                    await self._pw.stop()
        finally:
            # Release the CTBG portal lock acquired in __aenter__.
            if hasattr(self, "_stack") and self._stack is not None:
                await self._stack.__aexit__(exc_type, exc, tb)

    # ------------------------------------------------------------------
    # Listing
    # ------------------------------------------------------------------

    async def list_expedientes(self, full_crawl: bool = False) -> list[ExpedienteCTBG]:
        """Return expedientes visible in /expedientes.6.

        Clicks "Mostrar más" until exhausted. When ``full_crawl`` is False,
        stops as soon as a fully-closed batch (no open expedientes) appears,
        because the list is sorted with the most recent activity first.
        """
        page = self._require_page()
        await self._navigate_authenticated(page, f"{self.portal_url}/expedientes.6")

        seen: set[str] = set()
        all_rows: list[ExpedienteCTBG] = []
        prev_total = 0
        for _ in range(60):  # safety cap on Mostrar más clicks
            rows = self._parse_expediente_rows(await page.content())
            new_rows = [r for r in rows if r.numero not in seen]
            for r in new_rows:
                seen.add(r.numero)
                all_rows.append(r)

            if not new_rows and len(rows) == prev_total:
                break
            prev_total = len(rows)

            if not full_crawl and new_rows and not any(r.is_open for r in new_rows):
                break

            try:
                btn = page.locator(
                    'button:has-text("Mostrar más"), a:has-text("Mostrar más")'
                ).first
                if not await btn.is_visible(timeout=1500):
                    break
                await btn.click()
                await page.wait_for_timeout(1200)
            except Exception:
                break

        return all_rows

    # ------------------------------------------------------------------
    # Detail walk
    # ------------------------------------------------------------------

    async def iter_documents(
        self,
        exp: ExpedienteCTBG,
        skip_csvs: set[str] | None = None,
        download_dir: Path | None = None,
    ) -> list[tuple[DocumentoCTBGExpediente, Path | None]]:
        """Open every phase of an expediente and return all card descriptors.

        When ``download_dir`` is given, every document whose key is not in
        ``skip_csvs`` is downloaded IMMEDIATELY, while its phase view is still
        open. Wicket download URLs are page-state-scoped: as soon as we
        navigate to another phase (or expediente) the server invalidates them
        and serves HTTP 500 or an HTML error page instead of the PDF —
        deferred downloads are not an option.

        Returns ``(doc, path)`` tuples; ``path`` is ``None`` when the doc was
        skipped (already synced), no ``download_dir`` was given (dry-run), or
        its download failed (caller should retry next cycle).
        """
        skip_csvs = skip_csvs or set()
        page = self._require_page()
        await self._navigate_authenticated(page, exp.detail_href)

        # Snapshot the phase labels — clicking each re-renders the page so we
        # cannot keep a stale list of locators.
        phase_labels: list[str] = await page.eval_on_selector_all(
            "a.singleActionLink.exp",
            "els => els.map(a => a.innerText.trim()).filter(t => t.length > 0)",
        )

        out: list[tuple[DocumentoCTBGExpediente, Path | None]] = []
        for label in phase_labels:
            try:
                await self._open_phase(page, label)
            except Exception as e:
                console.print(f"[yellow]CTBG {exp.numero}: fase {label!r} ignorada ({e})[/]")
                await self._safe_back_to_detail(page, exp)
                continue

            cards = await page.eval_on_selector_all(
                ".c-card__content",
                """els => els.map(el => ({
                    title: el.querySelector('.c-card-document__title')?.innerText.replace(/\\s+/g,' ').trim() || '',
                    text:  el.innerText || '',
                    href:  el.querySelector('a[href]')?.getAttribute('href') || ''
                }))""",
            )
            for card in cards:
                title = (card.get("title") or "").strip()
                href = (card.get("href") or "").strip()
                text = card.get("text") or ""
                # The detail view renders the phase tiles themselves as
                # .c-card__content whose only link is the phase navigation
                # anchor (href="#"). Real document cards always carry a
                # Wicket download URL.
                if not title or not href or href == "#":
                    continue
                csv_match = _CSV_RE.search(text)
                csv = csv_match.group(1) if csv_match else ""
                if not csv:
                    # User-attached docs (solicitud, acuse) and CTBG
                    # communications carry no CSV in their card. Derive a
                    # stable surrogate from expediente + phase + title so
                    # they still pass the "new docs" filter and dedup
                    # bookkeeping (Wicket hrefs are session-scoped, so they
                    # can't be the key).
                    digest = hashlib.sha1(
                        f"{exp.numero}|{label}|{title}".encode("utf-8")
                    ).hexdigest()[:20].upper()
                    csv = f"NOCSV-{digest}"
                doc = DocumentoCTBGExpediente(
                    expediente_ref=exp.numero,
                    phase=label,
                    title=title,
                    csv=csv,
                    descarga_href=urljoin(page.url, href),
                )

                dest: Path | None = None
                if download_dir is not None and doc.csv not in skip_csvs:
                    dest = download_dir / (
                        f"ctbg_{exp.numero.replace('/', '-')}_{doc.csv}.pdf"
                    )
                    try:
                        console.print(f"[dim]CTBG descargando [{label}] {title}...[/]")
                        await self._download_fresh(doc, dest)
                    except Exception as e:
                        console.print(
                            f"[red]CTBG: descarga falló para {title}: {str(e)[:200]}[/]"
                        )
                        dest = None

                out.append((doc, dest))

            await self._safe_back_to_detail(page, exp)

        return out

    # ------------------------------------------------------------------
    # Download
    # ------------------------------------------------------------------

    async def _download_fresh(self, doc: DocumentoCTBGExpediente, dest: Path) -> Path:
        """Download a card's PDF while its Wicket URL is still fresh.

        Must be called while the phase view that rendered ``doc`` is still
        the current page — see ``iter_documents``.
        """
        if self._context is None:
            raise RuntimeError("scraper not entered")
        dest.parent.mkdir(parents=True, exist_ok=True)
        response = await self._context.request.get(doc.descarga_href)
        body = await response.body()
        if response.status >= 400:
            raise RuntimeError(
                f"HTTP {response.status} para {doc.title}: {body[:200]!r}"
            )
        dest.write_bytes(body)
        return dest

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------

    def _require_page(self) -> Page:
        if self._page is None:
            raise RuntimeError("scraper not entered")
        return self._page

    async def _navigate_authenticated(self, page: Page, url: str) -> None:
        """Navigate and, if the sede shows the Identificación page (Wicket
        session expired), re-run the Cl@ve flow inline before retrying.

        Cookies in launch_persistent_context survive across processes for
        domains that set persistent cookies, but the CTBG sede uses session
        cookies — so a fresh browser instance often lands on the
        "Identificación electrónica" landing page even when our keyring still
        has cookies from a moments-old SessionManager run.
        """
        await page.goto(url, wait_until="domcontentloaded")
        if not await self._needs_clave(page):
            return

        await self._do_clave_flow(page)
        # After Cl@ve we tend to land on /info or similar — re-navigate.
        await page.goto(url, wait_until="domcontentloaded")
        if await self._needs_clave(page):
            raise SessionExpiredError(
                f"CTBG: tras Cl@ve seguimos en pantalla de identificación ({page.url})"
            )

    async def _needs_clave(self, page: Page) -> bool:
        url = page.url
        if "clave.gob.es" in url or "claveproxy" in url or "pasarela" in url:
            return True
        title = (await page.title()).lower()
        return "identificación electrónica" in title or "identificacion electronica" in title

    async def _do_clave_flow(self, page: Page) -> None:
        # Mirror of agent/auth/playwright_auth.py's CTBG branch.
        try:
            await page.get_by_text("Identifícate").first.click(timeout=8_000)
            console.print("[dim]CTBG expedientes: clic en 'Identifícate'[/]")
        except Exception:
            console.print("[dim]CTBG expedientes: no se encontró 'Identifícate'[/]")
        try:
            await page.locator('a:has-text("Cl@ve")').first.click(timeout=10_000)
            console.print("[dim]CTBG expedientes: clic en 'Cl@ve'[/]")
        except Exception:
            console.print("[dim]CTBG expedientes: no se encontró 'Cl@ve'[/]")
        try:
            await page.locator('button[onclick*="AFIRMA"]').first.click(timeout=10_000)
            console.print("[dim]CTBG expedientes: AFIRMA seleccionado[/]")
        except Exception:
            console.print("[dim]CTBG expedientes: no se pudo seleccionar AFIRMA[/]")
        # Wait for the round-trip to land back on the sede.
        try:
            await page.wait_for_url(f"{self.portal_url}/**", timeout=120_000)
            console.print("[green]CTBG expedientes: autenticación completada[/]")
        except PlaywrightTimeoutError as e:
            raise SessionExpiredError("CTBG: timeout esperando vuelta de Cl@ve") from e

    async def _open_phase(self, page: Page, label: str) -> None:
        # `:has-text` does substring match — escape backslashes / quotes by
        # using JS-passed text via JSON to avoid Playwright selector parsing.
        await page.locator("a.singleActionLink.exp", has_text=label).first.click()
        # Don't wait on .c-card__content: the detail view ALSO renders the
        # phase tiles as cards, so that selector resolves before the Wicket
        # re-render and we'd scrape the stale page. The goback link only
        # exists inside a phase view — it arrives in the same Ajax response
        # as the document cards, so it's the reliable "phase loaded" signal.
        await page.wait_for_selector("a.singleActionLink.goback", timeout=15_000)

    async def _safe_back_to_detail(self, page: Page, exp: ExpedienteCTBG) -> None:
        try:
            await page.locator("a.singleActionLink.goback").first.click(timeout=3_000)
            await page.wait_for_selector("a.singleActionLink.exp", timeout=10_000)
        except (PlaywrightTimeoutError, Exception):
            await page.goto(exp.detail_href, wait_until="domcontentloaded")

    def _parse_expediente_rows(self, html: str) -> list[ExpedienteCTBG]:
        soup = BeautifulSoup(html, "html.parser")
        table = soup.find("table", class_="EFolderInstanceListPanel")
        if not table:
            return []
        tbody = table.find("tbody")
        if not tbody:
            return []

        rows: list[ExpedienteCTBG] = []
        for tr in tbody.find_all("tr"):
            classes = tr.get("class") or []
            if "emptyRow" in classes:
                continue
            tds = tr.find_all("td")
            if len(tds) < 6:
                continue
            anchor = tds[0].find("a", class_="exp")
            if not anchor:
                continue
            numero = anchor.get_text(strip=True)
            href = anchor.get("href", "")
            detail_href = urljoin(self.portal_url + "/expedientes.6", href)
            titulo = tds[1].get_text(" ", strip=True)
            relacion = tds[2].get_text(strip=True)
            estado = tds[3].get_text(strip=True)
            fecha_apertura = tds[4].get_text(strip=True)
            fecha_cierre = tds[5].get_text(strip=True)
            rows.append(ExpedienteCTBG(
                numero=numero,
                titulo=titulo,
                relacion=relacion,
                estado=estado,
                fecha_apertura=fecha_apertura,
                fecha_cierre=fecha_cierre,
                detail_href=detail_href,
            ))
        return rows
