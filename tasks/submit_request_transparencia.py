"""Handler for ``submit_request_transparencia`` AgentTask.

Drives Playwright through the AGE Portal de Transparencia wizard at
``/procedimiento/formulario?idProc=133628&idAmb={ID}``. Three steps:

  1. Datos del solicitante  — assumed pre-filled from the FNMT session.
  2. Datos de solicitud      — Asunto + Información que solicita.
  3. Firma de solicitud      — pick "Firma básica" (no AutoFirma) + the two
     mandatory checkboxes + Siguiente.

Discovery details and selectors live in
``app/docs/transparencia_age_submission.md``.

The form is built with ``dnt-*`` Web Components (Shadow DOM). We poke their
properties from JavaScript (``el.value = ...``) and dispatch input/change
events, because DOM-level CSS selectors can't reach into the closed shadow
roots reliably.

Submission produces a justificante PDF + an ``idBorr`` (borrador) reference
that the portal eventually upgrades to an ``idExpediente``. Both come back
to PideInfo via:

  - ``client.complete_task(success=True, result={...})`` for the
    structured metadata (externalId, sentAt, idBorr, idExpediente).
  - ``client.upload_filed_request_documents(...)`` (TODO: backend endpoint
    not yet implemented) for the PDF — falls back to a console log if the
    method isn't on the client yet.
"""
from __future__ import annotations

import asyncio
import logging
import os
import platform
from pathlib import Path
from typing import Any, Optional

logger = logging.getLogger(__name__)


def _downloads_dir() -> Path:
    if platform.system() == "Windows":
        return Path(os.environ.get("USERPROFILE", str(Path.home()))) / "Downloads" / "PideInfo"
    return Path.home() / "Downloads" / "PideInfo"


def handle(task: dict, client: Any) -> None:
    """Sync entry point invoked by ``tasks.dispatch_*``."""
    from rich.console import Console
    console = Console()

    task_id = task["id"]
    payload = task["payload"]
    mode = task.get("mode") or "auto"

    console.print(f"[bold]submit_request_transparencia[/] [{mode}] {task_id[:8]} — preparando")

    required = ("access_request_id", "transparency_portal_amb_id", "title", "description")
    missing = [k for k in required if not payload.get(k)]
    if missing:
        client.complete_task(task_id, success=False, error=f"missing_payload:{','.join(missing)}")
        return

    work_dir = _downloads_dir() / f"submit_request_transparencia_{task_id[:8]}"
    work_dir.mkdir(parents=True, exist_ok=True)

    client.progress_task(task_id, status="in_progress", note="Abriendo Portal de Transparencia")

    try:
        result = asyncio.run(_drive(payload, console, client, task_id, mode, work_dir))
    except Exception as e:
        logger.exception("submit_request_transparencia pipeline crashed")
        try:
            from observability import capture_exception
            capture_exception(e, task_type="submit_request_transparencia", task_id=task_id, mode=mode)
        except Exception:
            pass
        client.complete_task(
            task_id, success=False,
            error=f"pipeline_crashed:{e!s}"[:2000],
            result={"mode": mode, "work_dir": str(work_dir)},
        )
        return

    client.complete_task(task_id, success=True, result=result)
    console.print(
        f"[green]Tarea {task_id[:8]} completada — solicitud presentada[/]"
        f"{' (' + (result.get('externalId') or result.get('idBorr', '?')) + ')' if isinstance(result, dict) else ''}"
    )


