"""
main.py — API de la interfaz de predicción de retrasos.

Une todas las piezas detrás de cuatro endpoints HTTP:

    GET  /api/estaciones   catálogo de estaciones para el selector
    GET  /api/lineas       trazados y colores para el mapa
    POST /api/consulta     origen + destino + hora  ->  trenes con predicción
    GET  /api/salud        estado de las fuentes, para el modo degradado

Dos decisiones que gobiernan el rendimiento:

  1. El catálogo se carga UNA vez al arrancar, no por petición.
  2. El contexto (estado de red, meteo, alertas) lo refresca una tarea en segundo plano
     cada 60 s; la petición del usuario solo lee de la caché. Es lo que mantiene la
     latencia por debajo de 1 s en una máquina de 2 vCPU compartida con la captura 24/7.

Ejecutar en desarrollo (PC):
    uvicorn main:app --reload --port 8000
    -> documentación interactiva en http://127.0.0.1:8000/docs

TFM Cercanías RENFE · UCM · 2026
"""

from __future__ import annotations

import asyncio
import datetime as dt
import logging
import os
import time
import uuid
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles

import features
from catalogo import Catalogo
from estado_red import INTERVALO_REFRESCO_S, CacheContexto, FuenteSimulada
from modelos import (
    ConsultaTrayecto,
    Estacion,
    OpcionTrayecto,
    ParadaTramo,
    RespuestaConsulta,
    Retraso,
    Tramo as TramoRespuesta,
)
from predictor import Predictor, PredictorError
from resolver import resolver_trayecto
from tiempo import ahora_utc, desde_iso, iso_utc

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
log = logging.getLogger("api")

RUTA_CATALOGO = os.getenv("RUTA_CATALOGO", "../datos/catalogo.json")
RUTA_WEB = Path(os.getenv("RUTA_WEB", "../web"))

