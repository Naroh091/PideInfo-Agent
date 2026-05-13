"""Handler for ``submit_request_reg`` AgentTask.

Drives Playwright through the REG / RED SARA wizard at
``https://reg.redsara.es/es/nuevo-registro``. Four steps:

  1. Datos del solicitante  — modo "Interesado", dirección postal,
     teléfono + email, marcar avisos.
  2. Datos de solicitud      — buscar Unidad por DIR3, rellenar asunto,
     EXPONE y SOLICITA (cada uno ≤4000 caracteres).
  3. Documentación           — adjuntar el PDF generado por PideInfo.
  4. Firma de solicitud      — firmar con Cl@ve (certificado FNMT
     preinstalado en el perfil Firefox) y capturar el REGAGE.

REG es una SPA Angular: usamos selectores Angular Material (mat-radio,
mat-form-field, mat-select, mat-option) y nos apoyamos en el "rol" /
texto visible cuando el DOM no expone identificadores estables.

La sesión Cl@ve la mantiene ``RedSaraSessionManager``; aquí sólo nos
aseguramos de que el perfil Firefox persistente tiene cookies frescas
antes de abrir el wizard.
"""
from __future__ import annotations

import asyncio
import logging
import os
import platform
import re
from pathlib import Path
from typing import Any, Optional

logger = logging.getLogger(__name__)