async def _drive(
    payload: dict,
    console: Any,
    client: Any,
    task_id: str,
    mode: str,
    work_dir: Path,
) -> dict:
    """Drive the wizard end-to-end and forward the receipt back to PideInfo."""
    from playwright.async_api import async_playwright
    from config import Settings
    from auth.session_manager import SessionManager
    from auth.profile_seed import seed_from_master, promote_to_master
    from portal_locks import lock_for

    settings = Settings()
    portal_url = payload.get("transparency_portal_url") or settings.portal_url
    profile_dir = settings.firefox_profile_for("transparencia")
    seed_from_master(profile_dir, settings.firefox_profile_master)

    # Make sure we have a valid Cl@ve+FNMT session before we even open the
    # wizard. /procedimiento/formulario does NOT auto-redirect to Cl@ve when
    # unauthenticated — it just returns /error/401 — so we have to ensure
    # the session is alive up-front (verified against /privada/expedientes).
    session = SessionManager(
        portal_url=portal_url,
        cookies_file=settings.cookies_file,
        auth_timeout=settings.auth_timeout_seconds,
        firefox_profile_dir=profile_dir,
        firefox_profile_master=settings.firefox_profile_master,
        force_headed=False,
        portal_id="transparencia",
    )
    await session.get_valid_session()

    id_amb = int(payload["transparency_portal_amb_id"])
    wizard_url = f"{portal_url}/procedimiento/formulario?idProc=133628&idAmb={id_amb}"

    async with lock_for("transparencia"), async_playwright() as p:
        firefox_user_prefs = {
            "security.osclientcerts.autoload": True,
            "security.default_personal_cert": "Select Automatically",
            "browser.sessionstore.resume_from_crash": False,
            "browser.startup.page": 0,
        }
        # Headless by default. Set HEADLESS_DISABLED=true to debug visually.
        headless = not _is_headed_debug(settings)
        context = await p.firefox.launch_persistent_context(
            user_data_dir=str(profile_dir),
            headless=headless,
            firefox_user_prefs=firefox_user_prefs,
            locale="es-ES",
            ignore_https_errors=True,
        )
        page = context.pages[0] if context.pages else await context.new_page()

        # Firefox doesn't persist session cookies (no Expires) across launches,
        # so cookies.sqlite of a freshly-launched profile is empty for
        # transparencia.sede.gob.es even though the keyring/httpx side may
        # still have valid ones. /procedimiento/formulario does NOT redirect
        # to Cl@ve when unauthenticated — it just 401s. Force a silent Cl@ve
        # round-trip first to refresh Firefox's in-memory session cookies.
        client.progress_task(task_id, status="in_progress", note="Refrescando sesión Cl@ve")
        await _warm_up_clave_session(page, portal_url, console)

        client.progress_task(task_id, status="in_progress", note="Abriendo wizard PT")
        await page.goto(wizard_url, wait_until="domcontentloaded")
        if "/error/401" in page.url:
            from auth.session_manager import SessionExpiredError
            raise SessionExpiredError(
                f"transparencia: 401 al abrir el wizard tras warm-up ({page.url})."
            )

        # Wait for the dnt-* runtime to load and at least one dnt-input to render.
        try:
            await page.wait_for_function(
                "document.querySelectorAll('dnt-input').length > 0",
                timeout=20_000,
            )
        except Exception:
            await _capture_failure(page, work_dir, "wizard_did_not_render")
            raise

        try:
            client.progress_task(task_id, status="in_progress", note="Paso 1: Datos del solicitante")
            await _step1_advance(page, payload.get("solicitante") or {}, console)

            client.progress_task(task_id, status="in_progress", note="Paso 2: Asunto y solicitud")
            await _step2_fill_request(page, payload, console)
            id_borr = await _step2_submit_capture_id_borr(page, console)

            client.progress_task(task_id, status="in_progress", note="Paso 3: Firma básica")
            await _step3_sign(page, console)

            client.progress_task(task_id, status="in_progress", note="Capturando justificante")
            id_exp, expediente_ref, downloaded_files = await _capture_receipt(
                page, context, work_dir, console, id_borr=id_borr,
            )
            # Successful signing must produce either an idExp or an
            # expedienteRef. If we only have idBorr, the firma never
            # completed and the request is still a draft — fail loudly
            # so the user knows to retry instead of seeing a misleading
            # success notification.
            if not id_exp and not expediente_ref:
                raise RuntimeError(
                    f"firma_no_registrada: solo tenemos idBorr={id_borr!r}, "
                    f"el portal no devolvió idExp ni expedienteRef "
                    f"(URL final: {page.url}). La solicitud quedó como borrador."
                )
            external_id = expediente_ref or id_exp
            justificante_path = downloaded_files.get("Justificante") or downloaded_files.get("Solicitud")
        except Exception as e:
            await _capture_failure(page, work_dir, "filler_failed")
            if settings.debug:
                console.print(
                    "[bold yellow]DEBUG=True: dejando navegador abierto. URL: {}\n"
                    "Error: {}\nCtrl+C para cerrar.[/]".format(page.url, e)
                )
                while True:
                    await asyncio.sleep(60)
            raise

    # Promote the profile so other portals don't have to re-prompt for the
    # cert. Done AFTER the `async with` above closes the browser context — on
    # Windows Firefox holds the profile locked (parent.lock & co.) while it
    # runs, so an in-session copy raises PermissionError [Errno 13].
    try:
        promote_to_master(profile_dir, settings.firefox_profile_master)
    except Exception as exc:  # profile-copy is an optimisation, never fatal
        console.print(f"[dim]No se pudo promover el perfil maestro: {exc}[/]")

    # -- Post-submission webhook (out of the browser context) --------------
    upload_summaries: list[dict] = []
    for label, pdf in downloaded_files.items():
        if not pdf.exists():
            continue
        try:
            summary = await _upload_pdf(
                client,
                access_request_id=payload["access_request_id"],
                external_id=external_id or id_borr or "",
                label=label,
                pdf_path=pdf,
            )
            upload_summaries.append({"label": label, **summary})
        except Exception:
            logger.exception("upload of %s failed", label)

    return {
        "mode": mode,
        "externalId": external_id,
        "expedienteRef": expediente_ref,
        "idExp": id_exp,
        "idBorr": id_borr,
        "sentAt": _utcnow_iso(),
        "registry_no": external_id,
        "downloads": {label: str(p) for label, p in downloaded_files.items()},
        "upload_summaries": upload_summaries,
    }


