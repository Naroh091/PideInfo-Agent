"""Handler for ``present_complaint_reg`` AgentTask.

Drives Playwright through the REG / RED SARA wizard at
``https://reg.redsara.es/es/nuevo-registro`` to file a transparency
complaint with a regional transparency council that accepts complaints
via the General Electronic Registry (REG).

The wizard flow mirrors ``submit_request_reg`` with two differences:

  1. Step 2 fills fixed fields (asunto, expone_reg, solicita_reg) instead
     of reading from AccessRequest.expone/solicita. The EXPONE already
     includes the ``A/A: <organismo>`` header prepended by the backend.
  2. Step 3 uploads PDFs (solicitud always; resolución if branch=yes;
     justificante REG of the original request if available).

See docs/documentacion-procesos-envio/ for the full spec.
"""
from __future__ import annotations

import asyncio
import logging
import os
import platform
from pathlib import Path
from typing import Any, Optional

from runtime import FIREFOX_LAUNCH_ARGS
from tasks._submission import UncertainSubmission

logger = logging.getLogger(__name__)

_WIZARD_URL = "https://reg.redsara.es/es/nuevo-registro"
_SUBJECT = "Reclamación relativa a solicitud de acceso a información pública"


def _downloads_dir() -> Path:
    if platform.system() == "Windows":
        return Path(os.environ.get("USERPROFILE", str(Path.home()))) / "Downloads" / "PideInfo"
    return Path.home() / "Downloads" / "PideInfo"


def _is_headed_debug(settings) -> bool:
    from storage.preferences import is_headless_disabled
    return is_headless_disabled(settings)


def handle(task: dict, client: Any) -> None:
    """Sync entry point invoked by ``tasks.dispatch_*``."""
    from rich.console import Console
    console = Console()

    task_id = task["id"]
    payload = task["payload"]
    mode = task.get("mode") or "auto"

    console.print(f"[bold]present_complaint_reg[/] [{mode}] {task_id[:8]} — preparando")

    required = ("access_request_id", "dir3_code", "organismo_name", "expone_reg", "solicita_reg", "solicitante")
    missing = [k for k in required if not payload.get(k)]
    if missing:
        client.complete_task(task_id, success=False, error=f"missing_payload:{','.join(missing)}")
        return

    if not payload.get("solicitud_pdf_url"):
        client.complete_task(task_id, success=False, error="missing_pdf:solicitud")
        return

    work_dir = _downloads_dir() / f"present_complaint_reg_{task_id[:8]}"
    work_dir.mkdir(parents=True, exist_ok=True)

    client.progress_task(task_id, status="in_progress", note="Preparando sesión REG")

    try:
        result = asyncio.run(_drive(payload, console, client, task_id, mode, work_dir))
    except UncertainSubmission as e:
        logger.warning("present_complaint_reg uncertain: %s", e)
        try:
            from observability import capture_exception
            capture_exception(e, task_type="present_complaint_reg", task_id=task_id, mode=mode)
        except Exception:
            pass
        client.complete_task(
            task_id, outcome="uncertain",
            error=f"submission_uncertain:{e!s}"[:2000],
            result={"mode": mode, "work_dir": str(work_dir), "markers": e.markers},
        )
        return
    except Exception as e:
        logger.exception("present_complaint_reg pipeline crashed")
        try:
            from observability import capture_exception
            capture_exception(e, task_type="present_complaint_reg", task_id=task_id, mode=mode)
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
        f"[green]Tarea {task_id[:8]} completada — REGAGE {result.get('externalId', '?')}[/]"
    )


