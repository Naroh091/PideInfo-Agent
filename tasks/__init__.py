"""Type-keyed dispatcher for agent tasks.

Each task type registers a handler with signature: handler(task: dict, client) -> None
The handler is responsible for the full lifecycle: progress → complete (or failed).
The CALLER is responsible for claiming the task before invoking the handler;
see dispatch_action_id() which combines claim + dispatch.
"""

from __future__ import annotations

import logging
from typing import Callable, Protocol

logger = logging.getLogger(__name__)


class _ClientProto(Protocol):
    base_url: str
    def claim_task(self, task_id: str) -> dict | None: ...
    def progress_task(self, task_id: str, status: str, note: str | None = ...) -> None: ...
    def complete_task(self, task_id: str, success: bool = ..., *, outcome: str | None = ..., result: dict | None = ..., error: str | None = ...) -> None: ...
    def download_pdf(self, path: str) -> bytes: ...


_HANDLERS: dict[str, Callable[[dict, _ClientProto], None]] = {}


def register(task_type: str, handler: Callable[[dict, _ClientProto], None]) -> None:
    _HANDLERS[task_type] = handler


class _NotifyingClient:
    """Proxy around the agent client that emits desktop notifications when
    the handler (or the dispatcher itself) calls ``complete_task``.

    Only the first ``complete_task`` for a given task triggers a notification
    — handlers may opportunistically retry on transient errors, but the user
    only cares about the terminal outcome.
    """

    def __init__(self, inner, task: dict) -> None:
        self._inner = inner
        self._task_id = task["id"]
        self._task_type = task["type"]
        self._notified = False

    def __getattr__(self, name):
        return getattr(self._inner, name)

    def complete_task(self, task_id, success=False, *, outcome=None, result=None, error=None):
        if not self._notified and task_id == self._task_id:
            self._notified = True
            try:
                from notifier.desktop import notify_task_succeeded, notify_task_failed
                # 'outcome' (done|failed|uncertain) tiene prioridad; si no se
                # da, se deriva del booleano 'success'. Solo 'done' es éxito;
                # 'uncertain' se notifica como no-éxito (la firma no se pudo
                # confirmar).
                succeeded = (outcome == "done") if outcome else success
                if succeeded:
                    notify_task_succeeded(self._task_type, result)
                else:
                    notify_task_failed(self._task_type, error)
            except Exception:
                logger.exception("notify on complete_task failed")
        return self._inner.complete_task(
            task_id, success, outcome=outcome, result=result, error=error
        )


def _notify_started(task: dict) -> None:
    try:
        from notifier.desktop import notify_task_started
        notify_task_started(task["type"])
    except Exception:
        logger.exception("notify on task start failed")


def dispatch_action_id(action: str, task_id: str, client: _ClientProto) -> None:
    """Resolve an action+task_id (from a pideinfo:// URL) to a task and dispatch it."""
    task = client.claim_task(task_id)
    if task is None:
        logger.info("Task %s already claimed; ignoring.", task_id)
        return
    _notify_started(task)
    notifying = _NotifyingClient(client, task)
    handler = _HANDLERS.get(task["type"])
    if handler is None:
        logger.error("No handler for task type %r (id=%s)", task["type"], task_id)
        notifying.complete_task(task_id, success=False, error=f"no_handler:{task['type']}")
        return
    try:
        handler(task, notifying)
    except Exception as e:
        logger.exception("Handler for %s crashed: %s", task["type"], e)
        _report_exception(e, task_type=task["type"], task_id=task_id)
        try:
            notifying.complete_task(task_id, success=False, error=f"handler_crashed:{e!s}"[:2000])
        except Exception:
            pass


def dispatch_existing(task: dict, client: _ClientProto) -> None:
    """Dispatch a task that was already claimed (e.g. discovered via pending poll)."""
    _notify_started(task)
    notifying = _NotifyingClient(client, task)
    handler = _HANDLERS.get(task["type"])
    if handler is None:
        notifying.complete_task(task["id"], success=False, error=f"no_handler:{task['type']}")
        return
    try:
        handler(task, notifying)
    except Exception as e:
        logger.exception("Handler for %s crashed: %s", task["type"], e)
        _report_exception(e, task_type=task["type"], task_id=task["id"])
        try:
            notifying.complete_task(task["id"], success=False, error=f"handler_crashed:{e!s}"[:2000])
        except Exception:
            pass


def _report_exception(exc: BaseException, **tags: str) -> None:
    """Forward to Sentry explicitly. LoggingIntegration usually does this on
    its own, but this guarantees the event leaves even if the logging
    hierarchy is misconfigured."""
    try:
        from observability import capture_exception
        capture_exception(exc, **tags)
    except Exception:
        pass


# Auto-register handlers
from tasks.present_complaint import handle as _present_complaint_handle  # noqa: E402
from tasks.present_complaint_reg import handle as _present_complaint_reg_handle  # noqa: E402
from tasks.submit_request_transparencia import handle as _submit_request_transparencia_handle  # noqa: E402
from tasks.submit_request_reg import handle as _submit_request_reg_handle  # noqa: E402

register("present_complaint", _present_complaint_handle)
register("present_complaint_reg", _present_complaint_reg_handle)
register("submit_request_transparencia", _submit_request_transparencia_handle)
register("submit_request_reg", _submit_request_reg_handle)