# ─────────────────────────────────────────────────────────────────────────
# Step helpers — interact with dnt-* Web Components via page.evaluate.
# ─────────────────────────────────────────────────────────────────────────


async def _step1_advance(page, profile: dict | None, console) -> None:
    """Step 1 (Datos del solicitante).

    The FNMT session pre-fills identity (NIF, nombre, apellidos, tipo doc)
    but **not** contact data (email, address). The ``payload.solicitante``
    bag may carry any subset of:
        email, provincia_ine, municipio, codigo_postal, via, telefono, pais

    PideInfo currently only ships ``email`` (from ``User.email``) — the
    rest of the fields are typed manually by the user during the first
    submission and the persistent Firefox profile of the agent caches
    them for subsequent runs. If the form rejects the advance because
    those fields are still empty, the user has to do one headed
    submission to populate the cache.

    Discovery (2026-05-02 with David's session) confirmed that:
      - The "En nombre propio" radio (value="1") needs to be ticked even
        though the FNMT session already implies personal capacity.
      - All `dnt-input` / `dnt-select` accept programmatic value updates
        as long as we dispatch `input` + `change` events with `composed:true`.
      - There is at least one hidden `dnt-input` whose value is regenerated
        by other step-1 components on validation. Setting it from JS does
        not stick — the portal needs the actual click events that Playwright
        synthesizes natively (not the JS-only `.click()` MCP can issue),
        which is why the agent uses `page.click(...)` rather than evaluate.
    """
    profile = profile or {}

    # Tick "En nombre propio" — needed even though FNMT brings identity.
    try:
        await _select_dnt_radio_by_value(page, "1")
    except RuntimeError:
        # No radio rendered (older portal version?). Continue and let the
        # validation surface tell us if it was actually required.
        pass

    # Fill the contact / address fields if the payload carries them.
    field_map = [
        ("Correo electrónico", profile.get("email")),
        ("Municipio",          profile.get("municipio")),
        ("Municipio / Población", profile.get("municipio")),
        ("Código Postal",      profile.get("codigo_postal")),
        ("Nombre vía, número", profile.get("via")),
        ("Teléfono",           profile.get("telefono")),
    ]
    for label, value in field_map:
        if not value:
            continue
        try:
            await _set_dnt_input_by_label(page, label, str(value))
        except RuntimeError:
            # Field may not be rendered for this organism; skip silently.
            pass

    if profile.get("provincia_ine"):
        try:
            await _set_dnt_select_by_value(page, label_includes="Provincia", value=str(profile["provincia_ine"]))
        except RuntimeError:
            # Fallback: pick the third dnt-select (provincia is at index 2 on AGE).
            try:
                await _set_dnt_select_at_index(page, 2, str(profile["provincia_ine"]))
            except RuntimeError:
                pass

    if profile.get("pais"):
        try:
            await _set_dnt_select_at_index(page, 1, str(profile["pais"]))
        except RuntimeError:
            pass

    await _click_dnt_button(page, "Siguiente")
    await asyncio.sleep(0.8)

    errors = await _collect_validation_errors(page)
    still_on_step1 = await _is_step_active(page, 1)
    if still_on_step1 and errors:
        raise RuntimeError(
            "step1_required_fields_missing: " + "; ".join(errors[:6])
        )


async def _step2_fill_request(page, payload: dict, console) -> None:
    title = (payload.get("title") or "").strip()
    description = (payload.get("description") or "").strip()
    if not title or not description:
        raise RuntimeError("missing_title_or_description")

    await _set_dnt_input_by_label(page, "Asunto", title)
    await _set_dnt_input_by_label(page, "Información que solicita", description)


async def _step2_submit_capture_id_borr(page, console) -> Optional[str]:
    """Click "Siguiente" and wait for the URL to flip to /procedimiento/firma
    with ?idBorr=…. The portal mints the borrador on this transition.

    The mint is a server round-trip behind an "Estamos procesando tu
    solicitud…" spinner that can run for minutes under load — a fixed
    timeout is the wrong tool. Instead we poll: as long as that spinner is
    up the portal is still working, so we keep waiting (up to a hard cap);
    only once the spinner clears WITHOUT reaching /firma do we treat it as a
    real failure and read the (visible) validation errors.
    """
    # Hard ceiling for the borrador mint. Overridable without a rebuild via
    # PT_STEP2_MINT_TIMEOUT_S — handy while we still don't know the portal's
    # real worst case.
    overall_cap_s = int(os.getenv("PT_STEP2_MINT_TIMEOUT_S", "360"))
    chunk_ms = 20_000

    await _click_dnt_button(page, "Siguiente")

    waited = 0
    while True:
        try:
            await page.wait_for_url("**/procedimiento/firma?**", timeout=chunk_ms)
            return _extract_query_param(page.url, "idBorr")
        except Exception:
            waited += chunk_ms // 1000
            if await _portal_is_processing(page):
                if waited >= overall_cap_s:
                    raise RuntimeError(
                        f"step2_portal_timeout: el portal lleva {waited}s en "
                        "'Estamos procesando tu solicitud' sin emitir el borrador"
                    )
                console.print(f"[dim]Portal procesando el envío… ({waited}s)[/]")
                continue
            # Spinner gone and still not on /firma → genuine step-2 failure.
            errors = await _collect_validation_errors(page)
            if errors:
                raise RuntimeError("step2_validation: " + "; ".join(errors[:6]))
            raise RuntimeError(
                "step2_did_not_advance: el portal dejó de procesar sin avanzar "
                f"a la firma (URL: {page.url})"
            )


