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
            registry_number, registry_uuid = await _step4_firma(page, context, console)
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

    # Descargar justificante vía API directa (mismo camino que el sync
    # de fondo de Red SARA). Saltarse la UI evita el popup "URL
    # solicitada no existe" que aparece si el doc_uuid aún no está
    # vivo: aquí podemos reintentar limpiamente la llamada API.
    justificante_path: Optional[Path] = None
    if registry_uuid:
        client.progress_task(task_id, status="in_progress", note="Descargando justificante")
        justificante_path = await _download_justificante_via_api(
            session_manager, registry_uuid, registry_number, work_dir, console
        )

    # Subir justificante al backend vía webhook (source=redsara_rec).
    # Reusamos el método canónico del cliente — el sync de fondo de Red
    # SARA usa el mismo, así el backend procesa el justificante por la
    # misma ruta esté quien esté generándolo.
    upload_summary = None
    if justificante_path and justificante_path.exists():
        from models.redsara import RedSaraRegistro

        registro = RedSaraRegistro(
            registry_number=registry_number or "",
            registry_number_temporary="",
            status="Enviado",
            entry_date=_utcnow_iso(),
            destiny_organism=payload["destination"].get("unit_name") or "",
            subject=(payload["request"].get("title") or "")[:200],
            act_like="Interesado",
            uuid=registry_uuid or "",
            app_user="REG",
        )
        try:
            upload_summary = await client.sync_redsara_document(
                registro,
                justificante_path,
                access_request_id=payload["access_request_id"],
            )
        except Exception as e:
            logger.exception("upload of justificante failed")
            console.print(
                f"[bold yellow]REG {registry_number or ''}: justificante NO subido al backend → {e}. "
                f"PDF local: {justificante_path}[/]"
            )
    elif registry_number:
        console.print(
            f"[bold yellow]REG {registry_number}: justificante no descargado, nada que subir al backend.[/]"
        )

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

