"""
contrato.py — Contrato de predicción entre la interfaz y el modelo.

Única fuente de verdad sobre QUÉ entra y QUÉ sale del modelo. Lo importan tres piezas:
  1. El stub de servicio (`serving_stub.py`), mientras no hay modelo real.
  2. El cliente de la interfaz (`predictor.py`).
  3. OBLIGATORIO: el código de entrenamiento del equipo de modelado.

Si las tres no importan este fichero, hay train/serve skew garantizado.

TFM Cercanías RENFE · UCM · 2026
"""

from __future__ import annotations

from typing import Any

# Versión del contrato. Cambiar SOLO con acuerdo explícito del equipo de modelado.
CONTRACT_VERSION = "1.0.0"

# --------------------------------------------------------------------------------------
# Metadatos: viajan en cada fila para trazabilidad, pero NO son features del modelo.
# --------------------------------------------------------------------------------------
PASSTHROUGH_FIELDS: tuple[str, ...] = (
    "request_id",         # UUID de la petición (logs)
    "trip_id",            # identificador RT normalizado por núcleo
    "service_date",       # fecha de servicio GTFS 'YYYY-MM-DD' (NO fecha natural)
    "t0_utc",             # instante de consulta, ISO-8601 UTC con 'Z'
    "sched_arrival_utc",  # llegada teórica al destino según GTFS, ISO-8601 UTC
    "gtfs_version",       # sha256 corto del zip GTFS usado para resolver el horario
    "contract_version",   # eco de CONTRACT_VERSION
)

# --------------------------------------------------------------------------------------
# Features. Cada entrada: (nombre, tipo lógico, unidad, obligatoria, descripción)
#   - "obligatoria" = el modelo NUNCA la recibe nula. Si falta, la petición se rechaza.
#   - Las no obligatorias pueden llegar a None/NaN: el modelo debe tolerarlo de forma
#     nativa (LightGBM/XGBoost lo hacen). Prohibido imputar en la interfaz.
# --------------------------------------------------------------------------------------
FEATURE_SPEC: tuple[tuple[str, str, str, bool, str], ...] = (
    # --- Identidad del trayecto y topología (GTFS) ---
    ("line_id",              "cat",   "-",  True,  "C1..C10; modelo único con línea categórica"),
    ("origin_stop_id",       "cat",   "-",  True,  "parada de origen consultada"),
    ("dest_stop_id",         "cat",   "-",  True,  "parada de destino: donde se predice"),
    ("dest_stop_sequence",   "int",   "-",  True,  "posición del destino en el recorrido"),
    ("trip_total_stops",     "int",   "-",  True,  "nº total de paradas del trip"),
    ("stops_to_dest",        "int",   "-",  True,  "paradas entre origen y destino"),
    # --- Horizonte (variable, nunca constante) ---
    ("horizon_s",            "int",   "s",  True,  "sched_arrival_utc - t0_utc"),
    # --- Calendario (derivado en Europe/Madrid dentro de features.py) ---
    ("dow",                  "int",   "-",  True,  "día de la semana, 0=lunes"),
    ("hour_local",           "int",   "-",  True,  "hora local de la llegada teórica"),
    ("minute_of_day_local",  "int",   "min", True, "minuto del día de la llegada teórica"),
    ("is_weekend",           "bool",  "-",  True,  "sábado o domingo"),
    ("is_holiday",           "bool",  "-",  True,  "festivo nacional/autonómico/local Madrid"),
    ("is_school_period",     "bool",  "-",  True,  "periodo lectivo"),
    # --- Régimen de predicción ---
    ("regime",               "cat",   "-",  True,  "'A' tren en circulación / 'B' aún no salido"),
    # --- Estado propio del tren (nulo en régimen B) ---
    ("own_delay_s",          "float", "s",  False, "último retraso publicado del propio tren"),
    ("own_delay_age_s",      "float", "s",  False, "antigüedad de esa observación"),
    ("own_last_stop_sequence", "int", "-",  False, "última parada publicada del tren"),
    # --- Estado de red (ventana de 30 min hasta t0) ---
    ("line_delay_mean_30m_s", "float", "s", False, "retraso medio de la línea, últimos 30 min"),
    ("line_delay_p90_30m_s",  "float", "s", False, "p90 del retraso de la línea, últimos 30 min"),
    ("line_active_trains_30m", "int",  "-", False, "trenes activos de la línea, últimos 30 min"),
    ("net_delay_mean_30m_s",  "float", "s", False, "retraso medio del núcleo Madrid completo"),
    # --- Meteorología (estación AEMET asignada a la parada de destino) ---
    ("temp_c",               "float", "C",  False, "temperatura del aire"),
    ("precip_mm_1h",         "float", "mm", False, "precipitación acumulada 1 h"),
    ("wind_gust_ms",         "float", "m/s", False, "racha máxima de viento"),
    # --- Incidencias (salida del NLP; constante 0 hasta que se entregue) ---
    ("alerts_active_line",   "int",   "-",  False, "nº de alertas activas de la línea"),
    ("alerts_active_stop",   "int",   "-",  False, "nº de alertas activas de la parada"),
    ("alert_severity_max",   "float", "0-1", False, "severidad máxima estimada por NLP"),
)