async def _portal_is_processing(page) -> bool:
    """True while the portal shows its "Estamos procesando tu solicitud…"
    overlay — a visible `dnt-spinner` or that text both count. Used to tell a
    slow-but-working mint apart from a stalled one."""
    try:
        return await page.evaluate(
            """() => {
                const visible = (el) => {
                    if (!el) return false;
                    const cs = getComputedStyle(el);
                    if (cs.display === 'none' || cs.visibility === 'hidden') return false;
                    const r = el.getBoundingClientRect();
                    return r.width > 0 && r.height > 0;
                };
                for (const sp of document.querySelectorAll('dnt-spinner')) {
                    if (visible(sp)) return true;
                }
                return /procesando tu solicitud/i.test(document.body.innerText || '');
            }"""
        )
    except Exception:
        return False


async def _step3_sign(page, console) -> None:
    """Pick "Firma básica" (value=basica), tick both mandatory checkboxes,
    click "Siguiente", and survive the Cl@ve round-trip.

    Discovered (2026-05-02): even "Firma básica (no criptográfica)" requires
    a final pasaje through `pasarela-ident.clave.gob.es` as a non-repudiation
    confirmation. The portal stamps the URL with `signValidation={uuid}`,
    redirects to Cl@ve, expects the FNMT certificate to be auto-picked from
    the persistent profile, then redirects back to the portal which lands on
    the confirmation page.
    """
    await page.wait_for_function(
        "document.querySelectorAll('dnt-radio-group').length > 0",
        timeout=15_000,
    )
    await _select_dnt_radio_by_value(page, "basica")
    # Tick every checkbox on this step (we know there are exactly two:
    # privacy + veracidad). Idempotent if already checked.
    await _check_all_dnt_checkboxes(page)
    await _click_dnt_button(page, "Siguiente")

    # The Siguiente click triggers either:
    #   (a) navigation to clave.gob.es / pasarela-ident.clave.gob.es
    #   (b) inline transition to the confirmation page (no clave bounce)
    # Wait for ANY navigation off /procedimiento/firma OR the appearance
    # of a clave URL. Server side this can be slow (mint of signValidation
    # token + SAML AuthnRequest), so the timeout is generous.
    try:
        await page.wait_for_url(
            lambda url: ("clave.gob.es" in url) or ("/procedimiento/firma" not in url),
            timeout=120_000,
        )
    except Exception:
        errors = await _collect_validation_errors(page)
        raise RuntimeError(
            "step3_did_not_advance: " + ("; ".join(errors[:6]) if errors else page.url)
        )

    # If we ended up on Cl@ve, ride the bounce back. The persistent FNMT
    # profile usually picks the cert without prompting; if it stalls (e.g.
    # the user hasn't completed the cert picker yet), we time out.
    if "clave.gob.es" in page.url:
        console.print("[dim]transparencia: pasarela Cl@ve detectada para confirmación de firma[/]")
        # Try a few selectors for the AFIRMA / DNIe option in case the
        # markup changes between runs (same set as the warm-up handshake).
        selectors = [
            'button[onclick*="AFIRMA"]',
            'button[onclick*="afirma"]',
            'a[href*="AFIRMA"]',
            'button:has-text("DNIe")',
            'button:has-text("Certificado electrónico")',
        ]
        for sel in selectors:
            try:
                btn = page.locator(sel).first
                await btn.wait_for(state="visible", timeout=5_000)
                await btn.click()
                console.print(f"[dim]transparencia: AFIRMA clic via {sel!r} (firma)[/]")
                break
            except Exception:
                continue
        try:
            await page.wait_for_url("**/transparencia.sede.gob.es/**", timeout=180_000)
            console.print("[green]transparencia: vuelta de Cl@ve OK[/]")
        except Exception as e:
            from auth.session_manager import SessionExpiredError
            raise SessionExpiredError(
                f"transparencia: timeout esperando vuelta de Cl@ve tras la firma ({page.url})"
            ) from e

    # Final wait: confirm we left /procedimiento/firma. The portal lands on
    # /procedimiento/confirmacionSolicitud?idExp=N when registration succeeds.
    #
    # We used to be lenient about ``signValidation`` in the URL but that's a
    # FALSE positive: the portal stamps signValidation while the borrador is
    # still pending non-repudiation confirmation, NOT after success. Treating
    # it as success caused the task to report success while the request
    # stayed as a draft on the portal. Require the URL to actually leave
    # /procedimiento/firma.
    try:
        await page.wait_for_url(
            lambda url: "/procedimiento/firma" not in url,
            timeout=360_000,
        )
    except Exception as e:
        raise RuntimeError(
            f"step3_stuck_on_firma: la firma no se completó, el portal sigue en {page.url} "
            "(probablemente la solicitud quedó como borrador)"
        ) from e