_WIZARD_URL = "https://reg.redsara.es/es/nuevo-registro"


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

    console.print(f"[bold]submit_request_reg[/] [{mode}] {task_id[:8]} — preparando")

    required = ("access_request_id", "destination", "solicitante", "request")
    missing = [k for k in required if not payload.get(k)]
    if missing:
        client.complete_task(task_id, success=False, error=f"missing_payload:{','.join(missing)}")
        return

    work_dir = _downloads_dir() / f"submit_request_reg_{task_id[:8]}"
    work_dir.mkdir(parents=True, exist_ok=True)

    client.progress_task(task_id, status="in_progress", note="Preparando sesión REG")

    try:
        result = asyncio.run(_drive(payload, console, client, task_id, mode, work_dir))
    except Exception as e:
        logger.exception("submit_request_reg pipeline crashed")
        try:
            from observability import capture_exception
            capture_exception(e, task_type="submit_request_reg", task_id=task_id, mode=mode)
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

    # Asegúrate de que el perfil Firefox tiene cookies frescas Cl@ve para reg.redsara.es.
    # RedSaraSessionManager se encarga de relanzar la autenticación si caducó.
    session_manager = RedSaraSessionManager(
        portal_url="https://reg.redsara.es",
        cookies_file=settings.cookies_file,
        auth_timeout=settings.auth_timeout_seconds,
        firefox_profile_dir=profile_dir,
        firefox_profile_master=settings.firefox_profile_master,
        force_headed=False,
    )
    await session_manager.get_valid_session()

    # Descarga el PDF de la solicitud que tendremos que adjuntar en el paso 3.
    pdf_path = work_dir / "solicitud.pdf"
    try:
        pdf_bytes = client.download_pdf(
            f"/solicitudes/nueva/realizar/redactar/{payload['access_request_id']}/descargar-pdf"
        )
        pdf_path.write_bytes(pdf_bytes)
        console.print(f"[dim]Solicitud PDF descargada ({len(pdf_bytes)} bytes)[/]")
    except Exception as e:
        raise RuntimeError(f"could_not_download_request_pdf:{e}") from e

    async with lock_for("redsara"), async_playwright() as p:
        firefox_user_prefs = {
            "security.osclientcerts.autoload": True,
            "security.default_personal_cert": "Select Automatically",
            "browser.sessionstore.resume_from_crash": False,
            "browser.startup.page": 0,
        }
        headless = not _is_headed_debug(settings)
        context = await p.firefox.launch_persistent_context(
            user_data_dir=str(profile_dir),
            headless=headless,
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

            client.progress_task(task_id, status="in_progress", note="Paso 2: Datos de solicitud")
            await _wait_step_active(page, 2)
            await _step2_solicitud(page, payload["destination"], payload["request"], console)
            await _click_button(page, "Siguiente")

            client.progress_task(task_id, status="in_progress", note="Paso 3: Documentación")
            await _wait_step_active(page, 3)
            await _step3_documentacion(page, pdf_path, console)
            await _click_button(page, "Siguiente")

            client.progress_task(task_id, status="in_progress", note="Paso 4: Firma")
            await _wait_step_active(page, 4)
            registry_number, justificante_path = await _step4_firma_y_justificante(
                page, context, work_dir, console
            )
        except Exception as e:
            await _capture_failure(page, work_dir, "filler_failed")
            if settings.debug:
                console.print(
                    f"[bold yellow]DEBUG=True: dejando navegador abierto. URL: {page.url}\nError: {e}\nCtrl+C para cerrar.[/]"
                )
                while True:
                    await asyncio.sleep(60)
            raise

        promote_to_master(profile_dir, settings.firefox_profile_master)

    # Subir justificante al backend vía webhook (source=redsara_rec).
    upload_summary = None
    if justificante_path and justificante_path.exists():
        try:
            upload_summary = await _upload_justificante(
                client,
                access_request_id=payload["access_request_id"],
                registry_number=registry_number,
                pdf_path=justificante_path,
                destination_name=payload["destination"].get("unit_name") or "",
                subject=payload["request"].get("title") or "",
            )
        except Exception:
            logger.exception("upload of justificante failed")

    return {
        "mode": mode,
        "externalId": registry_number,
        "sentAt": _utcnow_iso(),
        "registry_no": registry_number,
        "downloads": {"Justificante": str(justificante_path)} if justificante_path else {},
        "upload_summary": upload_summary,
    }


# ─────────────────────────────────────────────────────────────────────────
# Steps
# ─────────────────────────────────────────────────────────────────────────


async def _step1_solicitante(page, solicitante: dict, console) -> None:
    """Rellena el Paso 1.

    NIF + nombre + apellidos los pre-rellena Cl@ve. Aquí sólo rellenamos:
      - radio "Interesado"
      - dirección postal: tipo de vía, dirección, país (ES por defecto),
        provincia, población, CP
      - datos de contacto: teléfono, correo
      - checkboxes "Deseo recibir avisos en correo/SMS"
    """
    address = (solicitante or {}).get("address") or {}

    # "¿Cómo quieres actuar?" → Interesado (default suele estarlo, pero
    # forzamos por idempotencia).
    try:
        await _click_radio_by_label(page, "Interesado")
    except RuntimeError as e:
        console.print(f"[yellow]REG: radio 'Interesado' no encontrado: {e}[/]")

    # Tipo de vía: mat-select con etiqueta "Tipo de vía".
    if address.get("street_type"):
        # PideInfo persiste la etiqueta exacta del select del REG (Calle,
        # Avenida, Plaza, Camí…); la pasamos tal cual.
        await _mat_select_by_label(page, "Tipo de vía", address["street_type"])

    if address.get("line"):
        await _mat_input_by_label(page, "Dirección", address["line"])

    # País: por defecto España; sólo cambiamos si payload trae otro.
    country = address.get("country") or "ES"
    if country and country != "ES":
        await _mat_select_by_label(page, "País", country)

    if address.get("province"):
        await _mat_select_by_label(page, "Provincia", address["province"])

    if address.get("municipality"):
        # Población: select dependiente de provincia. La etiqueta ya viene
        # tal cual la muestra REG, así que un match exacto es suficiente.
        await _mat_select_by_label(page, "Población", address["municipality"])

    if address.get("postal_code"):
        await _mat_input_by_label(page, "Código Postal", address["postal_code"])

    if solicitante.get("phone"):
        await _mat_input_by_label(page, "Teléfono", solicitante["phone"])

    if solicitante.get("email"):
        await _mat_input_by_label(page, "Correo electrónico", solicitante["email"])

    # Checkboxes "Deseo recibir avisos…": marcamos los dos.
    await _check_checkboxes_matching(page, ["Deseo recibir avisos"])


async def _step2_solicitud(page, destination: dict, request: dict, console) -> None:
    """Rellena el Paso 2 (Datos de solicitud).

    Selecciona la Unidad de destino por código DIR3 a través del buscador
    que la propia UI de REG ofrece. Después rellena asunto, EXPONE y
    SOLICITA (textareas independientes).
    """
    unit_dir3 = destination.get("unit_dir3")
    unit_name = destination.get("unit_name") or ""
    if not unit_dir3:
        raise RuntimeError("missing_destination_unit_dir3")

    # ── Selección de Unidad de destino ─────────────────────────────────
    # REG abre un modal de búsqueda; lo identificamos por el botón
    # "Seleccionar unidad" o por el icono de lupa junto al campo
    # "Unidad de destino". Discovery pendiente: aquí asumimos que el
    # buscador acepta el código DIR3 y devuelve un único resultado.
    try:
        await page.get_by_role("button", name=re.compile("Unidad de destino|Seleccionar unidad|Buscar", re.I)).first.click(timeout=5_000)
    except Exception:
        # Fallback: a veces el campo es un input visible donde podemos
        # teclear directamente.
        await _mat_input_by_label(page, "Unidad de destino", unit_dir3)

    # En el modal, escribir el DIR3 y elegir la fila correspondiente.
    try:
        search_box = page.get_by_role("textbox").last
        await search_box.fill(unit_dir3, timeout=3_000)
        # Esperar a que cargue el listado; pulsar la fila cuyo texto contiene el DIR3.
        await page.wait_for_timeout(900)
        row = page.locator(f"text=/{re.escape(unit_dir3)}/").first
        await row.click(timeout=5_000)
        # Confirmar la selección — botón "Aceptar" / "Seleccionar".
        for label in ("Seleccionar", "Aceptar", "Confirmar"):
            try:
                await page.get_by_role("button", name=re.compile(label, re.I)).click(timeout=1_500)
                break
            except Exception:
                continue
    except Exception as e:
        console.print(
            f"[yellow]REG: no se pudo seleccionar Unidad por DIR3 ({unit_dir3}); "
            f"probando con el nombre {unit_name!r}: {e}[/]"
        )
        # Fallback por nombre.
        await _mat_input_by_label(page, "Unidad de destino", unit_name)

    # ── Asunto, EXPONE, SOLICITA ───────────────────────────────────────
    if request.get("title"):
        await _mat_input_by_label(page, "Asunto", request["title"][:255])

    expone = (request.get("expone") or "")[:4000]
    solicita = (request.get("solicita") or "")[:4000]
    if expone:
        await _mat_input_by_label(page, "Expone", expone)
    if solicita:
        await _mat_input_by_label(page, "Solicita", solicita)


async def _step3_documentacion(page, pdf_path: Path, console) -> None:
    """Adjunta el PDF de la solicitud en el paso 3."""
    file_input = page.locator('input[type="file"]').first
    await file_input.wait_for(state="attached", timeout=10_000)
    await file_input.set_input_files(str(pdf_path))
    # Espera a que la UI confirme la subida (típicamente aparece el nombre).
    await page.wait_for_timeout(1_500)


async def _step4_firma_y_justificante(
    page,
    context,
    work_dir: Path,
    console,
) -> tuple[Optional[str], Optional[Path]]:
    """Firma con Cl@ve y captura el REGAGE + justificante PDF.

    El paso 4 dispara la pasarela de firma de Cl@ve (componente AutoFirma
    o redirección a `pasarela-ident.clave.gob.es`). Si el certificado
    FNMT está pre-cargado en el perfil de Firefox, el bounce es silencioso.
    Al volver, REG muestra la página de confirmación con el número
    REGAGE… y un botón "Descargar justificante".
    """
    await _click_button(page, "Firmar")

    # Esperamos a que (a) salgamos del paso 4 o (b) volvamos del bounce
    # de Cl@ve. El timeout es generoso porque la firma puede tardar.
    try:
        await page.wait_for_url(
            lambda url: ("clave.gob.es" in url) or ("/nuevo-registro" not in url and "/confirmacion" in url) or _regage_in_url(url),
            timeout=180_000,
        )
    except Exception as e:
        raise RuntimeError(f"firma_no_progreso: {page.url}") from e

    if "clave.gob.es" in page.url:
        console.print("[dim]REG: pasarela Cl@ve detectada para firma[/]")
        for sel in (
            'button[onclick*="AFIRMA"]', 'button[onclick*="afirma"]',
            'a[href*="AFIRMA"]', 'button:has-text("DNIe")',
            'button:has-text("Certificado electrónico")',
        ):
            try:
                btn = page.locator(sel).first
                await btn.wait_for(state="visible", timeout=5_000)
                await btn.click()
                console.print(f"[dim]REG: AFIRMA clic via {sel!r}[/]")
                break
            except Exception:
                continue
        try:
            await page.wait_for_url("**reg.redsara.es/**", timeout=180_000)
        except Exception as e:
            from auth.session_manager import SessionExpiredError
            raise SessionExpiredError(
                f"REG: timeout esperando vuelta de Cl@ve tras la firma ({page.url})"
            ) from e

    # Recuperar número REGAGE de la página de confirmación.
    registry_number: Optional[str] = None
    try:
        body_text = await page.locator("body").inner_text(timeout=5_000)
        m = re.search(r"(REGAGE\d{2}[a-z0-9]+)", body_text, re.IGNORECASE)
        if m:
            registry_number = m.group(1)
    except Exception:
        pass

    if not registry_number:
        # Algunos REG ponen el número en la propia URL como query param.
        m = re.search(r"REGAGE\d{2}[a-z0-9]+", page.url, re.IGNORECASE)
        if m:
            registry_number = m.group(0)

    # Descarga del justificante. REG suele ofrecer un botón "Descargar
    # justificante" que devuelve un PDF base64 (mismo endpoint API que el
    # scraper). Por simplicidad usamos el flujo nativo de Playwright.
    justificante_path: Optional[Path] = None
    try:
        async with page.expect_download(timeout=30_000) as download_info:
            await page.get_by_role("button", name=re.compile("Descargar justificante|Justificante", re.I)).first.click()
        download = await download_info.value
        target = work_dir / f"justificante_{registry_number or 'reg'}.pdf"
        await download.save_as(target)
        justificante_path = target
        console.print(f"[dim]→ Justificante: {target} ({target.stat().st_size} bytes)[/]")
    except Exception as e:
        console.print(f"[yellow]REG: no se pudo descargar justificante automáticamente ({e})[/]")

    return registry_number, justificante_path


# ─────────────────────────────────────────────────────────────────────────
# Webhook upload
# ─────────────────────────────────────────────────────────────────────────


async def _upload_justificante(
    client,
    *,
    access_request_id: str,
    registry_number: Optional[str],
    pdf_path: Path,
    destination_name: str,
    subject: str,
) -> dict:
    import base64
    import hashlib

    import httpx

    content = pdf_path.read_bytes()
    payload = {
        "source": "redsara_rec",
        "expedienteRef": registry_number or "",
        "documents": [
            {
                "filename": f"Justificante - {registry_number or 'REG'}.pdf",
                "contentType": "application/pdf",
                "content": base64.b64encode(content).decode("ascii"),
                "contentHash": hashlib.sha256(content).hexdigest(),
            }
        ],
        "metadata": {
            "access_request_id": access_request_id,
            "registryNumber": registry_number,
            "destinyOrganism": destination_name,
            "subject": subject,
        },
    }

    async with httpx.AsyncClient(timeout=120) as http:
        r = await http.post(
            client._webhook_url,
            json=payload,
            headers={**client._auth_headers, "Content-Type": "application/json"},
        )
        r.raise_for_status()
        return r.json()


# ─────────────────────────────────────────────────────────────────────────
# DOM helpers — REG uses Angular Material (mat-* components)
# ─────────────────────────────────────────────────────────────────────────


async def _wait_wizard_ready(page) -> None:
    """Espera a que el SPA Angular del REG haya pintado el primer mat-form-field."""
    await page.wait_for_selector("mat-form-field, mat-radio-group", timeout=30_000)


async def _wait_step_active(page, step: int) -> None:
    """Espera a que el paso indicado quede activo (heurística: hay un h1/h2
    que contiene "Paso {n} de 4")."""
    needle = re.compile(rf"Paso\s*{step}\s*de\s*4", re.I)
    try:
        await page.locator(f"text={needle.pattern}").first.wait_for(state="visible", timeout=20_000)
    except Exception:
        # No siempre el texto está visible; en ese caso confiamos en que
        # tras el Siguiente anterior el DOM se ha renovado lo bastante
        # como para tener nuevos mat-form-field.
        await page.wait_for_timeout(800)


async def _click_button(page, label: str) -> None:
    """Pulsa el botón con el texto indicado (case-insensitive). Usa el
    primer botón visible que coincide."""
    pattern = re.compile(re.escape(label), re.I)
    try:
        await page.get_by_role("button", name=pattern).first.click(timeout=10_000)
    except Exception:
        # Fallback: locator por texto.
        await page.locator(f"button:has-text('{label}')").first.click(timeout=10_000)


async def _click_radio_by_label(page, label: str) -> None:
    """Tilda un mat-radio-button por el texto visible."""
    pattern = re.compile(re.escape(label), re.I)
    try:
        await page.get_by_role("radio", name=pattern).first.check(timeout=5_000)
    except Exception:
        # Fallback: click directo sobre el contenedor.
        node = page.locator(f"mat-radio-button:has-text('{label}')").first
        await node.click(timeout=5_000)


async def _mat_input_by_label(page, label: str, value: str) -> None:
    """Rellena el input/textarea de un ``mat-form-field`` localizado por su label."""
    pattern = re.compile(re.escape(label), re.I)
    locator = page.get_by_label(pattern)
    await locator.first.fill(value, timeout=10_000)


async def _mat_select_by_label(page, label: str, value: str) -> None:
    """Abre un mat-select por su label y elige la opción exacta (texto)."""
    pattern = re.compile(re.escape(label), re.I)
    try:
        await page.get_by_label(pattern).first.click(timeout=5_000)
        await page.get_by_role("option", name=re.compile(rf"^{re.escape(value)}$", re.I)).first.click(timeout=5_000)
    except Exception:
        # Fallback más laxo por inclusión de texto.
        await _mat_select_contains(page, label, value)


async def _mat_select_contains(page, label: str, needle: str) -> None:
    """Variante de ``_mat_select_by_label`` que admite coincidencias parciales."""
    pattern = re.compile(re.escape(label), re.I)
    await page.get_by_label(pattern).first.click(timeout=5_000)
    await page.get_by_role("option", name=re.compile(re.escape(needle), re.I)).first.click(timeout=8_000)


async def _check_checkboxes_matching(page, label_substrings: list[str]) -> None:
    """Marca todos los mat-checkbox cuyo label contenga alguno de los substrings."""
    for substr in label_substrings:
        pattern = re.compile(re.escape(substr), re.I)
        for box in await page.get_by_role("checkbox", name=pattern).all():
            try:
                if not await box.is_checked():
                    await box.check(timeout=2_000)
            except Exception:
                continue


# ─────────────────────────────────────────────────────────────────────────
# Misc utilities
# ─────────────────────────────────────────────────────────────────────────


def _is_headed_debug(settings) -> bool:
    """Replica la condición que ``submit_request_transparencia`` usa para
    permitir abrir Firefox visible cuando ``HEADLESS_DISABLED=true``."""
    return bool(getattr(settings, "headless_disabled", False)) or os.environ.get("HEADLESS_DISABLED") == "true"


def _utcnow_iso() -> str:
    from datetime import datetime, timezone
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _regage_in_url(url: str) -> bool:
    return bool(re.search(r"REGAGE\d{2}[a-z0-9]+", url, re.IGNORECASE))


async def _capture_failure(page, work_dir: Path, label: str) -> None:
    """Guarda un screenshot + el HTML del momento para debugging."""
    try:
        await page.screenshot(path=str(work_dir / f"{label}.png"), full_page=True)
    except Exception:
        pass
    try:
        html = await page.content()
        (work_dir / f"{label}.html").write_text(html, encoding="utf-8")
    except Exception:
        pass