# Bandera de degradación: lista de bloques cuya fuente no estaba disponible.
# NO es feature: la usa la interfaz para avisar al usuario y el log para auditoría.
DEGRADED_BLOCKS_FIELD = "degraded_blocks"

FEATURE_NAMES: tuple[str, ...] = tuple(f[0] for f in FEATURE_SPEC)
REQUIRED_FEATURES: tuple[str, ...] = tuple(f[0] for f in FEATURE_SPEC if f[3])
OPTIONAL_FEATURES: tuple[str, ...] = tuple(f[0] for f in FEATURE_SPEC if not f[3])

# Campos de la respuesta del modelo (§4 del contrato).
PREDICTION_FIELDS: tuple[str, ...] = (
    "delay_s_p50",    # predicción central, en SEGUNDOS
    "delay_s_p10",    # cuantil bajo (== p50 si el modelo es puntual)
    "delay_s_p90",    # cuantil alto (== p50 si el modelo es puntual)
    "has_interval",   # False si el modelo v1 es puntual
    "model_version",  # versión del modelo registrado en MLflow
)


class ContractError(ValueError):
    """Violación del contrato: fila incompleta o campo desconocido."""


def validate_row(row: dict[str, Any], strict_unknown: bool = True) -> None:
    """Valida una fila de features contra el contrato.

    Lanza ContractError si falta una feature obligatoria, si viene nula, o si aparece
    un campo desconocido (con strict_unknown=True). Fallar aquí es barato; fallar en
    producción con una feature silenciosamente ausente, no.
    """
    missing = [f for f in REQUIRED_FEATURES if f not in row or row[f] is None]
    if missing:
        raise ContractError(f"Faltan features obligatorias o vienen nulas: {missing}")

    if strict_unknown:
        conocidos = set(FEATURE_NAMES) | set(PASSTHROUGH_FIELDS) | {DEGRADED_BLOCKS_FIELD}
        desconocidos = sorted(set(row) - conocidos)
        if desconocidos:
            raise ContractError(
                f"Campos fuera de contrato: {desconocidos}. "
                f"Añádelos primero a FEATURE_SPEC y sube CONTRACT_VERSION."
            )


def empty_row() -> dict[str, Any]:
    """Fila plantilla con todas las claves del contrato a None. Útil en tests."""
    fila: dict[str, Any] = {c: None for c in PASSTHROUGH_FIELDS}
    fila.update({f: None for f in FEATURE_NAMES})
    fila[DEGRADED_BLOCKS_FIELD] = []
    fila["contract_version"] = CONTRACT_VERSION
    return fila


def to_model_frame(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Proyecta las filas a SOLO las features del contrato, en orden fijo.

    El orden de columnas importa: algunos modelos serializados lo exigen. Los metadatos
    se quedan fuera para que el modelo no pueda aprender de ellos por accidente
    (p. ej. trip_id como categórica de alta cardinalidad = fuga encubierta).
    """
    return [{f: fila.get(f) for f in FEATURE_NAMES} for fila in rows]