async def _capture_receipt(
    page,
    context,
    work_dir: Path,
    console,
    id_borr: Optional[str],
) -> tuple[Optional[str], Optional[str], dict[str, Path]]:
    """Capture the post-submit confirmation: ``idExp``, the public
    ``expedienteRef`` (``ES_E04996103_YYYY_EXP_AC…``), and download both
    PDFs the portal exposes (solicitud + justificante de registro).

    Discovery (sesión 2026-05-02 con David, envío real) confirmó:
      - URL: ``/procedimiento/confirmacionSolicitud?idExp={idExp}``
      - Texto visible: ``Número de solicitud - ES_E04996103_2026_EXP_AC2000000676676``
      - 2 botones ``dnt-button:has-text("Descarga")`` cuyo `<a>` interno
        apunta a ``/.rest/download/v1/descargaDocumento?id={N}&tipoDocumento=docExpediente``.
        Mismo endpoint que el scraper de AGE ya usa.

    Returns ``(idExp, expedienteRef, files)`` where ``files`` maps a human
    label ("Solicitud", "Justificante") to the downloaded path.
    """
    import re

    id_exp = _extract_query_param(page.url, "idExp")

    # Public expediente reference — distinct from the internal idExp. Format
    # observed: ES_E04996103_2026_EXP_AC2000000676676 (where the trailing
    # block embeds the idExp).
    expediente_ref: Optional[str] = None
    try:
        body_txt = await page.locator("body").inner_text(timeout=3_000)
        m = re.search(r"(ES_E\d+_\d{4}_EXP_AC\d+)", body_txt)
        if m:
            expediente_ref = m.group(1)
    except Exception:
        pass

    # Walk the dnt-button[Descarga …] elements, follow their internal href,
    # download via context.request (cookies carried automatically).
    files: dict[str, Path] = {}
    try:
        download_metas = await page.evaluate(
            """() => {
                const out = [];
                for (const btn of document.querySelectorAll('dnt-button')) {
                    const sr = btn.shadowRoot;
                    const text = ((sr && sr.textContent) || btn.textContent || '').replace(/\\s+/g,' ').trim();
                    if (!/Descarga/i.test(text)) continue;
                    const innerA = sr && sr.querySelector('a');
                    if (!innerA) continue;
                    out.push({ text: text.slice(0, 80), href: innerA.getAttribute('href') || '' });
                }
                return out;
            }"""
        )
    except Exception:
        download_metas = []

    for entry in download_metas or []:
        href = entry.get("href") or ""
        text = entry.get("text") or "Documento"
        if not href:
            continue
        url = href if href.startswith("http") else f"https://transparencia.sede.gob.es{href}"
        try:
            response = await context.request.get(url, timeout=60_000)
            if not response.ok:
                console.print(f"[yellow]descarga {text!r} HTTP {response.status}[/]")
                continue
            label = "Justificante" if "justificante" in text.lower() else "Solicitud"
            target = work_dir / f"{label.lower()}_{id_exp or 'pt'}.pdf"
            target.write_bytes(await response.body())
            files[label] = target
            console.print(f"[dim]→ {label}: {target} ({target.stat().st_size} bytes)[/]")
        except Exception as e:
            console.print(f"[yellow]descarga {text!r} falló: {e}[/]")

    # Fallback: if we couldn't capture the public expedienteRef but we DO
    # have idExp, build the most likely shape so the AccessRequest has a
    # consistent reference. Only the AC-suffix changes.
    if not expediente_ref and id_exp:
        from datetime import datetime
        expediente_ref = f"ES_E04996103_{datetime.now().year}_EXP_AC{id_exp}"

    return id_exp, expediente_ref, files


# ─────────────────────────────────────────────────────────────────────────
# DOM utilities — talk to dnt-* Web Components via JS evaluation.
# ─────────────────────────────────────────────────────────────────────────


