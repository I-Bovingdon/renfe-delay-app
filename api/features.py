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

import alertas as alertas_mod
import calendario
from contrato import (
    ANTELACION_MAXIMA_SALIDA_S,
    CONTRACT_VERSION,
    DEGRADED_BLOCKS_FIELD,
    empty_row,
    validate_row,
)
from tiempo import MADRID, iso_utc
from resolver import Tramo


def filtrar_por_dominio(
    trayectos: list[Any], cat: Any, t0_utc: datetime
) -> tuple[list[Any], int]:
    """Deja solo los trayectos sobre los que el modelo puede predecir sin extrapolar.

    Vive aquí, junto a la construcción de la fila, porque es la otra mitad del mismo
    problema: `construir_fila` garantiza que las variables SIGNIFIQUEN lo mismo que en
    entrenamiento, y esto garantiza que el PUNTO del espacio de variables sea uno que el
    modelo llegó a ver. Las dos son defensas contra el mismo fallo silencioso.

    Lo importan la API y el asistente, que resuelven trayectos por caminos distintos y
    tienen que descartar exactamente lo mismo: si discreparan, la pantalla y el chat
    ofrecerían trenes distintos para la misma consulta.

    Devuelve los trayectos admitidos y cuántos se han descartado, para poder avisar.
    """
    admitidos, descartados = [], 0
    for trayecto in trayectos:
        # Con transbordos, manda el primer tramo: es el que aún no ha arrancado.
        tramo = trayecto.tramos[0]
        salida = cat.salida_cabecera_utc(tramo.trip_id, tramo.service_date)
        if salida is None:
            # Trip no localizado en el catálogo. No se descarta: no se puede afirmar
            # que esté fuera de dominio algo que no se ha podido situar en el horario.
            admitidos.append(trayecto)
            continue
        antelacion_s = (salida - t0_utc).total_seconds()
        if antelacion_s > ANTELACION_MAXIMA_SALIDA_S:
            descartados += 1
        else:
            admitidos.append(trayecto)
    return admitidos, descartados


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
        for clave in ("temp_c", "precip_mm_1h", "wind_speed_ms"):
            fila[clave] = meteo.get(clave)
    else:
        degradados.append("meteo")

    # --- Incidencias ---
    # Las seis columnas viajan SIEMPRE con valor, incluso con el feed caído: el
    # pipeline de entrenamiento hace fillna(0) y el modelo no vio nunca un nulo
    # aquí. Lo que se degrada es el AVISO al usuario, no la entrada del modelo.
    # Es la diferencia con la meteorología, donde el nulo sí está dentro de la
    # distribución de entrenamiento.
    fila.update(alertas if alertas is not None else alertas_mod.ALERTAS_CERO)
    if alertas is None:
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
    fuente_meteo: Any = None,
    almacen_alertas: Any = None,
) -> list[dict[str, Any]]:
    """Construye las filas de varios tramos leyendo el contexto de la caché.

    Se envían todas en una sola petición al modelo: el coste de una llamada por lotes
    es prácticamente el mismo que el de una individual.
    """
    # La ventana de incidencias se calcula UNA vez por consulta, no por tramo:
    # depende de t0 y de la línea, y recorrer el índice de alertas cuatro veces
    # para el mismo instante sería tirar trabajo.
    ventana_alertas = (
        almacen_alertas.ventana_modelo(t0_utc) if almacen_alertas is not None else None
    )

    filas = []
    for tramo in tramos:
        filas.append(
            construir_fila(
                tramo=tramo,
                t0_utc=t0_utc,
                gtfs_version=gtfs_version,
                estado_linea=cache.estado_linea(tramo.line_id),
                # F8: la meteorología ya NO viene de la caché de estado de red, que
                # la devolvía vacía. Viene de FuenteMeteo, que lee el raw de AEMET
                # en su propia tarea de fondo. Si no se inyecta (tests, pruebas del
                # módulo), el bloque queda degradado, que es el comportamiento
                # anterior y sigue siendo correcto.
                meteo=(
                    fuente_meteo.observacion(tramo.destino_stop_id)
                    if fuente_meteo is not None else None
                ),
                # F8: las incidencias vienen del almacén que alimenta la
                # pantalla de alertas, no de la caché de estado de red, que las
                # devolvía vacías. El cruce es por igualdad exacta de línea,
                # ramas incluidas, porque es lo que hacía el merge_asof del
                # entrenamiento. Ver el bloque RAMAS DE LÍNEA en alertas.py.
                alertas=(
                    ventana_alertas.get(tramo.line_id, alertas_mod.ALERTAS_CERO)
                    if ventana_alertas is not None else None
                ),
                # F7: estado real del tren, leído del feed por FuenteRaw. El cruce
                # feed <-> catálogo lo resuelve la caché por núcleo del trip_id.
                estado_propio=cache.estado_propio(tramo.trip_id),
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
