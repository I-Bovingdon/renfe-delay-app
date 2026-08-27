"""
serving_stub.py — Modelo falso con la forma exacta del real.

Expone el MISMO protocolo que Databricks Model Serving y que `mlflow models serve`
(POST /invocations con `dataframe_records`), de modo que la interfaz se construye y se
prueba entera antes de que exista el modelo, y la integración del 03/09 se reduce a
cambiar una variable de entorno.

Las predicciones son deterministas (hash del trip_id como semilla): la misma consulta
devuelve siempre lo mismo, imprescindible para poder escribir tests de la interfaz.

Ejecutar (PC Windows, PowerShell, dentro del .venv del proyecto):
    pip install fastapi uvicorn
    python serving_stub.py
    # -> http://127.0.0.1:8501/invocations

Ejecutar (VPS, para probar el circuito completo antes del modelo real):
    /usr/bin/python3 serving_stub.py --host 127.0.0.1 --port 8501

TFM Cercanías RENFE · UCM · 2026
"""

from __future__ import annotations

import argparse
import hashlib
import math
from typing import Any

from fastapi import FastAPI
from fastapi.responses import JSONResponse
from pydantic import BaseModel

from contrato import (
    CONTRACT_VERSION,
    REQUIRED_FEATURES,
    ContractError,
    validate_row,
)

STUB_MODEL_VERSION = f"stub-{CONTRACT_VERSION}"

app = FastAPI(title="Stub de predicción de retrasos — TFM Cercanías")


class ScoringRequest(BaseModel):
    """Cuerpo de la petición en formato MLflow scoring."""
    dataframe_records: list[dict[str, Any]]


def _semilla(fila: dict[str, Any]) -> float:
    """Ruido reproducible en [0, 1) derivado del trip_id y la parada de destino."""
    clave = f"{fila.get('trip_id')}|{fila.get('dest_stop_id')}|{fila.get('service_date')}"
    digest = hashlib.sha256(clave.encode("utf-8")).hexdigest()
    return int(digest[:8], 16) / 0xFFFFFFFF


def _prediccion_falsa(fila: dict[str, Any]) -> dict[str, Any]:
    """Retraso sintético plausible: hora punta + horizonte + estado de red + ruido.

    No pretende ser un modelo. Solo tiene que producir números en el rango correcto
    (mediana ~0-3 min, cola larga hasta ~30 min) para que la interfaz se vea y se
    pruebe como se verá con el modelo real.
    """
    hora = int(fila.get("hour_local") or 12)
    # Dos jorobas: punta de mañana (~08:00) y de tarde (~19:00).
    punta = 180.0 * (math.exp(-((hora - 8) ** 2) / 4.0) + math.exp(-((hora - 19) ** 2) / 5.0))

    # El retraso crece con el horizonte (más recorrido por delante = más incertidumbre).
    horizonte_min = (fila.get("horizon_s") or 0) / 60.0
    por_horizonte = 1.6 * horizonte_min

    # Si hay estado de red, arrastra: una línea ya retrasada tiende a seguir retrasada.
    estado = fila.get("line_delay_mean_30m_s")
    por_estado = 0.5 * float(estado) if estado is not None else 0.0

    # Régimen B (tren sin salir) => más incertidumbre, intervalo más ancho.
    ancho = 2.4 if fila.get("regime") == "B" else 1.6

    ruido = (_semilla(fila) - 0.35) * 240.0
    p50 = max(0.0, punta + por_horizonte + por_estado + ruido)

    return {
        "delay_s_p50": round(p50, 1),
        "delay_s_p10": round(max(0.0, p50 / ancho - 45.0), 1),
        "delay_s_p90": round(p50 * ancho + 120.0, 1),
        "has_interval": True,   # el stub SÍ da intervalo: obliga a que la UI lo soporte ya
        "model_version": STUB_MODEL_VERSION,
    }


@app.get("/health")
def health() -> dict[str, Any]:
    """Sonda de vida para systemd y para el modo degradado de la interfaz."""
    return {
        "status": "ok",
        "backend": "stub",
        "contract_version": CONTRACT_VERSION,
        "required_features": list(REQUIRED_FEATURES),
    }


@app.post("/invocations")
def invocations(peticion: ScoringRequest) -> Any:
    """Puntúa un lote de filas. Mismo contrato de entrada/salida que el modelo real."""
    filas = peticion.dataframe_records
    if not filas:
        return JSONResponse(status_code=400, content={"error": "dataframe_records vacío"})

    for i, fila in enumerate(filas):
        try:
            # strict_unknown=False: durante el desarrollo la UI puede enviar campos extra
            # sin romper; lo que NUNCA se tolera es que falte una feature obligatoria.
            validate_row(fila, strict_unknown=False)
        except ContractError as exc:
            faltan = [f for f in REQUIRED_FEATURES if fila.get(f) is None]
            return JSONResponse(
                status_code=400,
                content={"error": str(exc), "row_index": i, "missing_fields": faltan},
            )

    return {"predictions": [_prediccion_falsa(f) for f in filas]}


if __name__ == "__main__":
    import uvicorn

    parser = argparse.ArgumentParser(description="Stub de predicción TFM Cercanías")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8501)
    args = parser.parse_args()

    uvicorn.run(app, host=args.host, port=args.port)