async def _drive(
    payload: dict,
    console: Any,
    client: Any,
    task_id: str,
    mode: str,
    work_dir: Path,
) -> dict:
    from playwright.async_api import async_playwright
    from config import Settings
    from auth.profile_seed import seed_from_master, promote_to_master
    from auth.redsara_session_manager import RedSaraSessionManager
    from portal_locks import lock_for

    settings = Settings()
    profile_dir = settings.firefox_profile_for("redsara")
    seed_from_master(profile_dir, settings.firefox_profile_master)

    headed_debug = _is_headed_debug(settings)

    session_manager = RedSaraSessionManager(
        portal_url="https://reg.redsara.es",
        cookies_file=settings.cookies_file,
        auth_timeout=settings.auth_timeout_seconds,
        firefox_profile_dir=profile_dir,
        firefox_profile_master=settings.firefox_profile_master,
        headless=not headed_debug,
    )
    await session_manager.get_valid_session()

    # Download PDFs before launching the browser so upload is instant.
    pdf_paths = await _download_pdfs(payload, work_dir, client)

    async with lock_for("redsara"), async_playwright() as p:
        firefox_user_prefs = {
            "security.osclientcerts.autoload": True,
            "security.default_personal_cert": "Select Automatically",
            "browser.sessionstore.resume_from_crash": False,
            "browser.startup.page": 0,
        }
        headless = not headed_debug
        context = await p.firefox.launch_persistent_context(
            user_data_dir=str(profile_dir),
            headless=headless,
            args=FIREFOX_LAUNCH_ARGS,
            firefox_user_prefs=firefox_user_prefs,
            locale="es-ES",
            ignore_https_errors=True,
            accept_downloads=True,
        )
        page = context.pages[0] if context.pages else await context.new_page()

        client.progress_task(task_id, status="in_progress", note="Abriendo wizard REG")
        await page.goto(_WIZARD_URL, wait_until="domcontentloaded")
        await _wait_wizard_ready(page)

        try:
            client.progress_task(task_id, status="in_progress", note="Paso 1: Datos del solicitante")
            await _step1_solicitante(page, payload["solicitante"], console)
            await _click_button(page, "Siguiente")

            client.progress_task(task_id, status="in_progress", note="Paso 2: Datos de la reclamación")
            await _wait_step_active(page, 2)
            await _step2_reclamacion(page, payload, console)
            await _click_button(page, "Siguiente")

            client.progress_task(task_id, status="in_progress", note="Paso 3: Documentación")
            await _wait_step_active(page, 3)
            await _step3_documentos(page, pdf_paths, console)
            await _click_button(page, "Siguiente")

            client.progress_task(task_id, status="in_progress", note="Paso 4: Firma")
            await _wait_step_active(page, 4)
            registry_number, registry_uuid = await _step4_firma(page, context, console)
        except Exception as e:
            await _capture_failure(page, work_dir, "filler_failed")
            raise

        promote_to_master(profile_dir, settings.firefox_profile_master)

    justificante_path: Optional[Path] = None
    if registry_uuid:
        client.progress_task(task_id, status="in_progress", note="Descargando justificante")
        try:
            from portals.redsara_rec import RedSaraRecScraper
            scraper = RedSaraRecScraper(session_manager)
            dest = work_dir / "justificante_reclamacion.pdf"
            ok = await scraper.download_justificante(registry_uuid, dest)
            justificante_path = dest if ok else None
        except Exception as exc:
            logger.warning("No se pudo descargar el justificante de la reclamación REG: %s", exc)

    # Marcar la reclamación como presentada en el backend (persiste el REGAGE
    # en AccessRequestComplaint y actualiza el estado del expediente).
    if registry_number:
        try:
            client.mark_complaint_filed(
                access_request_id=payload["access_request_id"],
                registry_no=registry_number,
                csv=None,       # REG no emite CSV; solo REGAGE
                filed_at=None,  # backend defaults to today
            )
        except Exception:
            logger.exception("mark_complaint_filed failed (REG)")

    # Subir el justificante al backend vinculado a la solicitud.
    if justificante_path and registry_number:
        try:
            await client.upload_filed_complaint_documents(
                registry_no=registry_number,
                files={"Justificante REG": justificante_path},
            )
        except Exception:
            logger.exception("upload_filed_complaint_documents failed (REG)")

    return {
        "externalId": registry_number,
        "registry_uuid": registry_uuid,
        "mode": mode,
        "work_dir": str(work_dir),
        "justificante": str(justificante_path) if justificante_path else None,
    }


async def _download_pdfs(payload: dict, work_dir: Path, client: Any) -> dict:
    """Download PDFs referenced in payload. Returns mapping key → local Path."""
    import httpx

    paths: dict[str, Optional[Path]] = {}
    urls = {
        "solicitud":    payload.get("solicitud_pdf_url"),
        "respuesta":    payload.get("respuesta_pdf_url"),
        "notificacion": payload.get("notificacion_pdf_url"),
        "justificante": payload.get("justificante_pdf_url"),
        "prorroga":     payload.get("prorroga_pdf_url"),
    }

    for key, url in urls.items():
        if not url:
            paths[key] = None
            continue
        dest = work_dir / f"{key}.pdf"
        try:
            pdf_bytes = client.download_pdf(url)
            dest.write_bytes(pdf_bytes)
            paths[key] = dest
            logger.debug("Downloaded %s → %s", key, dest)
        except Exception as exc:
            logger.warning("Could not download %s PDF (%s): %s", key, url, exc)
            paths[key] = None

    return paths