async def _set_dnt_input_by_label(page, label: str, value: str) -> None:
    """Locate a `<dnt-input>` by its `label` property and set its value.
    Dispatches input + change events so the underlying form reactivity
    notices the new value (Stencil-based components listen on change)."""
    ok = await page.evaluate(
        """(args) => {
            const inputs = [...document.querySelectorAll('dnt-input,dnt-textarea')];
            const target = inputs.find(el => (el.label || '').trim() === args.label.trim());
            if (!target) return false;
            target.value = args.value;
            target.dispatchEvent(new Event('input', { bubbles: true, composed: true }));
            target.dispatchEvent(new Event('change', { bubbles: true, composed: true }));
            // Fall back to the inner native input if the host has one in shadow.
            const sr = target.shadowRoot;
            if (sr) {
                const inner = sr.querySelector('input,textarea');
                if (inner) {
                    inner.value = args.value;
                    inner.dispatchEvent(new Event('input', { bubbles: true, composed: true }));
                    inner.dispatchEvent(new Event('change', { bubbles: true, composed: true }));
                }
            }
            return true;
        }""",
        {"label": label, "value": value},
    )
    if not ok:
        raise RuntimeError(f"dnt_input_not_found: label={label!r}")


async def _select_dnt_radio_by_value(page, value: str) -> None:
    """Tick a `<dnt-radio>` by its `.value` property AND propagate the value
    to the parent `<dnt-radio-group>`.

    Discovered the hard way (2026-05-02): the parent group validates its
    OWN `.value`, not the children's `.checked`. Setting only the radio's
    `checked = true` leaves the group thinking nothing was picked and the
    portal returns "Este campo es obligatorio" on submit.
    """
    ok = await page.evaluate(
        """(args) => {
            const radios = [...document.querySelectorAll('dnt-radio')];
            const target = radios.find(r => (r.value || '').trim() === args.value);
            if (!target) return false;
            target.checked = true;
            target.dispatchEvent(new Event('change', { bubbles: true, composed: true }));
            // Propagate to the enclosing dnt-radio-group: that's what gets
            // validated on submit.
            const group = target.closest('dnt-radio-group');
            if (group) {
                group.value = args.value;
                group.dispatchEvent(new Event('change', { bubbles: true, composed: true }));
                group.dispatchEvent(new Event('input', { bubbles: true, composed: true }));
            }
            try { target.click(); } catch (_) {}
            return true;
        }""",
        {"value": value},
    )
    if not ok:
        raise RuntimeError(f"dnt_radio_not_found: value={value!r}")


async def _set_dnt_select_by_value(page, *, label_includes: str, value: str) -> None:
    """Set a `<dnt-select>` whose label or aria-label contains the given
    substring (case-insensitive) to the given option `.value`."""
    ok = await page.evaluate(
        """(args) => {
            const needle = args.needle.toLowerCase();
            const sels = [...document.querySelectorAll('dnt-select')];
            const target = sels.find(s => {
                const lbl = ((s.label || '') + ' ' + (s.getAttribute('aria-label') || '')).toLowerCase();
                return lbl.includes(needle);
            });
            if (!target) return false;
            target.value = args.value;
            target.dispatchEvent(new Event('change', { bubbles: true, composed: true }));
            target.dispatchEvent(new Event('input', { bubbles: true, composed: true }));
            return true;
        }""",
        {"needle": label_includes, "value": value},
    )
    if not ok:
        raise RuntimeError(f"dnt_select_not_found: label~{label_includes!r}")


async def _set_dnt_select_at_index(page, idx: int, value: str) -> None:
    """Fallback: set a `<dnt-select>` by zero-based DOM index."""
    ok = await page.evaluate(
        """(args) => {
            const sel = [...document.querySelectorAll('dnt-select')][args.idx];
            if (!sel) return false;
            sel.value = args.value;
            sel.dispatchEvent(new Event('change', { bubbles: true, composed: true }));
            sel.dispatchEvent(new Event('input', { bubbles: true, composed: true }));
            return true;
        }""",
        {"idx": idx, "value": value},
    )
    if not ok:
        raise RuntimeError(f"dnt_select_index_out_of_range: idx={idx}")


async def _check_all_dnt_checkboxes(page) -> None:
    await page.evaluate(
        """() => {
            for (const cb of document.querySelectorAll('dnt-checkbox')) {
                if (cb.checked === true) continue;
                cb.checked = true;
                cb.dispatchEvent(new Event('change', { bubbles: true, composed: true }));
                try { cb.click(); } catch (_) {}
            }
        }"""
    )


