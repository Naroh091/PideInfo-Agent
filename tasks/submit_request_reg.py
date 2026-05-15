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

    headed_debug = _is_headed_debug(settings)

    # Asegúrate de que el perfil Firefox tiene cookies frescas Cl@ve para reg.redsara.es.
    # RedSaraSessionManager se encarga de relanzar la autenticación si caducó.
    session_manager = RedSaraSessionManager(
        portal_url="https://reg.redsara.es",
        cookies_file=settings.cookies_file,
        auth_timeout=settings.auth_timeout_seconds,
        firefox_profile_dir=profile_dir,
        firefox_profile_master=settings.firefox_profile_master,
        headless=not headed_debug,
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
        headless = not headed_debug
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

    REG usa web components con Shadow DOM (``dnt-input``, ``dnt-select``,
    ``dnt-checkbox``, ``dnt-radio``); los inputs reales viven dentro del
    shadow root, así que ``page.fill()`` y ``get_by_label()`` no llegan.
    Inyectamos JS que perfora el shadow y hace los eventos que Angular
    necesita (``InputEvent`` con ``data`` por carácter).
    """
    address = (solicitante or {}).get("address") or {}
    payload = {
        "typeRepresented": "Interesado",
        "streetType": address.get("street_type") or "",
        "streetName": address.get("line") or "",
        "country": (address.get("country") or "ES"),
        "province": address.get("province") or "",
        "city": address.get("municipality") or "",
        "zipCode": address.get("postal_code") or "",
        "phone": solicitante.get("phone") or "",
        "email": solicitante.get("email") or "",
        "emailAlert": True,
    }
    failures = await page.evaluate(_FILL_STEP1_JS, payload)
    if failures:
        console.print(f"[yellow]REG paso 1: campos no rellenados: {failures}[/]")


# ─────────────────────────────────────────────────────────────────────────
# Shadow-DOM helpers (run inside the page via page.evaluate)
# ─────────────────────────────────────────────────────────────────────────

# Helpers adaptados de un bookmarklet probado. REG usa custom elements
# (``dnt-input``, ``dnt-select``, ``dnt-checkbox``, ``dnt-radio``) con
# Shadow DOM, así que para escribir hay que:
#   - perforar ``element.shadowRoot`` para llegar al input/textarea real
#   - despachar ``InputEvent`` con ``data`` por carácter (Angular escucha
#     ``valueChanges`` derivado de eso)
# Los autocompletes (``dnt-select#destinationOrganism``) son la excepción:
# requieren keystrokes REALES (vía ``page.keyboard.type``), no se disparan
# con InputEvent sintético.
_REG_HELPERS_JS = r"""
const delay = ms => new Promise(r => setTimeout(r, ms));
const visible = el => { const r = el.getBoundingClientRect(); return r.width > 0 && r.height > 0; };

function fillInput(formControlName, text) {
  const host = Array.from(document.querySelectorAll(
    `dnt-input[formcontrolname="${formControlName}"]`
  )).find(visible);
  if (!host) return false;
  const input = host.shadowRoot?.querySelector(
    'input.dnt-input__inner, textarea.dnt-textarea__inner'
  );
  if (!input) return false;
  input.focus();
  input.value = '';
  for (const ch of String(text)) {
    input.value += ch;
    input.dispatchEvent(new InputEvent('input', {
      data: ch, inputType: 'insertText', bubbles: true, composed: true,
    }));
  }
  input.dispatchEvent(new Event('change', { bubbles: true, composed: true }));
  input.dispatchEvent(new Event('blur', { bubbles: true, composed: true }));
  return true;
}

async function selectOption(idOrFcn, optionText) {
  const host = Array.from(document.querySelectorAll(
    `dnt-select#${idOrFcn},dnt-select[id="${idOrFcn}"],dnt-select[formcontrolname="${idOrFcn}"]`
  )).find(visible);
  if (!host) return false;
  host.click();
  await delay(300);
  for (const opt of host.querySelectorAll('dnt-option')) {
    const div = opt.shadowRoot?.querySelector('div[role="option"]');
    const text = (div?.textContent || opt.textContent || '').trim();
    if (text === optionText) {
      (div || opt).click();
      await delay(250);
      return true;
    }
  }
  document.body.click();
  return false;
}

function clickRadio(formControlName, value) {
  const group = document.querySelector(
    `dnt-radio-group[formcontrolname="${formControlName}"]`
  );
  if (!group) return false;
  const radio = Array.from(group.querySelectorAll('dnt-radio'))
    .find(r => (r.textContent || '').trim() === value);
  if (!radio) return false;
  const inner = radio.shadowRoot?.querySelector('input[type="radio"]');
  (inner || radio).click();
  return true;
}

function checkBox(formControlName) {
  const host = Array.from(document.querySelectorAll(
    `dnt-checkbox[formcontrolname="${formControlName}"]`
  )).find(visible);
  if (!host) return false;
  const inner = host.shadowRoot?.querySelector('input[type="checkbox"]');
  if (inner?.checked) return true;
  (inner || host).click();
  return true;
}
"""

_FILL_STEP1_JS = (
    "async (data) => {\n"
    + _REG_HELPERS_JS
    + r"""
  const failed = [];

  if (data.typeRepresented && !clickRadio('typeRepresented', data.typeRepresented)) {
    failed.push('typeRepresented');
  }
  await delay(200);

  if (data.streetType && !(await selectOption('streetType', data.streetType))) {
    failed.push('streetType');
  }
  if (data.streetName && !fillInput('streetName', data.streetName)) {
    failed.push('streetName');
  }
  if (data.country && data.country !== 'ES'
      && !(await selectOption('country', data.country))) {
    failed.push('country');
  }
  if (data.province && !(await selectOption('interested.province', data.province))) {
    failed.push('province');
  }
  // city depende de province — espera al cascade
  await delay(800);
  if (data.city && !(await selectOption('interested.city', data.city))) {
    failed.push('city');
  }
  if (data.zipCode && !fillInput('zipCode', data.zipCode)) failed.push('zipCode');
  if (data.phone && !fillInput('phone', data.phone)) failed.push('phone');
  if (data.email && !fillInput('email', data.email)) failed.push('email');
  if (data.emailAlert && !checkBox('emailAlert')) failed.push('emailAlert');

  return failed;
}
"""
)

_FILL_STEP2_TEXTS_JS = (
    "async (data) => {\n"
    + _REG_HELPERS_JS
    + r"""
  const failed = [];
  if (data.subject && !fillInput('subject', data.subject)) failed.push('subject');
  if (data.exposes && !fillInput('exposes', data.exposes)) failed.push('exposes');
  if (data.solicit && !fillInput('solicit', data.solicit)) failed.push('solicit');
  return failed;
}
"""
)

# Pulsa la primera dnt-option dentro del autocompletar de organismo cuyo
# texto empiece por el código DIR3 buscado.
_PICK_DESTINATION_JS = r"""
(dir3) => {
  const ds = document.querySelector('dnt-select#destinationOrganism');
  if (!ds) return 'no_select';
  for (const opt of ds.querySelectorAll('dnt-option')) {
    const div = opt.shadowRoot?.querySelector('div[role="option"]');
    const text = (div?.textContent || opt.textContent || '').trim();
    if (text.startsWith(dir3)) {
      (div || opt).click();
      return 'ok';
    }
  }
  return 'no_match';
}
"""


async def _step2_solicitud(page, destination: dict, request: dict, console) -> None:
    """Rellena el Paso 2 (Datos de solicitud).

    El select del organismo de destino es un autocomplete que solo se
    dispara con keystrokes reales (Angular escucha teclado, no el
    InputEvent sintético). Para los textos sí podemos usar el helper
    shadow-DOM porque exposes/solicit son ``<textarea>`` planos.
    """
    unit_dir3 = destination.get("unit_dir3")
    if not unit_dir3:
        raise RuntimeError("missing_destination_unit_dir3")

    # ── Selección de Unidad de destino vía autocompletar ──────────────
    # 1. Click en el dnt-select para abrir y enfocar el input interno.
    # 2. page.keyboard.type() escribe con eventos reales, lo que dispara
    #    la búsqueda XHR y carga las dnt-option.
    # 3. Click en la opción cuyo texto empieza por el DIR3.
    dest_select = page.locator("dnt-select#destinationOrganism")
    await dest_select.scroll_into_view_if_needed()
    await dest_select.click()
    await page.keyboard.type(unit_dir3, delay=40)
    # Espera a que la opción aparezca (XHR + render).
    try:
        await page.wait_for_function(
            "(dir3) => { const ds = document.querySelector('dnt-select#destinationOrganism'); "
            "return ds && Array.from(ds.querySelectorAll('dnt-option')).some(o => "
            "(o.shadowRoot?.querySelector('div[role=\"option\"]')?.textContent || o.textContent || '').trim().startsWith(dir3)); }",
            arg=unit_dir3,
            timeout=15_000,
        )
    except Exception as e:
        raise RuntimeError(f"destination_dir3_not_found:{unit_dir3}") from e
    pick_result = await page.evaluate(_PICK_DESTINATION_JS, unit_dir3)
    if pick_result != "ok":
        raise RuntimeError(f"destination_pick_failed:{pick_result}")

    # ── Asunto, EXPONE, SOLICITA ───────────────────────────────────────
    # Límites reales de REG: asunto 80 chars, expone/solicita 4000.
    payload = {
        "subject": (request.get("title") or "")[:80],
        "exposes": (request.get("expone") or "")[:4000],
        "solicit": (request.get("solicita") or "")[:4000],
    }
    failures = await page.evaluate(_FILL_STEP2_TEXTS_JS, payload)
    if failures:
        console.print(f"[yellow]REG paso 2: campos no rellenados: {failures}[/]")


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

    Pasos: (1) marcar el checkbox ``checkTerms``; (2) pulsar
    "Firmar y registrar (Cl@ve)" — un ``dnt-split-button`` cuyo botón
    principal lanza la pasarela Cl@ve. Si el certificado FNMT está
    pre-cargado en el perfil de Firefox, el bounce es silencioso y al
    volver REG muestra el REGAGE + "Descargar justificante".
    """
    # Marcar checkbox de confirmación antes de firmar (si no, el botón
    # de firmar queda deshabilitado).
    await page.evaluate(
        "() => { const cb = document.querySelector('dnt-checkbox[formcontrolname=\"checkTerms\"]'); "
        "const inner = cb?.shadowRoot?.querySelector('input[type=\"checkbox\"]'); "
        "if (inner && !inner.checked) (inner || cb).click(); }"
    )

    # Pulsar la parte principal del split-button (Cl@ve es la opción por
    # defecto). El chevron de la derecha abriría el dropdown con
    # "Firmar con certificado electrónico" — la diferenciamos por clase.
    clicked = await page.evaluate(
        "() => { const sb = document.querySelector('dnt-split-button'); "
        "const main = sb?.shadowRoot?.querySelector('.dnt-split-button__main-button'); "
        "if (!main) return false; main.click(); return true; }"
    )
    if not clicked:
        raise RuntimeError("firmar_button_not_found")

    # Esperamos a que (a) entremos en la pasarela Cl@ve o (b) lleguemos
    # ya a la página de detalle del registro. El timeout es generoso
    # porque la firma puede tardar mientras se elige el certificado.
    try:
        await page.wait_for_url(
            lambda url: (
                "clave.gob.es" in url
                or "/detalle-registro/" in url
                or _regage_in_url(url)
            ),
            timeout=180_000,
        )
    except Exception as e:
        raise RuntimeError(f"firma_no_progreso: {page.url}") from e

    if "clave.gob.es" in page.url:
        console.print("[dim]REG: pasarela Cl@ve detectada — eligiendo DNIe/Certificado[/]")
        # Preferimos siempre certificado electrónico (FNMT pre-cargado en
        # el perfil Firefox); si no, caemos a otros métodos disponibles.
        for sel in (
            'button:has-text("Acceso DNIe / Certificado electrónico")',
            'a:has-text("Acceso DNIe / Certificado electrónico")',
            'button:has-text("Certificado electrónico")',
            'button:has-text("DNIe")',
            'button[onclick*="AFIRMA"]', 'button[onclick*="afirma"]',
            'a[href*="AFIRMA"]',
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
            await page.wait_for_url("**reg.redsara.es/es/detalle-registro/**", timeout=180_000)
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

    # Descarga del justificante. REG expone un ``dnt-button`` con texto
    # "Descargar justificante" que dispara un GET autenticado al
    # ``reg-api.redsara.es/documents/uuid/{doc_uuid}`` y devuelve el PDF;
    # Playwright lo intercepta como un download nativo.
    justificante_path: Optional[Path] = None
    try:
        async with page.expect_download(timeout=30_000) as download_info:
            await _click_button(page, "Descargar justificante")
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
# DOM helpers — REG usa custom elements DNT (dnt-input, dnt-select, …)
# ─────────────────────────────────────────────────────────────────────────


async def _wait_wizard_ready(page) -> None:
    """Espera a que el SPA Angular pinte el primer ``dnt-input`` o ``dnt-radio-group``."""
    await page.wait_for_selector("dnt-input, dnt-radio-group", timeout=30_000)


async def _wait_step_active(page, step: int) -> None:
    """Espera a que el paso indicado quede activo (heurística: hay un h1/h2
    que contiene "Paso {n} de 4")."""
    needle = re.compile(rf"Paso\s*{step}\s*de\s*4", re.I)
    try:
        await page.locator(f"text={needle.pattern}").first.wait_for(state="visible", timeout=20_000)
    except Exception:
        # Si el texto no está visible, esperamos a que el DOM se renueve.
        await page.wait_for_timeout(800)


async def _click_button(page, label: str) -> None:
    """Pulsa el ``dnt-button`` (o ``<button>``) cuyo texto coincide.

    REG envuelve los botones de navegación en custom elements, así que
    ``page.get_by_role('button')`` no siempre los encuentra. Buscamos
    primero entre los ``dnt-button`` (light DOM) y, si no, caemos al
    selector estándar.
    """
    clicked = await page.evaluate(
        "(label) => { const norm = s => (s || '').trim().toLowerCase(); "
        "const target = norm(label); "
        "const visible = el => { const r = el.getBoundingClientRect(); return r.width > 0 && r.height > 0; }; "
        "const btn = Array.from(document.querySelectorAll('dnt-button')) "
        "  .find(b => visible(b) && norm(b.textContent) === target); "
        "if (btn) { btn.click(); return true; } "
        "return false; }",
        label,
    )
    if clicked:
        return
    pattern = re.compile(re.escape(label), re.I)
    try:
        await page.get_by_role("button", name=pattern).first.click(timeout=10_000)
    except Exception:
        await page.locator(f"button:has-text('{label}')").first.click(timeout=10_000)


# ─────────────────────────────────────────────────────────────────────────
# Misc utilities
# ─────────────────────────────────────────────────────────────────────────


def _is_headed_debug(settings) -> bool:
    from storage.preferences import is_headless_disabled
    return is_headless_disabled(settings)


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
