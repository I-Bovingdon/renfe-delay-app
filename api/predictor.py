"""
predictor.py — Cliente único del modelo para la interfaz.

La interfaz llama SIEMPRE a `Predictor.predict(filas)` y no sabe dónde vive el modelo.
Tres backends intercambiables por variable de entorno, sin tocar una línea de la UI:

    PREDICTOR_BACKEND=stub        -> HTTP contra serving_stub.py (hoy, sin modelo)
    PREDICTOR_BACKEND=local       -> mlflow.pyfunc cargado en el propio proceso del VPS
    PREDICTOR_BACKEND=databricks  -> REST contra Databricks Model Serving

Esto es lo que convierte "¿Model Serving o inferencia local?" en una decisión reversible
en 5 segundos el día de la defensa, en vez de en una reescritura.

Secretos: el token de Databricks se lee de la variable de entorno DATABRICKS_TOKEN,
que vive en el fichero de entorno del servicio systemd (modo 600, propietario tfm) y
NUNCA en el repositorio.

TFM Cercanías RENFE · UCM · 2026
"""

from __future__ import annotations

import logging
import os
import time
from typing import Any

import requests

from contrato import (
    CONTRACT_VERSION,
    PREDICTION_FIELDS,
    ContractError,
    to_model_frame,
    validate_row,
)

log = logging.getLogger(__name__)

TIMEOUT_S = float(os.getenv("PREDICTOR_TIMEOUT_S", "20"))
REINTENTOS = int(os.getenv("PREDICTOR_RETRIES", "1"))


class PredictorError(RuntimeError):
    """Fallo al obtener predicciones. La interfaz lo traduce a modo degradado."""


# --------------------------------------------------------------------------------------
# Backends
# --------------------------------------------------------------------------------------
class _BackendHTTP:
    """Base común para stub y Databricks: ambos hablan protocolo MLflow scoring."""

    def __init__(self, url: str, cabeceras: dict[str, str] | None = None, nombre: str = "http"):
        self.url = url
        self.cabeceras = {"Content-Type": "application/json", **(cabeceras or {})}
        self.nombre = nombre

    def score(self, filas: list[dict[str, Any]]) -> list[dict[str, Any]]:
        cuerpo = {"dataframe_records": filas}
        ultimo_error: Exception | None = None

        for intento in range(REINTENTOS + 1):
            try:
                resp = requests.post(
                    self.url, json=cuerpo, headers=self.cabeceras, timeout=TIMEOUT_S
                )
                if resp.status_code == 400:
                    # Violación de contrato: reintentar no arregla nada, es un bug nuestro.
                    raise ContractError(f"El modelo rechazó la petición: {resp.text[:500]}")
                resp.raise_for_status()
                return resp.json()["predictions"]
            except ContractError:
                raise
            except Exception as exc:  # noqa: BLE001 — se relanza como PredictorError
                ultimo_error = exc
                # Arranque en frío de Model Serving: 10-20 s, a veces minutos.
                espera = 2 ** intento
                log.warning("Backend %s falló (intento %d): %s", self.nombre, intento + 1, exc)
                if intento < REINTENTOS:
                    time.sleep(espera)

        raise PredictorError(f"Backend {self.nombre} no respondió: {ultimo_error}")


class _BackendLocal:
    """Inferencia en el propio VPS con el modelo MLflow descargado.

    Es el camino recomendado para producción: sin red, sin token, sin arranque en frío,
    sin cuota. Databricks sigue siendo la plataforma de entrenamiento y de registro.
    """

    def __init__(self, model_uri: str):
        import mlflow.pyfunc  # import perezoso: no cargar mlflow si no se usa este backend

        self.model_uri = model_uri
        self.modelo = mlflow.pyfunc.load_model(model_uri)
        log.info("Modelo MLflow cargado en local desde %s", model_uri)

    def score(self, filas: list[dict[str, Any]]) -> list[dict[str, Any]]:
        import pandas as pd

        salida = self.modelo.predict(pd.DataFrame(filas))
        # El envoltorio pyfunc debe devolver un DataFrame con las columnas del contrato.
        if hasattr(salida, "to_dict"):
            return salida.to_dict(orient="records")
        # Respaldo si el modelo devuelve un array plano (v1 puntual, sin envoltorio).
        return [
            {
                "delay_s_p50": float(v),
                "delay_s_p10": float(v),
                "delay_s_p90": float(v),
                "has_interval": False,
                "model_version": self.model_uri,
            }
            for v in salida
        ]