async def _click_dnt_button(page, label: str) -> None:
    """Click a `<dnt-button>` whose visible text matches the given label.

    Uses Playwright's real mouse click (dispatches mousedown → mouseup →
    click) on the host element rather than a synthetic ``inner.click()``
    from JS.

    Discovery (2026-05-04): step-2 submits were rejected with a generic
    "formato no válido" until we switched to a real click. dnt-button's
    pre-submit pipeline relies on ``mousedown`` to commit any pending
    state on the dnt-input siblings; a bare JS click skips that and the
    form sees the still-"pending" inputs as malformed even though their
    visible values are correct. A manual user click on the same form
    advanced without changes — confirming the click mechanism is what
    matters, not the field values.

    ``:has-text`` doesn't pierce the dnt-button shadow root, so we tag
    the matching host with a data attribute via JS and click that
    selector through Playwright.
    """
    label_norm = label.strip().lower()
    tagged = await page.evaluate(
        """(args) => {
            for (const el of document.querySelectorAll('dnt-button[data-pideinfo-click]')) {
                el.removeAttribute('data-pideinfo-click');
            }
            // The wizard renders all 3 steps at once and toggles visibility
            // on the inactive ones, so several "Siguiente" buttons coexist.
            // Filter to the visible ones — the one inside the active step
            // is the only clickable target.
            const isVisible = (el) => {
                const cs = getComputedStyle(el);
                if (cs.display === 'none' || cs.visibility === 'hidden') return false;
                if (el.offsetParent === null && cs.position !== 'fixed') return false;
                const rect = el.getBoundingClientRect();
                return rect.width > 0 && rect.height > 0;
            };
            const buttons = [...document.querySelectorAll('dnt-button')].filter(isVisible);
            const target = buttons.find(b => {
                const txt = (b.shadowRoot && b.shadowRoot.textContent) || b.textContent || '';
                return txt.trim().toLowerCase() === args.label;
            });
            if (!target) return false;
            target.setAttribute('data-pideinfo-click', 'target');
            return true;
        }""",
        {"label": label_norm},
    )
    if not tagged:
        raise RuntimeError(f"dnt_button_not_found_or_hidden: label={label!r}")
    try:
        await page.click('dnt-button[data-pideinfo-click="target"]')
    finally:
        try:
            await page.evaluate(
                """() => {
                    for (const el of document.querySelectorAll('dnt-button[data-pideinfo-click]')) {
                        el.removeAttribute('data-pideinfo-click');
                    }
                }"""
            )
        except Exception:
            pass


async def _is_step_active(page, step_index: int) -> bool:
    """Returns True if the visible `dnt-step` at ``step_index`` (1-based)
    looks active. The portal renders all 3 steps in DOM and toggles a
    `[active]` attribute on the current one."""
    return await page.evaluate(
        """(args) => {
            const steps = [...document.querySelectorAll('dnt-step')];
            const idx = args.idx - 1;
            if (idx < 0 || idx >= steps.length) return false;
            const s = steps[idx];
            return !!(s.active || s.getAttribute('active') !== null
                || s.getAttribute('aria-current') === 'step'
                || (s.shadowRoot && s.shadowRoot.querySelector('.is-active, [class*="active"]')));
        }""",
        {"idx": step_index},
    )


async def _collect_validation_errors(page) -> list[str]:
    """Pull error messages from dnt-* components that are actually VISIBLE.

    The wizard keeps all three steps in the DOM (toggling visibility), and a
    Stencil component can retain a stale `.error` property from an earlier
    pass. Scanning blindly yields false positives — e.g. a "formato no
    válido" scraped off a hidden field while the real problem is elsewhere.
    Restricting to visible elements keeps the report trustworthy."""
    return await page.evaluate(
        """() => {
            const visible = (el) => {
                const cs = getComputedStyle(el);
                if (cs.display === 'none' || cs.visibility === 'hidden') return false;
                if (el.offsetParent === null && cs.position !== 'fixed') return false;
                const r = el.getBoundingClientRect();
                return r.width > 0 && r.height > 0;
            };
            const out = [];
            for (const el of document.querySelectorAll('dnt-input,dnt-textarea,dnt-select,dnt-radio-group,dnt-checkbox-group')) {
                if (!visible(el)) continue;
                const err = el.error || (el.errorMessage || '');
                if (typeof err === 'string' && err.trim() !== '') {
                    const label = (el.label || '').trim();
                    out.push(label ? label + ': ' + err.trim() : err.trim());
                }
            }
            return out;
        }"""
    )


def _extract_query_param(url: str, key: str) -> Optional[str]:
    from urllib.parse import urlparse, parse_qs
    try:
        qs = parse_qs(urlparse(url).query)
        values = qs.get(key)
        if values:
            return values[0]
    except Exception:
        pass
    return None


def _is_headed_debug(settings) -> bool:
    from storage.preferences import is_headless_disabled
    return is_headless_disabled(settings)


def _utcnow_iso() -> str:
    from datetime import datetime, timezone
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S%z")