# Busca atómicamente la dnt-option cuyo *unidad principal* (el primer
# <span> con la clase dnt-txt-body-350) coincide con el DIR3 buscado y
# la clica. Importante:
#   - REG renderiza TODA la jerarquía (unidad + organismo intermedio +
#     organismo raíz) dentro de UNA sola dnt-option como <span>+<p>+<p>,
#     así que el textContent concatenado mezcla los tres DIR3. Por eso
#     comparamos contra el primer <span> (la unidad seleccionable), no
#     contra textContent completo, para no clicar accidentalmente la
#     opción cuando lo que matchea es el organismo padre.
#   - Sondeo interno con timeout: el XHR /dir3/search puede tardar y el
#     CDK overlay re-pinta la lista; combinar espera y click elimina la
#     ventana de carrera.
#   - En no_match devuelve las opciones realmente renderizadas para
#     diagnosticar (typo, organismo dado de baja, etc.).
_PICK_DESTINATION_JS = r"""
async ({ dir3, timeoutMs }) => {
  const target = String(dir3 || '').trim().toLowerCase();
  // El <span> con dnt-txt-body-350 es la unidad seleccionable; los <p>
  // con dnt-txt-body-200 son la cadena padres → raíz, no clicables como
  // entidad propia desde aquí.
  const unitText = opt => {
    const span = opt.querySelector('span.dnt-txt-body-350, span');
    return ((span?.textContent) || '').trim();
  };
  const findMatch = () => {
    const ds = document.querySelector('dnt-select#destinationOrganism');
    if (!ds) return { ds: null, opt: null };
    const opts = Array.from(ds.querySelectorAll('dnt-option'));
    let opt = opts.find(o => unitText(o).toLowerCase().startsWith(target));
    if (!opt) opt = opts.find(o => unitText(o).toLowerCase().includes(target));
    return { ds, opt };
  };
  const start = Date.now();
  while (Date.now() - start < timeoutMs) {
    const { ds, opt } = findMatch();
    if (opt) {
      const span = opt.querySelector('span.dnt-txt-body-350, span');
      (span || opt).click();
      return { status: 'ok', text: unitText(opt) };
    }
    if (!ds) return { status: 'no_select' };
    await new Promise(r => setTimeout(r, 200));
  }
  const ds = document.querySelector('dnt-select#destinationOrganism');
  const options = ds
    ? Array.from(ds.querySelectorAll('dnt-option')).map(o => unitText(o).slice(0, 120))
    : null;
  return { status: 'no_match', options };
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
    # El <dnt-select> es un wrapper plano (~21 px); el <input> combobox
    # real vive dentro del shadow de su <dnt-input>, ~36 px más abajo.
    # Clicar el host no foca el input → page.keyboard.type cae en body
    # y NUNCA se dispara el XHR /dir3/search. Hay que clicar el input
    # real con coordenadas reales para que reciba foco.
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
    await page.keyboard.type(unit_dir3, delay=80)
    # Esperar a que la opción aparezca y clicarla en una sola operación
    # atómica — el CDK overlay re-pinta la lista varias veces durante el
    # XHR de búsqueda, así que entre `wait_for_function` y el click puede
    # cambiar el DOM. El JS hace polling interno (timeout 15 s).
    pick_result = await page.evaluate(_PICK_DESTINATION_JS, {"dir3": unit_dir3, "timeoutMs": 15_000})
    status = (pick_result or {}).get("status")
    if status == "ok":
        console.print(f"[dim]REG paso 2: destino seleccionado → {pick_result.get('text')!r}[/]")
    else:
        opts = (pick_result or {}).get("options")
        if status == "no_match" and opts is not None:
            preview = ", ".join(opts[:5]) if opts else "(lista vacía)"
            raise RuntimeError(
                f"destination_pick_failed:no_match dir3={unit_dir3!r} options=[{preview}]"
            )
        raise RuntimeError(f"destination_pick_failed:{status or 'unknown'}")

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


async def _step4_firma(
    page,
    context,
    console,
) -> tuple[Optional[str], Optional[str]]:
    """Firma con Cl@ve y devuelve ``(REGAGE, registry_uuid)``.

    Pasos: (1) marcar el checkbox ``checkTerms``; (2) pulsar
    "Firmar y registrar (Cl@ve)" — un ``dnt-split-button`` cuyo botón
    principal lanza la pasarela Cl@ve. Si el certificado FNMT está
    pre-cargado en el perfil de Firefox, el bounce es silencioso y al
    volver REG nos deja en ``/es/detalle-registro/{uuid}``.

    El justificante NO se descarga desde la UI: el botón "Descargar
    justificante" apunta a una URL que tarda unos segundos en estar
    viva y, mientras tanto, abre un popup "La URL solicitada no
    existe". Sacamos el UUID del registro de la URL y descargamos por
    API directa (``reg-api.redsara.es/documents/uuid/…``) más tarde,
    fuera del flujo del browser.
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

    # UUID del registro: la URL tras la firma es
    # https://reg.redsara.es/es/detalle-registro/{uuid}[/...]
    registry_uuid: Optional[str] = None
    m = re.search(r"/detalle-registro/([0-9a-f-]{36})", page.url, re.IGNORECASE)
    if m:
        registry_uuid = m.group(1)
    else:
        console.print(
            f"[yellow]REG: no se pudo extraer registry_uuid de la URL ({page.url})[/]"
        )

    return registry_number, registry_uuid


# ─────────────────────────────────────────────────────────────────────────
# Justificante download (API directa, sin UI)
# ─────────────────────────────────────────────────────────────────────────


async def _download_justificante_via_api(
    session_manager,
    registry_uuid: str,
    registry_number: Optional[str],
    work_dir: Path,
    console,
) -> Optional[Path]:
    """Descarga el justificante por la API REST de Red SARA.

    Reusa ``RedSaraRecScraper.download_justificante`` (el mismo método
    que el sync de fondo). Reintenta si el documento aún no está
    indexado tras la firma — el backend SARA puede tardar varios
    segundos en exponer el PDF en /documents/uuid/{doc_uuid}.
    """
    from portals.redsara_rec import RedSaraRecScraper

    scraper = RedSaraRecScraper(session_manager)
    target = work_dir / f"justificante_{registry_number or 'reg'}.pdf"

    # 6 intentos × backoff 3/5/8/13/21/34 s ≈ 84 s totales
    delays = [3, 5, 8, 13, 21, 34]
    last_err: Optional[str] = None
    for attempt, delay in enumerate(delays, start=1):
        try:
            ok = await scraper.download_justificante(registry_uuid, target)
            if ok and target.exists() and target.stat().st_size > 0:
                console.print(
                    f"[dim]→ Justificante: {target} ({target.stat().st_size} bytes)[/]"
                )
                await scraper.close()
                return target
        except Exception as e:
            last_err = str(e)
        console.print(
            f"[yellow]REG: justificante aún no disponible (intento {attempt}/{len(delays)}), reintento en {delay}s[/]"
        )
        await asyncio.sleep(delay)

    await scraper.close()
    console.print(
        f"[bold yellow]REG: no se pudo descargar justificante tras {len(delays)} intentos"
        + (f" — último error: {last_err}" if last_err else "")
        + "[/]"
    )
    return None


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
