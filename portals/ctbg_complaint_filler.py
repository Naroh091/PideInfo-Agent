"""Drive the CTBG complaint Wicket wizard from step 1 (Identificación) all
the way through step 6 (Acuse de recibo), downloading the receipt and the
signed instance.

Spec reference: docs/CTBG_RECLAMACIONES.md (mapped via MCP on 2026-05-01).
"""
from __future__ import annotations

import re
from pathlib import Path
from typing import Optional

from tasks._submission import UncertainSubmission

from playwright.async_api import (
    FrameLocator,
    Page,
    TimeoutError as PlaywrightTimeoutError,
)
from rich.console import Console

console = Console()


# Requisito de validez (paso 1 del modal de subida)
VALIDITY_ORIGINAL = "EE01"          # documento original electrónico (PDFs PideInfo)
VALIDITY_AUTHENTIC_COPY = "EE03"    # copia auténtica
VALIDITY_SIMPLE_COPY = "EEG02"      # copia simple

# Forma de aportación
ORIGIN_BRING_MYSELF = "I_DECIDE_TO_BRING_IT_MYSELF"
ORIGIN_PRESENTED_ELSEWHERE = "WAS_PRESENTED_IN_ANOTHER_ADMINISTRATION"

# Map AccessRequest.resolutionResult → opción literal del desplegable
# "RAZONES DE LA RECLAMACIÓN" del CTBG. Espejo de
# `App\Controller\ComplaintController::mapStatusToCtbg()` en el backend.
# El backend ya envía la cadena en `payload.complaint_reason`, pero
# preferimos derivarla del valor tipado `payload.resolution_result` para que
# la fuente de verdad sea una constante y no la cadena en español que el
# portal podría retocar — `complaint_reason` se usa sólo como fallback para
# tasks encolados antes de que el backend incluyera `resolution_result`.
RESOLUTION_RESULT_TO_CTBG_REASON: dict[str, str] = {
    "inadmitted":        "No se admitió a trámite la solicitud formulada",
    "denied":            "Se denegó el acceso a toda información solicitada",
    "partially_granted": "Se denegó el acceso a parte de la información solicitada",
    "granted":           "Estoy disconforme con la información recibida",
    # `silence` / None se gestionan en la rama branch=no (sin razón explícita).
}


class CtbgFillerError(RuntimeError):
    """Raised when the filler cannot complete a step."""