async def _warm_up_clave_session(page, portal_url: str, console) -> None:
    """Trigger a silent Cl@ve handshake inside the current Firefox context so
    transparencia.sede.gob.es session cookies land in the in-memory cookie
    jar before we navigate to ``/procedimiento/formulario``.

    Hits ``/claveproxy/clave/authenticate?returnUrl=/privada/expedientes`` →
    if Cl@ve shows the IdP picker, click AFIRMA → cert is auto-selected
    from the trained profile → portal stamps session cookies → land at
    ``/privada/expedientes``.

    Cookies on ``*.clave.gob.es`` are session-only too, so the IdP picker
    may appear on every cold start; we always click through it explicitly
    rather than counting on a remembered choice.
    """
    import urllib.parse
    from auth.session_manager import SessionExpiredError

    return_url = urllib.parse.quote(f"{portal_url}/privada/expedientes")
    target = f"{portal_url}/claveproxy/clave/authenticate?returnUrl={return_url}"

    await page.goto(target, wait_until="domcontentloaded")

    # If we already landed back at the portal (cached session lucky path), done.
    if "transparencia.sede.gob.es" in page.url and "claveproxy" not in page.url:
        if "/privada/expedientes" in page.url:
            console.print("[dim]transparencia: sesión Cl@ve aún viva, sin pasarela[/]")
            return

    # Otherwise we should be on the Cl@ve pasarela. Pick AFIRMA explicitly —
    # try a few selectors in case the markup changes.
    if "clave.gob.es" in page.url or "claveproxy" in page.url:
        clicked = False
        selectors = [
            'button[onclick*="AFIRMA"]',
            'button[onclick*="afirma"]',
            'a[href*="AFIRMA"]',
            'button:has-text("DNIe")',
            'button:has-text("Certificado electrónico")',
        ]
        for sel in selectors:
            try:
                btn = page.locator(sel).first
                await btn.wait_for(state="visible", timeout=5_000)
                await btn.click()
                clicked = True
                console.print(f"[dim]transparencia: AFIRMA clic via {sel!r}[/]")
                break
            except Exception:
                continue
        if not clicked:
            raise SessionExpiredError(
                f"transparencia: pasarela Cl@ve sin botón AFIRMA reconocible ({page.url})"
            )

    # Wait for navigation back to the portal. Treat /error/ as a definite fail
    # so we surface a useful diagnostic instead of sitting until the timeout.
    try:
        await page.wait_for_url(
            lambda url: "transparencia.sede.gob.es" in url and "claveproxy" not in url,
            timeout=180_000,
        )
    except Exception as e:
        raise SessionExpiredError(
            f"transparencia: warm-up de Cl@ve no volvió al portal ({page.url})"
        ) from e

    if "/error/" in page.url:
        raise SessionExpiredError(
            f"transparencia: warm-up de Cl@ve devolvió error del portal ({page.url})"
        )
    if "/privada/expedientes" not in page.url:
        raise SessionExpiredError(
            f"transparencia: warm-up de Cl@ve aterrizó fuera de /privada/expedientes ({page.url})"
        )

    console.print("[dim]transparencia: sesión Cl@ve refrescada[/]")


async def _capture_failure(page, work_dir: Path, label: str) -> None:
    work_dir.mkdir(parents=True, exist_ok=True)
    target = work_dir / f"failure_{label}.png"
    try:
        await page.screenshot(path=str(target), full_page=True)
        logger.warning("submit_request_transparencia screenshot: %s", target)
    except Exception:
        logger.exception("screenshot failed")


# ─────────────────────────────────────────────────────────────────────────
# Webhook upload — falls back if the dedicated client method isn't there.
# ─────────────────────────────────────────────────────────────────────────


async def _upload_pdf(
    client,
    *,
    access_request_id: str,
    external_id: str,
    label: str,
    pdf_path: Path,
) -> dict:
    """Forward a single PDF (Solicitud or Justificante) to PideInfo using
    the same agent webhook the AGE scraper already uses
    (``source=transparencia_age``). The backend's existing
    ``AgentWebhookProcessor`` dedups by content hash and links the
    document to the matching ``AccessRequest`` via ``expedienteRef``."""
    import base64
    import hashlib
    import httpx

    pdf_bytes = pdf_path.read_bytes()
    safe_label = label.replace("/", "-")
    payload = {
        "source": "transparencia_age",
        "expedienteRef": external_id or "",
        "documents": [{
            "filename": f"{safe_label} - {external_id or access_request_id}.pdf",
            "contentType": "application/pdf",
            "content": base64.b64encode(pdf_bytes).decode("ascii"),
            "contentHash": hashlib.sha256(pdf_bytes).hexdigest(),
        }],
        "metadata": {
            "submission_kind": "agent_submitted",
            "submission_label": label,
            "access_request_id": access_request_id,
        },
    }

    webhook_url = getattr(client, "_webhook_url", None)
    if callable(webhook_url):
        url = webhook_url()
    else:
        url = f"{client.base_url}/api/agent/webhook"
    auth_headers = getattr(client, "_auth_headers", {}) or {}

    async with httpx.AsyncClient(timeout=120) as http:
        response = await http.post(
            url,
            json=payload,
            headers={**auth_headers, "Content-Type": "application/json"},
        )
        response.raise_for_status()
        return response.json()
