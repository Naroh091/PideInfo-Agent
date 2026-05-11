from __future__ import annotations

import subprocess
import sys

from rich.console import Console

console = Console()


def _notify(title: str, message: str) -> None:
    """Send a desktop notification. Uses osascript on macOS, falls back to console."""
    if sys.platform == "darwin":
        try:
            safe_title = title.replace('"', '\\"')
            safe_message = message.replace('"', '\\"')
            subprocess.run(
                [
                    "osascript",
                    "-e",
                    f'display notification "{safe_message}" with title "{safe_title}"',
                ],
                capture_output=True,
                timeout=5,
            )
            return
        except Exception:
            pass
    console.print(f"[bold]{title}[/]: {message}")


def notify_auth_required() -> None:
    _notify(
        "PideInfo Agent — Autenticación requerida",
        "Abre el navegador para autenticarte con tu certificado electrónico.",
    )


def notify_new_documents(count: int, expediente_ref: str = "", portal: str = "") -> None:
    msg = f"Se han sincronizado {count} documento(s) nuevo(s)"
    if expediente_ref:
        msg += f" del expediente {expediente_ref}"
    if portal:
        msg += f" ({portal})"
    _notify("PideInfo Agent — Nuevos documentos", msg)


def notify_pending_signatures(count: int, portal: str = "") -> None:
    portal_txt = f" en {portal}" if portal else " en el portal"
    _notify(
        "PideInfo Agent — Firmas pendientes",
        f"Hay {count} notificación(es) pendiente(s) de firma{portal_txt}.",
    )


def notify_error(message: str) -> None:
    _notify("PideInfo Agent — Error", message)


# ─────────────────────────────────────────────────────────────────────────
# Task lifecycle notifications (start / success / failure).
# ─────────────────────────────────────────────────────────────────────────

_TASK_LABELS = {
    "present_complaint": "Presentación de reclamación",
    "submit_request_transparencia": "Presentación de solicitud (Transparencia AGE)",
    "submit_request_reg": "Presentación de solicitud (REG)",
}


def _task_label(task_type: str) -> str:
    return _TASK_LABELS.get(task_type, task_type)


def _result_reference(result: dict | None) -> str:
    if not isinstance(result, dict):
        return ""
    for key in ("registry_no", "expedienteRef", "externalId", "idBorr", "idExp"):
        value = result.get(key)
        if value:
            return str(value)
    return ""


def notify_task_started(task_type: str) -> None:
    _notify(
        "PideInfo Agent — Tarea iniciada",
        f"{_task_label(task_type)}: en curso.",
    )


def notify_task_succeeded(task_type: str, result: dict | None = None) -> None:
    ref = _result_reference(result)
    msg = f"{_task_label(task_type)}: completada correctamente"
    if ref:
        msg += f" ({ref})"
    msg += "."
    _notify("PideInfo Agent — Tarea completada", msg)


def notify_task_failed(task_type: str, error: str | None = None) -> None:
    msg = f"{_task_label(task_type)}: ha fallado"
    if error:
        trimmed = error.strip()
        if len(trimmed) > 180:
            trimmed = trimmed[:177] + "…"
        msg += f" — {trimmed}"
    msg += "."
    _notify("PideInfo Agent — Tarea fallida", msg)
