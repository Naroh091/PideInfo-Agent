"""Contrato compartido del desenlace de una presentación a un portal.

Presentar una solicitud es una acción externa no idempotente. El paso de
firma es el punto de no retorno: una vez que el agente pulsa el botón que
envía la firma, el portal puede cerrar el registro en su servidor aunque el
agente pierda después la visibilidad. Reportar ese caso como un ``failed``
limpio invita a un reintento a ciegas y a una presentación duplicada.

``UncertainSubmission`` marca exactamente eso: se lanza SOLO desde código que
corre *después* de pulsar el botón de firma. El handler la traduce al
desenlace ``uncertain`` del backend, que no re-envía a ciegas.
"""
from __future__ import annotations


class UncertainSubmission(Exception):
    """La presentación pudo llegar al portal pero el agente no lo confirmó.

    Es decir: un fallo *posterior* al envío de la firma. ``markers`` lleva
    cualquier handle del portal capturado antes de perder visibilidad
    (transparencia: ``{"idBorr": "..."}``); vacío en canales sin borrador
    previo a la firma (REG, CTBG).
    """

    def __init__(self, message: str, *, markers: dict | None = None) -> None:
        super().__init__(message)
        self.markers: dict = markers or {}