# --------------------------------------------------------------------------------------
# Fachada
# --------------------------------------------------------------------------------------
class Predictor:
    """Punto único de entrada al modelo desde la interfaz."""

    def __init__(self, backend: str | None = None):
        self.backend_nombre = (backend or os.getenv("PREDICTOR_BACKEND", "stub")).lower()

        if self.backend_nombre == "stub":
            url = os.getenv("STUB_URL", "http://127.0.0.1:8601/invocations")
            self._backend: Any = _BackendHTTP(url, nombre="stub")

        elif self.backend_nombre == "local":
            uri = os.getenv("MODEL_URI", "/home/tfm/modelo/cercanias_delay")
            self._backend = _BackendLocal(uri)

        elif self.backend_nombre == "databricks":
            url = os.environ["DATABRICKS_ENDPOINT_URL"]     # .../serving-endpoints/<n>/invocations
            token = os.environ["DATABRICKS_TOKEN"]          # SOLO desde el entorno del servicio
            self._backend = _BackendHTTP(
                url, {"Authorization": f"Bearer {token}"}, nombre="databricks"
            )
        else:
            raise ValueError(f"Backend desconocido: {self.backend_nombre}")

    def predict(self, filas: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """Valida contra el contrato, puntúa y normaliza la respuesta.

        `filas` son filas completas (metadatos + features). Al modelo solo viajan las
        features, en el orden fijado por el contrato.
        """
        if not filas:
            return []

        for fila in filas:
            fila.setdefault("contract_version", CONTRACT_VERSION)
            validate_row(fila, strict_unknown=False)

        t0 = time.perf_counter()
        predicciones = self._backend.score(to_model_frame(filas))
        latencia_ms = (time.perf_counter() - t0) * 1000

        if len(predicciones) != len(filas):
            raise PredictorError(
                f"El modelo devolvió {len(predicciones)} predicciones para {len(filas)} filas"
            )

        salida = []
        for fila, pred in zip(filas, predicciones):
            normalizada = {campo: pred.get(campo) for campo in PREDICTION_FIELDS}
            normalizada.update(
                {
                    "trip_id": fila.get("trip_id"),
                    "dest_stop_id": fila.get("dest_stop_id"),
                    "sched_arrival_utc": fila.get("sched_arrival_utc"),
                    "degraded_blocks": fila.get("degraded_blocks", []),
                    "backend": self.backend_nombre,
                    "latency_ms": round(latencia_ms, 1),
                }
            )
            salida.append(normalizada)
        return salida


if __name__ == "__main__":
    # Prueba de humo end-to-end contra el stub (levántalo antes en otra terminal).
    from contrato import empty_row

    fila = empty_row()
    fila.update(
        {
            "request_id": "smoke-001",
            "trip_id": "10T1234C5",
            "service_date": "2026-08-25",
            "t0_utc": "2026-08-25T06:30:00Z",
            "sched_arrival_utc": "2026-08-25T07:12:00Z",
            "gtfs_version": "a1b2c3d4",
            "line_id": "C5",
            "origin_stop_id": "17000",
            "dest_stop_id": "18000",
            "dest_stop_sequence": 14,
            "trip_total_stops": 22,
            "stops_to_dest": 9,
            "horizon_s": 2520,
            "dow": 1,
            "hour_local": 9,
            "minute_of_day_local": 552,
            "is_weekend": False,
            "is_holiday": False,
            "is_school_period": False,
            "regime": "B",
        }
    )
    for p in Predictor().predict([fila]):
        print(p)
