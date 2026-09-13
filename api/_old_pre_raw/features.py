"""
features.py — Construcción de la fila que consume el modelo.

ESTA ES LA PIEZA CRÍTICA DEL PROYECTO. Es la única defensa contra el train/serve skew:
el modelo aprende una relación entre unos números concretos y el retraso, y si en
producción le llegan números calculados de otra forma (otra ventana, otro huso, otro
criterio de nulos) no da error, da predicciones sutilmente peores y nadie se entera.

Por eso esta función tiene que ser LITERALMENTE el mismo código en entrenamiento y en
servicio, y por eso la mantiene quien gobierna el pipeline, no quien entrena.

Entrada: un tramo del resolutor + el contexto de la caché.
Salida: una fila que cumple el contrato de predicción v1.0.0, validada antes de salir.

TFM Cercanías RENFE · UCM · 2026
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

import calendario
from contrato import (
    CONTRACT_VERSION,
    DEGRADED_BLOCKS_FIELD,
    empty_row,
    validate_row,
)
from tiempo import MADRID, iso_utc
from resolver import Tramo


def construir_fila(
    tramo: Tramo,
    t0_utc: datetime,
    gtfs_version: str,
    estado_linea: dict[str, Any] | None = None,
    meteo: dict[str, Any] | None = None,
    alertas: dict[str, Any] | None = None,
    estado_propio: dict[str, Any] | None = None,
    request_id: str | None = None,
) -> dict[str, Any]:
    """Construye la fila de features de un tramo para el instante de consulta t0.

    Los bloques opcionales que lleguen a None se dejan nulos y se anotan en
    'degraded_blocks'. Nunca se imputan aquí: el modelo sabe tratar un nulo, y una
    imputación silenciosa en el servicio sería una diferencia invisible con el
    entrenamiento.
    """
    fila = empty_row()

    # --- Metadatos (no son features; viajan para trazabilidad) ---
    fila.update(
        {
            "request_id": request_id or str(uuid.uuid4()),
            "trip_id": tramo.trip_id,
            "service_date": tramo.service_date.isoformat(),
            "t0_utc": iso_utc(t0_utc),
            "sched_arrival_utc": iso_utc(tramo.llegada_teorica_utc),
            "gtfs_version": gtfs_version,
            "contract_version": CONTRACT_VERSION,
        }
    )

    # --- Topología y horizonte ---
    fila.update(
        {
            "line_id": tramo.line_id,
            "origin_stop_id": tramo.origen_stop_id,
            "dest_stop_id": tramo.destino_stop_id,
            "dest_stop_sequence": tramo.dest_stop_sequence,
            "trip_total_stops": tramo.trip_total_stops,
            "stops_to_dest": tramo.paradas_intermedias + 1,
            "horizon_s": tramo.horizon_s,
            "regime": tramo.regime,
        }
    )

    # --- Calendario ---
    # Se derivan de la LLEGADA TEÓRICA en hora de Madrid, no del instante de consulta:
    # lo que condiciona el retraso es cuándo llega el tren, no cuándo se preguntó.
    llegada_local = tramo.llegada_teorica_utc.astimezone(MADRID)
    fila.update(calendario.features_calendario(tramo.service_date))
    fila.update(
        {
            "hour_local": llegada_local.hour,
            "minute_of_day_local": llegada_local.hour * 60 + llegada_local.minute,
        }
    )

    degradados: list[str] = []

    # --- Estado de red ---
    if estado_linea:
        for clave in (
            "line_delay_mean_30m_s",
            "line_delay_p90_30m_s",
            "line_active_trains_30m",
            "net_delay_mean_30m_s",
        ):
            fila[clave] = estado_linea.get(clave)
    else:
        degradados.append("estado_red")

    # --- Estado propio del tren (solo existe en régimen A) ---
    if tramo.regime == "A":
        if estado_propio:
            fila["own_delay_s"] = estado_propio.get("own_delay_s")
            fila["own_delay_age_s"] = estado_propio.get("own_delay_age_s")
            fila["own_last_stop_sequence"] = estado_propio.get("own_last_stop_sequence")
        else:
            # El tren circula pero no tenemos su estado: es una degradación real y hay
            # que declararla. En régimen B, en cambio, el nulo es lo esperado y no
            # significa que falte ningún dato.
            degradados.append("estado_propio")

    # --- Meteorología ---
    if meteo:
        for clave in ("temp_c", "precip_mm_1h", "wind_gust_ms"):
            fila[clave] = meteo.get(clave)
    else:
        degradados.append("meteo")

    # --- Incidencias ---
    if alertas:
        fila["alerts_active_line"] = alertas.get("alerts_active_line")
        fila["alerts_active_stop"] = alertas.get("alerts_active_stop", 0)
        fila["alert_severity_max"] = alertas.get("alert_severity_max", 0.0)
    else:
        degradados.append("alertas")

    fila[DEGRADED_BLOCKS_FIELD] = degradados

    # Falla ruidosamente aquí antes que en silencio en el modelo.
    validate_row(fila, strict_unknown=False)
    return fila


def construir_filas(
    tramos: list[Tramo],
    t0_utc: datetime,
    gtfs_version: str,
    cache: Any,
    request_id: str | None = None,
) -> list[dict[str, Any]]:
    """Construye las filas de varios tramos leyendo el contexto de la caché.

    Se envían todas en una sola petición al modelo: el coste de una llamada por lotes
    es prácticamente el mismo que el de una individual.
    """
    filas = []
    for tramo in tramos:
        filas.append(
            construir_fila(
                tramo=tramo,
                t0_utc=t0_utc,
                gtfs_version=gtfs_version,
                estado_linea=cache.estado_linea(tramo.line_id),
                meteo=cache.meteo(tramo.destino_stop_id),
                alertas=cache.alertas(tramo.line_id),
                estado_propio=None,  # F7: se leerá del feed en tiempo real
                request_id=request_id,
            )
        )
    return filas


if __name__ == "__main__":
    from catalogo import Catalogo
    from estado_red import CacheContexto, FuenteSimulada
    from resolver import resolver_trayecto
    from tiempo import ahora_utc, formatear_local

    cat = Catalogo("../datos/catalogo.json")
    lineas = [l["line_id"] for l in cat.lineas]
    cache = CacheContexto(FuenteSimulada(), lineas)
    cache.refrescar()

    origen = cat.buscar_estaciones("atocha", 1)[0]["stop_id"]
    destino = cat.buscar_estaciones("alcala de henares", 1)[0]["stop_id"]

    ahora = ahora_utc()
    trayectos, aviso = resolver_trayecto(cat, origen, destino, ahora)
    if not trayectos:
        print("Sin trayectos:", aviso)
        raise SystemExit

    tramos = [t.tramos[0] for t in trayectos]
    filas = construir_filas(tramos, ahora, cat.gtfs_version, cache)

    print(f"{len(filas)} filas construidas y validadas contra el contrato "
          f"v{CONTRACT_VERSION}\n")

    f = filas[0]
    print(f"Primer tren: {f['line_id']} {f['trip_id']} · llega "
          f"{formatear_local(tramos[0].llegada_teorica_utc)}")
    print(f"  horizonte {f['horizon_s'] // 60} min · régimen {f['regime']} · "
          f"{f['stops_to_dest']} paradas hasta el destino")
    print(f"  calendario: dow={f['dow']} finde={f['is_weekend']} "
          f"festivo={f['is_holiday']} lectivo={f['is_school_period']}")
    print(f"  estado de línea: media {f['line_delay_mean_30m_s']} s · "
          f"{f['line_active_trains_30m']} trenes activos")
    print(f"  meteo: {f['temp_c']} °C · {f['precip_mm_1h']} mm · {f['wind_gust_ms']} m/s")
    print(f"  bloques degradados: {f['degraded_blocks']}")

    # Prueba de extremo a extremo contra el stub, si está levantado.
    try:
        from predictor import Predictor

        for pred, tramo in zip(Predictor().predict(filas), tramos):
            minutos = pred["delay_s_p50"] / 60
            print(f"\n{tramo.line_id} sale {formatear_local(tramo.salida_teorica_utc)} · "
                  f"llegada teórica {formatear_local(tramo.llegada_teorica_utc)} · "
                  f"retraso previsto {minutos:.1f} min")
    except Exception as exc:  # noqa: BLE001
        print(f"\n(Stub no disponible: {exc})")