class CtbgComplaintFiller:
    """Fill the CTBG complaint form for one task.

    `payload` keys (from `ComplaintController::presentViaAgent`):
        public_body_name,
        resolution_result ('granted'|'partially_granted'|'denied'|'inadmitted'
            |'silence'|None — tipo de respuesta de la administración; fuente
            canónica para elegir branch y razón en el CTBG),
        complaint_branch ('yes'|'no' — pre-derivado por el backend; fallback
            si no llega `resolution_result`),
        complaint_reason (4 literales o None — pre-derivado por el backend;
            fallback si no llega `resolution_result`),
        notification_date (dd/mm/yyyy or None),
        complaint_body (full text), request_external_id,
        autonomous_local_entity (value del desplegable "Comunidad Autónoma" del
            formulario regional del CTBG, p.ej. 'principado_asturias'; None o
            ausente en el trámite estatal — su presencia activa el paso CCAA
            del paso 2 y solo toma uno de los 7 values soportados por el portal)

    `files`:
        reclamacion: Path  – PDF of the complaint argument (always)
        solicitud:   Path  – original request PDF (always)
        respuesta:   Path|None – agency's resolution PDF (branch=yes)
        notificacion: Path|None – agency's notification PDF (branch=yes)
    """

    def __init__(
        self,
        page: Page,
        payload: dict,
        files: dict[str, Optional[Path]],
        download_dir: Optional[Path] = None,
    ):
        self.page = page
        self.payload = payload
        self.files = files
        self.download_dir = download_dir

    def _resolve_branch(self) -> str:
        """Branch del CTBG ('yes' = recibí respuesta, 'no' = silencio).

        Preferimos el valor tipado `resolution_result`: cualquier resultado
        explícito de la administración (granted/partially_granted/denied/
        inadmitted) implica branch='yes'; `silence` o ausencia de resultado
        implican branch='no'. Sólo recurrimos al `complaint_branch` que envía
        el backend si el payload no incluye `resolution_result` (tasks
        encolados antes de añadirlo).
        """
        result = self.payload.get("resolution_result")
        if result is not None:
            return "no" if result == "silence" else "yes"
        return self.payload.get("complaint_branch") or "no"

    def _resolve_reason(self) -> Optional[str]:
        """Texto literal de la opción «RAZONES DE LA RECLAMACIÓN» del CTBG.

        Derivamos del valor tipado `resolution_result` cuando lo tenemos
        (fuente de verdad del tipo de respuesta administrativa); usamos
        `complaint_reason` sólo como fallback. Devuelve None en silencio o
        cuando no hay datos suficientes — el caller decide si eso es un
        error según la rama.
        """
        result = self.payload.get("resolution_result")
        if result is not None:
            return RESOLUTION_RESULT_TO_CTBG_REASON.get(result)
        return self.payload.get("complaint_reason")

    async def run(self) -> dict:
        await self._step1_identification()
        await self._step2_form()
        await self._step3_documents()
        await self._step4_declaro()
        await self._step5_sign()
        return await self._step6_acuse()

    # ------------------------------------------------------------------
    # Steps
    # ------------------------------------------------------------------

    async def _step1_identification(self) -> None:
        await self._wait_for_step("1")
        # "Para <NOMBRE> (NIF)" radio: name=representationMode, value=PERSONAL.
        # The native radio is visually hidden; the styled <label> intercepts
        # clicks. force=True bypasses Playwright's actionability gate so the
        # check goes straight to the underlying input.
        await self.page.locator(
            'input[type="radio"][name*="representationMode"]'
        ).first.check(force=True)
        await self._click_save_continue()

    async def _step2_form(self) -> None:
        await self._wait_for_step("2")
        # I. DATOS DE LA RECLAMACIÓN
        await self._wicket_write(
            label_re=re.compile(r"ENTIDAD RECLAMADA", re.I),
            value=self.payload["public_body_name"] or "",
        )

        # Vía autonómica/local: el formulario regional añade un desplegable
        # "Comunidad Autónoma" (obligatorio) que no existe en el estatal. El
        # backend manda el value del portal en `autonomous_local_entity` solo
        # para ese trámite; en el estatal la clave es None/ausente → se omite.
        # Seleccionamos por value (enum estable del portal: principado_asturias,
        # cantabria, …), no por la etiqueta visible (sensible a idioma/acentos).
        ccaa_value = self.payload.get("autonomous_local_entity")
        if ccaa_value:
            await self._wicket_select(
                label_re=re.compile(r"Comunidad Aut.noma", re.I),
                option_value=ccaa_value,
            )

        # Nº de expediente. La etiqueta varía entre trámites:
        #   estatal:  "(Indique el) Nº expediente del Portal de Transparencia"
        #             — obligatorio.
        #   regional: "En su caso, Nº expediente de la solicitud que origina
        #             esta reclamación" — opcional.
        # Matcheamos la parte invariante ("Nº expediente") y solo lo rellenamos
        # si tenemos identificador: en silencio autonómico/local puede no haber
        # expediente y el campo es opcional, así que no forzamos su escritura.
        external_id = self.payload.get("request_external_id")
        if external_id:
            await self._wicket_write(
                label_re=re.compile(r"N.{0,3}\s*expediente", re.I),
                value=external_id,
            )

        # B. RESPUESTA A SU SOLICITUD — derivar branch y razón del valor tipado
        # `resolution_result` (preferente), con fallback a los campos
        # pre-derivados por el backend.
        branch = self._resolve_branch()
        reason = self._resolve_reason()
        b_option = (
            "Sí he recibido respuesta a la solicitud" if branch == "yes"
            else "No he recibido respuesta a la solicitud"
        )
        await self._wicket_select(
            label_re=re.compile(r"Se.ale la opci.n correspondiente", re.I),
            option_text=b_option,
        )

        if branch == "yes":
            # B.1 — fecha + razones
            await self._wicket_set_date(
                label_re=re.compile(r"Fecha de notificaci", re.I),
                dmy=self.payload["notification_date"] or "",
            )
            if not reason:
                raise CtbgFillerError(
                    "Falta la razón de la reclamación: ni `resolution_result` "
                    "ni `complaint_reason` permiten derivarla."
                )
            await self._wicket_select(
                label_re=re.compile(r"RAZONES DE LA RECLAMACI", re.I),
                option_text=reason,
            )

        # Motivos (común a ambas ramas)
        await self._wicket_write(
            label_re=re.compile(r"Exponga brevemente los motivos", re.I),
            value=self.payload["complaint_body"] or "",
        )

        await self._click_save_continue()

    async def _step3_documents(self) -> None:
        await self._wait_for_step("3")

        is_regional = bool(self.payload.get("autonomous_local_entity"))
        branch = self._resolve_branch()

        if is_regional:
            # Formulario autonómico/local: siempre 1 sola card obligatoria
            # ("Solicitud de información"), independientemente de la rama.
            # El formulario regional NO pide resolución/notificación como docs
            # obligatorios — solo la solicitud original. Si disponemos de la
            # respuesta/notificación las subimos como documentación adicional.
            await self._attach_required_doc(0, self.files["solicitud"])
            for label, key in [
                ("Resolución / respuesta de la administración", "respuesta"),
                ("Notificación de la resolución", "notificacion"),
            ]:
                if self.files.get(key):
                    await self._add_additional_doc(self.files[key], description=label)
        else:
            # Formulario estatal: 3 cards obligatorias si hay respuesta, 1 si silencio.
            if branch == "yes":
                # Card 0: Resolución frente a la que se reclama
                await self._attach_required_doc(0, self.files["respuesta"])
                # Card 1: Notificación de la resolución
                await self._attach_required_doc(1, self.files["notificacion"])
                # Card 2: Solicitud de información
                await self._attach_required_doc(2, self.files["solicitud"])
            else:
                # Single card: Solicitud de información
                await self._attach_required_doc(0, self.files["solicitud"])

        # Adjuntamos también el PDF de la reclamación argumentada como
        # documentación adicional. Su texto ya va en el campo "Exponga
        # brevemente los motivos" del paso 2, pero el PDF conserva el
        # formato (negritas, párrafos, citas) que ese textarea no admite.
        if self.files.get("reclamacion"):
            await self._add_additional_doc(
                self.files["reclamacion"],
                description="Reclamación argumentada",
            )

        await self._click_save_continue()

    async def _step4_declaro(self) -> None:
        await self._wait_for_step("4")
        await self.page.locator(
            'input[type="checkbox"][name="declarationList:0:check"]'
        ).check(force=True)
        await self._click_save_continue()
        await self._wait_for_step("5")  # parked on Firmar

    async def _step5_sign(self) -> None:
        """Confirm the truth declaration and submit the form to register it.

        El paso 5 NO usa AutoFirma ni cliente PKI: la firma es la propia
        identidad Cl@ve con la que se entró al wizard. El submit "Firmar" es
        un POST Wicket normal que avanza al paso 6.
        """
        await self._wait_for_step("5")
        await self.page.locator(
            'input[type="checkbox"][name="viewFolderAdmissible:declareThatItsTrue"]'
        ).check(force=True)
        await self.page.locator(
            'button[name="viewFolderAdmissible:confirm"]'
        ).click()
        # ── PUNTO DE NO RETORNO: la firma CTBG se ha enviado ──
        try:
            await self.page.wait_for_load_state("domcontentloaded")
            await self._wait_for_step("6")
        except UncertainSubmission:
            raise
        except Exception as e:
            raise UncertainSubmission(
                f"ctbg_firma_sin_confirmar: {e}",
                markers={},
            ) from e

    async def _step6_acuse(self) -> dict:
        """Extract the registry number/CSV and download the two PDFs.

        Returns ``{registry_no, csv, downloads: {recibo: Path, instancia: Path}}``.
        """
        await self._wait_for_step("6")
        body_text = await self.page.locator("body").inner_text()

        registry_re = re.compile(r"\b(\d{4}-E-RE-\d+)\b")
        csv_re = re.compile(r"CSV:\s*([A-Z0-9]+)")

        registry_no: Optional[str] = None
        m = registry_re.search(body_text)
        if m:
            registry_no = m.group(1)

        csv: Optional[str] = None
        m = csv_re.search(body_text)
        if m:
            csv = m.group(1)

        downloads: dict[str, Path] = {}
        target_dir = self.download_dir or Path.cwd()
        target_dir.mkdir(parents=True, exist_ok=True)

        # The two "Descargar Recibo" / "Descargar Instancia firmada" buttons
        # trigger Wicket form posts that stream a PDF back. Use Playwright's
        # download capture to grab them deterministically.
        for label, slug in (
            ("Descargar Recibo", "recibo"),
            ("Descargar Instancia firmada", "instancia_firmada"),
        ):
            try:
                async with self.page.expect_download(timeout=60_000) as dl_info:
                    await self.page.locator(f'button:has-text("{label}")').first.click()
                d = await dl_info.value
                dest = target_dir / f"{slug}.pdf"
                await d.save_as(str(dest))
                downloads[slug] = dest
                # The acuse page may not render the registry number in HTML —
                # CTBG bakes it into the PDF filename ("Instancia firmada-
                # 2026-E-RE-2491.pdf"), so harvest it from there as a fallback.
                if not registry_no:
                    suggested = d.suggested_filename or ""
                    fm = registry_re.search(suggested)
                    if fm:
                        registry_no = fm.group(1)
                console.print(f"[green]CTBG: bajado {slug} → {dest}[/]")
            except PlaywrightTimeoutError as e:
                raise CtbgFillerError(
                    f"No se pudo descargar '{label}' del paso 6"
                ) from e

        return {
            "registry_no": registry_no,
            "csv": csv,
            "downloads": downloads,
        }

    # ------------------------------------------------------------------
    # Wicket helpers
    # ------------------------------------------------------------------

    async def _wait_for_step(self, number: str) -> None:
        """Wait until the current step heading shows `<n>. ...`."""
        try:
            await self.page.locator(f'h2:has-text("{number}. ")').first.wait_for(
                state="visible", timeout=30_000,
            )
        except PlaywrightTimeoutError as e:
            raise CtbgFillerError(f"Paso {number} no apareció a tiempo") from e

    async def _click_save_continue(self) -> None:
        """Click 'Guardar y continuar' and wait for the page to advance."""
        await self.page.locator('button:has-text("Guardar y continuar")').first.click()
        # Wicket re-renders fully; give it a beat before the next assertion.
        await self.page.wait_for_load_state("domcontentloaded")
        await self.page.wait_for_timeout(800)

    async def _wait_wicket_idle(self) -> None:
        """Wait until no inline edit panel is open. Signals the previous
        `Aceptar` click's Ajax round-trip has finished and the form has been
        re-rendered, so subsequent field lookups see the new DOM."""
        try:
            await self.page.locator('[name*=":modal:"]').first.wait_for(
                state="detached", timeout=15_000,
            )
        except PlaywrightTimeoutError:
            # Either there was never a panel, or the page just timed out.
            # Don't fail here — the caller's locator will raise a more
            # specific error if the page really is broken.
            pass

    async def _open_wicket_field(self, label_re: re.Pattern[str]) -> None:
        """Click the 'Escribir' / 'Seleccionar' anchor whose surrounding label
        matches *label_re*.

        Each field on the Wicket form is structured as::

            <p>
              "A. ENTIDAD RECLAMADA"   ← text node, not an element
              <br>
              <span class="outerForm table-form">
                <a class="inputTextDiv personalizedField">Escribir</a>
              </span>
            </p>

        The label sits as a text node inside the `<p>` *grandparent* of the
        anchor — `el.parentElement` is the wrapping `<span>`, whose innerText
        is just "Escribir".

        We reconstruct the label as the text that appears **before** the anchor
        within its nearest block (a Range from block-start to the anchor). This
        is field-specific: when several fields share one `<p>` (the autonómico/
        local form packs ENTIDAD + "Comunidad Autónoma" + Nº expediente into a
        single `<p>`), the plain ancestor innerText bleeds a field's label into
        its siblings, which would make `/Comunidad Autónoma/` also match the
        ENTIDAD anchor. Preceding-only text can't bleed *backwards*, so matching
        the first anchor in DOM order whose preceding text hits the regex always
        lands on the field right after that label. Falls back to the old
        ancestor walk when there is no preceding text.

        Retries up to 3 times because Wicket can finish rendering after our
        idle wait completes.
        """
        await self._wait_wicket_idle()
        last_labels: list[str] = []
        for _attempt in range(3):
            anchors = self.page.locator("a.inputTextDiv.personalizedField")
            count = await anchors.count()
            last_labels = []
            for i in range(count):
                a = anchors.nth(i)
                label = await a.evaluate(
                    """el => {
                        const link = (el.innerText || '').trim();
                        // Preferred: text preceding this anchor within its
                        // nearest block — field-specific, can't inherit a
                        // sibling field's label that sits *after* it.
                        const block = el.closest('p, li, td, dd, div');
                        if (block) {
                            try {
                                const range = document.createRange();
                                range.setStart(block, 0);
                                range.setEndBefore(el);
                                const pre = range.toString()
                                    .replace(/\\s+/g, ' ').trim();
                                if (pre.length > 0) return pre;
                            } catch (e) { /* fall through */ }
                        }
                        // Fallback: walk ancestors, stripping the link's text.
                        let cur = el.parentElement;
                        for (let depth = 0; depth < 4 && cur; depth++) {
                            const own = (cur.innerText || '').replace(/\\s+/g, ' ').trim();
                            const stripped = link
                                ? own.split(link).join(' ').replace(/\\s+/g, ' ').trim()
                                : own;
                            if (stripped.length > 0) return stripped;
                            cur = cur.parentElement;
                        }
                        return '';
                    }"""
                )
                last_labels.append(label or "")
                text = (await a.inner_text()).strip()
                if label_re.search(label or "") and text in {"Escribir", "Seleccionar"}:
                    await a.click()
                    return
            await self.page.wait_for_timeout(400)
        sample = " | ".join(repr(l)[:90] for l in last_labels[:8])
        raise CtbgFillerError(
            f"No encontré campo para {label_re.pattern}. "
            f"Anchors visibles ({len(last_labels)}): {sample}"
        )

    async def _accept_modal(self) -> None:
        """Click the inline panel's 'Aceptar' and wait until the panel is gone.

        Waiting on the `:modal:` input being detached is deterministic — a
        fixed `wait_for_timeout` raced Wicket's Ajax round-trip and was the
        root cause of the field-not-found error David hit in production."""
        await self.page.locator('button:has-text("Aceptar")').first.click()
        await self._wait_wicket_idle()

    async def _wicket_write(self, label_re: re.Pattern[str], value: str) -> None:
        await self._open_wicket_field(label_re)
        # The freshly-opened panel always contains exactly one editable element.
        ta = self.page.locator('textarea, input[type="text"]').filter(
            has_not=self.page.locator('[disabled]'),
        )
        # Pick the visible one whose name contains ":modal:" (our inline panel).
        candidates = self.page.locator('textarea[name*=":modal:"], input[type="text"][name*=":modal:"]')
        await candidates.first.fill(value)
        await self._accept_modal()

    async def _wicket_select(
        self,
        label_re: re.Pattern[str],
        option_text: Optional[str] = None,
        option_value: Optional[str] = None,
    ) -> None:
        """Select an option in a Wicket inline `<select>`.

        Match the option either by its visible text (`option_text`) or by its
        `value` attribute (`option_value`). Prefer `option_value` for fields
        whose values are a stable portal-defined enum (e.g. the autonómico/local
        "Comunidad Autónoma" dropdown: `principado_asturias`, …), since the
        visible labels are locale-sensitive; use `option_text` when the backend
        already carries the literal label (e.g. RAZONES DE LA RECLAMACIÓN).
        """
        if (option_text is None) == (option_value is None):
            raise CtbgFillerError(
                "_wicket_select requiere exactamente uno de option_text/option_value"
            )
        await self._open_wicket_field(label_re)
        sel = self.page.locator('select[name*=":select-field"]').first
        await sel.wait_for(state="attached", timeout=10_000)
        # Wicket may hide the native <select>; set the value by matching the
        # option via JS and dispatch change so the framework's listener fires.
        await sel.evaluate(
            """(el, {text, value}) => {
                const opt = Array.from(el.options).find(o =>
                    value != null ? o.value === value : o.text.trim() === text);
                if (!opt) throw new Error('option not found: ' + (value ?? text));
                el.value = opt.value;
                el.dispatchEvent(new Event('change', { bubbles: true }));
            }""",
            {"text": option_text, "value": option_value},
        )
        await self._accept_modal()

    async def _wicket_set_date(self, label_re: re.Pattern[str], dmy: str) -> None:
        await self._open_wicket_field(label_re)
        date_input = self.page.locator('input[name*=":field:date"]').first
        await date_input.fill(dmy)
        await self._accept_modal()

    # ------------------------------------------------------------------
    # Step 3 helpers (uploads)
    # ------------------------------------------------------------------

    async def _attach_required_doc(
        self,
        slot_index: int,
        file_path: Optional[Path],
        validity: str = VALIDITY_ORIGINAL,
    ) -> None:
        if file_path is None:
            raise CtbgFillerError(f"Slot obligatorio {slot_index} sin fichero")

        # Pick "Decido aportarlo yo mismo" on the slot's select. Wicket may
        # hide the native <select> behind a custom widget — set the value via
        # JS and dispatch change so its listener fires.
        select_locator = self.page.locator(
            f'select[name*="requiredDocumentationList:{slot_index}:"]'
        ).first
        await select_locator.wait_for(state="attached", timeout=10_000)
        await select_locator.evaluate(
            """(el, value) => {
                el.value = value;
                el.dispatchEvent(new Event('change', { bubbles: true }));
            }""",
            ORIGIN_BRING_MYSELF,
        )
        await self.page.wait_for_timeout(800)  # Wicket re-render

        # The "Adjuntar" button now exists for this slot. We can rely on its
        # name attribute carrying the same path index.
        cards = self.page.locator(".c-card-document")
        await cards.nth(slot_index).locator('button:has-text("Adjuntar")').click()

        await self._upload_via_modal(file_path, validity)

    async def _add_additional_doc(
        self,
        file_path: Path,
        description: str = "",
        validity: str = VALIDITY_ORIGINAL,
    ) -> None:
        """Click "Añadir documento adicional" and upload *file_path* via the modal.

        El formulario autonómico/local usa esta vía para respuesta/notificación
        (son opcionales — el CTBG los acepta como documentación adicional cuando
        los tenemos pero no los requiere como cards obligatorias).

        El modal de documentación adicional tiene el mismo flujo de dos pasos
        que el modal de cards obligatorias (validez → fichero). Si el portal
        regional omitiera el paso de validez, el localizador `button:has-text
        ("Siguiente")` no encontraría el botón y lanzará CtbgFillerError con un
        mensaje claro — en ese caso adaptar a un único paso.
        """
        await self.page.locator('button:has-text("Añadir documento adicional")').first.click()
        await self._upload_via_modal(file_path, validity, description=description)

    async def _upload_via_modal(self, file_path: Path, validity: str, description: str = "") -> None:
        """Drive the two-step "Cargar documento" iframe modal.

        Selectors are kept generic because Wicket assigns ids per render
        (`id17d` from the mapping session is not the same id at runtime).
        The modal contains exactly one select (Requisito de validez), one
        textarea (Descripción, prefilled) and one file input, so positional
        selectors are unambiguous.
        """
        iframe_selector = 'iframe[id^="_wicket_window_"]'
        # Wait for the modal iframe to actually appear (Wicket Ajax can take
        # 1–2 s) before trying to enter it.
        try:
            await self.page.locator(iframe_selector).wait_for(state="attached", timeout=15_000)
        except PlaywrightTimeoutError as e:
            raise CtbgFillerError(f"El modal de Cargar documento no apareció tras pulsar Adjuntar (subiendo {file_path.name})") from e

        iframe: FrameLocator = self.page.frame_locator(iframe_selector)

        # Step 1 of modal: pick "Requisito de validez". Wicket may wrap the
        # native <select> in a custom widget that hides it (display:none /
        # zero size), so Playwright's actionability checks fail. Set the value
        # via JS and dispatch a change event so Wicket's listener fires.
        console.print(f"[dim]CTBG: cargar {file_path.name} — step 1 (validez={validity})[/]")
        select_loc = iframe.locator("select").first
        await select_loc.wait_for(state="attached", timeout=10_000)
        await select_loc.evaluate(
            """(el, value) => {
                el.value = value;
                el.dispatchEvent(new Event('change', { bubbles: true }));
            }""",
            validity,
        )
        # Description is prefilled — leave as is.
        await iframe.locator('button:has-text("Siguiente")').first.click()
        await self.page.wait_for_timeout(1000)

        # Step 2 of modal: file input + Cargar.
        console.print(f"[dim]CTBG: cargar {file_path.name} — step 2 (set_input_files)[/]")
        file_input = iframe.locator('input[type="file"]').first
        await file_input.wait_for(state="attached", timeout=10_000)
        await file_input.set_input_files(str(file_path))
        await iframe.locator('button:has-text("Cargar")').first.click()

        # Wait for the iframe to close (modal removed from DOM).
        try:
            await self.page.locator(iframe_selector).first.wait_for(
                state="detached", timeout=60_000,
            )
            console.print(f"[green]CTBG: {file_path.name} subido[/]")
        except PlaywrightTimeoutError as e:
            raise CtbgFillerError(f"El modal de subida no cerró tras subir {file_path.name}") from e

        await self.page.wait_for_timeout(700)