async def _step2_reclamacion(page, payload: dict, console) -> None:
    """Fill step 2: destination by DIR3, fixed asunto, and LLM-generated EXPONE/SOLICITA."""
    # Reuse the same DIR3 autocomplete logic from submit_request_reg.
    from tasks.submit_request_reg import (
        _PICK_DESTINATION_JS,
        _FILL_STEP2_TEXTS_JS,
    )

    dir3_code = payload["dir3_code"]

    # Select destination unit by DIR3 (same approach as submit_request_reg._step2_solicitud)
    dest_select = page.locator("dnt-select#destinationOrganism")
    await dest_select.scroll_into_view_if_needed()
    input_coords = await page.evaluate(
        """() => {
          const ds = document.querySelector('dnt-select#destinationOrganism');
          const dntInput = ds?.shadowRoot?.querySelector('dnt-input[role="combobox"]');
          const real = dntInput?.shadowRoot?.querySelector('input.dnt-input__inner');
          if (!real) return null;
          const r = real.getBoundingClientRect();
          return { x: r.left + r.width / 2, y: r.top + r.height / 2 };
        }"""
    )
    if not input_coords:
        raise RuntimeError("destination_combobox_input_not_found")
    await page.mouse.click(input_coords["x"], input_coords["y"])
    await page.keyboard.type(dir3_code, delay=80)
    pick_result = await page.evaluate(_PICK_DESTINATION_JS, {"dir3": dir3_code, "timeoutMs": 15_000})
    status = (pick_result or {}).get("status")
    if status == "ok":
        console.print(f"[dim]REG paso 2: destino seleccionado → {pick_result.get('text')!r}[/]")
    else:
        opts = (pick_result or {}).get("options")
        preview = ", ".join((opts or [])[:5]) if opts is not None else "?"
        raise RuntimeError(
            f"destination_pick_failed:{status or 'unknown'} dir3={dir3_code!r} options=[{preview}]"
        )

    # Fill asunto, expone, solicita
    text_payload = {
        "subject": _SUBJECT[:80],
        "exposes": (payload.get("expone_reg") or "")[:4000],
        "solicit": (payload.get("solicita_reg") or "")[:4000],
    }
    failures = await page.evaluate(_FILL_STEP2_TEXTS_JS, text_payload)
    if failures:
        console.print(f"[yellow]REG paso 2 reclamación: campos no rellenados: {failures}[/]")


async def _step3_documentos(page, pdf_paths: dict, console) -> None:
    """Upload attachments in step 3.

    REG paso 3 tiene un único botón "Explorar documentos" cuyo inner
    <button> inyecta un <input id="attachments" type="file" multiple>
    en el DOM principal (no en shadow root). El input acepta multiple
    ficheros de una sola llamada, así que subimos todos los PDFs a la
    vez vía Playwright's expect_file_chooser.

    Orden de adjuntos (se omiten los None):
      solicitud · respuesta · notificacion · justificante
    Límites del portal: máx 5 ficheros, 10 MB cada uno, 15 MB total.
    """
    paths_to_upload = [
        p for p in [
            pdf_paths.get("solicitud"),
            pdf_paths.get("prorroga"),
            pdf_paths.get("respuesta"),
            pdf_paths.get("notificacion"),
            pdf_paths.get("justificante"),
        ]
        if p is not None
    ]

    if not paths_to_upload:
        console.print("[dim]REG paso 3: sin adjuntos (ningún PDF disponible)[/]")
        return

    names = [p.name for p in paths_to_upload]
    console.print(f"[dim]REG paso 3: adjuntando {len(paths_to_upload)} fichero(s): {', '.join(names)}[/]")

    # El inner <button> del dnt-button "Explorar documentos" inyecta
    # un <input id="attachments" type="file" multiple> al ser pulsado.
    async with page.expect_file_chooser() as fc_info:
        await page.evaluate("""() => {
            const btn = [...document.querySelectorAll('dnt-button')]
                .find(b => /explorar/i.test(b.textContent || ''));
            btn?.shadowRoot?.querySelector('button')?.click();
        }""")
    file_chooser = await fc_info.value
    await file_chooser.set_files([str(p) for p in paths_to_upload])

    # Esperar a que el portal confirme la carga (lista de ficheros visible).
    try:
        await page.locator(f"text={paths_to_upload[0].name}").first.wait_for(
            state="visible", timeout=15_000
        )
        console.print(f"[green]REG paso 3: {len(paths_to_upload)} fichero(s) adjuntados[/]")
    except Exception:
        # No fatal — el portal puede mostrar el nombre de otra forma.
        console.print(f"[yellow]REG paso 3: ficheros enviados (confirmación visual no detectada)[/]")


# ──────────────────────────────────────────────────────────────────────────────
# Helpers shared with submit_request_reg (imported directly to avoid duplication)
# ──────────────────────────────────────────────────────────────────────────────

async def _wait_wizard_ready(page) -> None:
    from tasks.submit_request_reg import _wait_wizard_ready as _base
    return await _base(page)


async def _wait_step_active(page, step: int) -> None:
    from tasks.submit_request_reg import _wait_step_active as _base
    return await _base(page, step)


async def _click_button(page, label: str) -> None:
    from tasks.submit_request_reg import _click_button as _base
    return await _base(page, label)


async def _step1_solicitante(page, solicitante: dict, console) -> None:
    from tasks.submit_request_reg import _step1_solicitante as _base
    return await _base(page, solicitante, console)


async def _step4_firma(page, context, console):
    from tasks.submit_request_reg import _step4_firma as _base
    return await _base(page, context, console)


async def _capture_failure(page, work_dir: Path, label: str) -> None:
    from tasks.submit_request_reg import _capture_failure as _base
    return await _base(page, work_dir, label)
