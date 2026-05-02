"""Stub handler for ``submit_request_reg`` AgentTask.

The Red SARA REG (``reg.redsara.es``) submission flow has not been mapped
yet. Until the discovery is done and a real driver lands, this handler
fails the task cleanly so the AccessRequest stays in ``pending`` with a
visible error instead of silently disappearing.

Tracked in app/docs/transparencia_age_submission.md (sucesores).
"""
from __future__ import annotations

import logging
from typing import Any

logger = logging.getLogger(__name__)


def handle(task: dict, client: Any) -> None:
    task_id = task["id"]
    payload = task.get("payload") or {}
    body = payload.get("public_body_name", "<organismo desconocido>")
    logger.info(
        "submit_request_reg: not implemented yet — task %s for %s left as failed",
        task_id, body,
    )
    client.complete_task(
        task_id,
        success=False,
        error="not_implemented:submit_request_reg",
        result={
            "mode": task.get("mode") or "auto",
            "public_body_name": body,
            "note": "El envío automático a REG aún no está implementado en el agente. La solicitud sigue como 'pendiente' en PideInfo; presenta manualmente o espera a la siguiente entrega.",
        },
    )