# Estado del proceso. Se puebla en el arranque (lifespan) y no se toca después.
estado: dict[str, object] = {}


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Arranque y parada del servicio.

    Cargar el catálogo aquí, y no en la primera petición, hace que un error de datos se
    manifieste al desplegar y no delante del tribunal.
    """
    t0 = time.perf_counter()
    cat = Catalogo(RUTA_CATALOGO)
    log.info(
        "Catálogo cargado en %.2f s · versión %s · %d estaciones · %d trips",
        time.perf_counter() - t0, cat.gtfs_version, len(cat.estaciones), len(cat.trips),
    )

    cache = CacheContexto(FuenteSimulada(), [l["line_id"] for l in cat.lineas])
    cache.refrescar()

    estado["catalogo"] = cat
    estado["cache"] = cache
    estado["predictor"] = Predictor()
    estado["arrancado_utc"] = ahora_utc()

    tarea = asyncio.create_task(_refresco_periodico(cache))
    log.info("API lista. Refresco de contexto cada %d s.", INTERVALO_REFRESCO_S)

    yield

    tarea.cancel()
    log.info("API detenida.")


async def _refresco_periodico(cache: CacheContexto) -> None:
    """Refresca la caché en segundo plano, para siempre.

    Envuelto en try/except a conciencia: una excepción no capturada aquí mataría la
    tarea en silencio y la caché se quedaría congelada sin que nadie lo notase.
    """
    while True:
        try:
            await asyncio.sleep(INTERVALO_REFRESCO_S)
            await asyncio.to_thread(cache.refrescar)
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001
            log.exception("Error en el refresco periódico; se reintenta en el siguiente ciclo")


app = FastAPI(
    title="Predicción de retrasos · Cercanías Madrid",
    description="TFM · Máster en Data Science, Big Data & Business Analytics (UCM)",
    version="1.0.0",
    lifespan=lifespan,
)

# En producción el navegador y la API comparten origen (Caddy sirve ambos), así que
# CORS no haría falta. Se deja permisivo solo para poder abrir el HTML en local
# mientras se desarrolla; en F6 se restringe al dominio real.
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["GET", "POST"],
    allow_headers=["*"],
)


def _cat() -> Catalogo:
    return estado["catalogo"]  # type: ignore[return-value]


def _a_estacion(datos: dict) -> Estacion:
    return Estacion(**{k: datos[k] for k in ("stop_id", "nombre", "lat", "lon", "lineas")})


# ============================================================== endpoints ==========
@app.get("/api/estaciones", response_model=list[Estacion])
def listar_estaciones(q: str | None = None):
    """Estaciones del núcleo Madrid. Con `q`, filtra para el autocompletado."""
    cat = _cat()
    datos = cat.buscar_estaciones(q, limite=10) if q else cat.listar_estaciones()
    return [_a_estacion(e) for e in datos]


@app.get("/api/lineas")
def listar_lineas():
    """Trazados y colores de las líneas, para pintar el mapa."""
    cat = _cat()
    return {
        "gtfs_version": cat.gtfs_version,
        "lineas": [
            {"line_id": l["line_id"], "color": l["color"], "trazado": l["trazado"]}
            for l in cat.lineas
        ],
    }


@app.post("/api/consulta", response_model=RespuestaConsulta)
def consultar(peticion: ConsultaTrayecto):
    """Resuelve un trayecto y devuelve los trenes candidatos con su predicción."""
    inicio = time.perf_counter()
    cat = _cat()
    cache: CacheContexto = estado["cache"]  # type: ignore[assignment]
    predictor: Predictor = estado["predictor"]  # type: ignore[assignment]

    origen = cat.estacion(peticion.origen)
    destino = cat.estacion(peticion.destino)
    if origen is None or destino is None:
        raise HTTPException(status_code=404, detail="Estación no encontrada.")

        # Una fecha mal formada es culpa de quien llama, no un fallo del servidor: 400, no 500.
    try:
        t0 = desde_iso(peticion.salida_desde_utc) if peticion.salida_desde_utc else ahora_utc()
    except ValueError as exc:
        raise HTTPException(
            status_code=400,
            detail="Formato de fecha no válido. Se espera ISO-8601 UTC, "
                   "por ejemplo 2026-09-18T07:30:00Z.",
        ) from exc
    request_id = str(uuid.uuid4())

    trayectos, aviso = resolver_trayecto(cat, peticion.origen, peticion.destino, t0)
    if not trayectos:
        return RespuestaConsulta(
            origen=_a_estacion(origen),
            destino=_a_estacion(destino),
            consultado_en_utc=iso_utc(t0),
            gtfs_version=cat.gtfs_version,
            opciones=[],
            aviso=aviso,
        )

    # Un tramo por trayecto mientras solo haya directos. Cuando entren los transbordos,
    # esta lista tendrá varios por trayecto y el resto del código no cambia.
    tramos = [tr for t in trayectos for tr in t.tramos]
    filas = features.construir_filas(tramos, t0, cat.gtfs_version, cache, request_id)

    try:
        predicciones = predictor.predict(filas)
    except PredictorError as exc:
        log.warning("El modelo no respondió (%s)", exc)
        raise HTTPException(
            status_code=503,
            detail="El servicio de predicción no está disponible en este momento.",
        ) from exc

    # Las predicciones vuelven en el MISMO orden en que se enviaron las filas, así que
    # se consumen con un índice que avanza en paralelo al recorrido de los tramos.
    opciones: list[OpcionTrayecto] = []
    i_pred = 0
    for trayecto in trayectos:
        tramos_resp: list[TramoRespuesta] = []
        for tramo in trayecto.tramos:
            pred = predicciones[i_pred]
            i_pred += 1
            retraso_s = float(pred["delay_s_p50"] or 0.0)
            llegada_estimada = tramo.llegada_teorica_utc.timestamp() + retraso_s

            tramos_resp.append(
                TramoRespuesta(
                    trip_id=tramo.trip_id,
                    line_id=tramo.line_id,
                    origen=ParadaTramo(
                        stop_id=tramo.origen_stop_id,
                        nombre=tramo.origen_nombre,
                        hora_teorica_utc=iso_utc(tramo.salida_teorica_utc),
                    ),
                    destino=ParadaTramo(
                        stop_id=tramo.destino_stop_id,
                        nombre=tramo.destino_nombre,
                        hora_teorica_utc=iso_utc(tramo.llegada_teorica_utc),
                    ),
                    paradas_intermedias=tramo.paradas_intermedias,
                    retraso_s=Retraso(
                        p10=float(pred["delay_s_p10"] or 0.0),
                        p50=retraso_s,
                        p90=float(pred["delay_s_p90"] or 0.0),
                        con_intervalo=bool(pred.get("has_interval", False)),
                    ),
                    llegada_estimada_utc=iso_utc(
                        dt.datetime.fromtimestamp(llegada_estimada, tz=dt.timezone.utc)
                    ),
                    regime=tramo.regime,
                    degraded_blocks=list(pred.get("degraded_blocks") or []),
                )
            )

        ultimo = tramos_resp[-1]
        opciones.append(
            OpcionTrayecto(
                tramos=tramos_resp,
                n_transbordos=trayecto.n_transbordos,
                salida_teorica_utc=tramos_resp[0].origen.hora_teorica_utc,
                llegada_teorica_utc=ultimo.destino.hora_teorica_utc,
                llegada_estimada_utc=ultimo.llegada_estimada_utc,
                retraso_total_s=ultimo.retraso_s.p50,
                enlace_en_riesgo=False,  # siempre falso mientras solo haya directos
            )
        )

    ms = (time.perf_counter() - inicio) * 1000
    log.info(
        "consulta %s: %s -> %s · %d opciones · %.0f ms",
        request_id[:8], origen["nombre"], destino["nombre"], len(opciones), ms,
    )

    return RespuestaConsulta(
        origen=_a_estacion(origen),
        destino=_a_estacion(destino),
        consultado_en_utc=iso_utc(t0),
        gtfs_version=cat.gtfs_version,
        opciones=opciones,
        aviso=aviso,
    )


@app.get("/api/salud")
def salud():
    """Diagnóstico del servicio. Lo consulta la interfaz para avisar de degradaciones."""
    cat = _cat()
    cache: CacheContexto = estado["cache"]  # type: ignore[assignment]
    arrancado = estado["arrancado_utc"]
    return {
        "estado": "ok",
        "arrancado_utc": iso_utc(arrancado),  # type: ignore[arg-type]
        "uptime_s": round((ahora_utc() - arrancado).total_seconds()),  # type: ignore[operator]
        "catalogo": {
            "gtfs_version": cat.gtfs_version,
            "generado_utc": cat.generado_utc,
            "estaciones": len(cat.estaciones),
            "trips": len(cat.trips),
        },
        "contexto": cache.salud(),
        "predictor": {"backend": estado["predictor"].backend_nombre},  # type: ignore[attr-defined]
    }


# La web estática se monta al final para que no capture las rutas /api/*.
# En desarrollo puede no existir todavía; no es motivo para no arrancar la API.
if RUTA_WEB.is_dir():
    app.mount("/", StaticFiles(directory=RUTA_WEB, html=True), name="web")
