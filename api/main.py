"""
main.py — API de la interfaz de predicción de retrasos.

Une todas las piezas detrás de cuatro endpoints HTTP:

    GET  /api/estaciones   catálogo de estaciones para el selector
    GET  /api/lineas       trazados y colores para el mapa
    POST /api/consulta     origen + destino + hora  ->  trenes con predicción
    GET  /api/alertas      incidencias del día, clasificadas
    GET  /api/mapa         posiciones de los trenes en circulación ahora
    GET  /api/salud        estado de las fuentes, para el modo degradado
    POST /api/chat         asistente conversacional (detrás de CHAT_HABILITADO)

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

from fastapi import FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles

import alertas
import chat as chat_mod
import features
import historico as historico_mod
import meteo as meteo_mod
from catalogo import Catalogo
from estado_red import INTERVALO_REFRESCO_S, CacheContexto
from fuente_raw import FuenteRaw
from modelos import (
    ConsultaChat,
    ConsultaTrayecto,
    Estacion,
    OpcionTrayecto,
    ParadaTramo,
    RespuestaConsulta,
    Retraso,
    Tramo as TramoRespuesta,
)
from posiciones import FuentePosiciones
from predictor import Predictor, PredictorError
import contrato
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

    # FuenteRaw necesita el conjunto de paradas del núcleo para filtrar los trenes de
    # Madrid: el feed de RENFE es nacional y el sufijo de línea se repite entre
    # núcleos (existe un C1 en Sevilla). El filtro es topológico, no geográfico.
    cache = CacheContexto(
        FuenteRaw(paradas_madrid=cat.estaciones),
        [l["line_id"] for l in cat.lineas],
    )
    if not cache.refrescar():
        # No se aborta el arranque: la API tiene que responder aunque el estado de red
        # no esté disponible, y el modelo sabe tratar los nulos. Pero se registra en
        # ERROR para que se vea en journalctl al desplegar.
        log.error(
            "El estado de red no se pudo calcular en el arranque: %s",
            cache.salud()["ultimo_error"],
        )

    estado["catalogo"] = cat
    estado["cache"] = cache
    estado["predictor"] = Predictor()
    estado["arrancado_utc"] = ahora_utc()

    # Alertas: caché propia, hilo propio. No se reutiliza estado_red para no
    # acoplar dominios de fallo (ver alertas.py, docstring de arrancar_tarea).
    almacen = alertas.AlmacenAlertas(
        paradas_madrid=frozenset(cat.estaciones),
        nombre_parada=lambda sid: (cat.estacion(sid) or {}).get("nombre", sid),
    )
    almacen.cargar_ultima_captura()       # ~3 ms: el endpoint ya responde
    almacen.arrancar_tarea_de_fondo()     # backfill + refresco cada 60 s
    estado["alertas"] = almacen

    # Posiciones en vivo para el mapa. Caché propia y tarea propia: un fallo leyendo
    # vehicle_positions no puede dejar sin refrescar el estado de red, que sí está en
    # la ruta crítica de la predicción.
    posiciones = FuentePosiciones(cat)
    posiciones.refrescar()            # el endpoint ya responde desde el primer segundo
    estado["posiciones"] = posiciones

    # Meteorología: cuarta fuente del proyecto y cuarto dominio de fallo. AEMET
    # publica una vez por hora con 1,5-4 h de latencia, una cadencia que no tiene
    # nada que ver con la del feed de RENFE, así que comparte tan poco con el resto
    # que meterla en CacheContexto solo acoplaría fallos.
    fuente_meteo = meteo_mod.FuenteMeteo(cat)
    if not fuente_meteo.refrescar():
        log.error("La meteorología no se pudo cargar en el arranque: %s",
                  fuente_meteo.ultimo_error)
    estado["meteo"] = fuente_meteo

    # Puntualidad histórica. Un JSON de pocos KB generado por
    # scripts/generar_puntualidad.py, cargado una vez. Si no existe, `cargar`
    # devuelve None sin lanzar y el asistente contesta que no puede responder a
    # esas preguntas: el histórico es un extra, no un requisito de arranque.
    ruta_puntualidad = os.getenv(
        "RUTA_PUNTUALIDAD", "/home/tfm/renfe-delay-app/datos/puntualidad.json"
    )
    hist = historico_mod.cargar(ruta_puntualidad)
    estado["historico"] = hist

    # Asistente conversacional (F9). Recibe las MISMAS instancias que sirven a las
    # tres pantallas: no abre ficheros por su cuenta ni mantiene una segunda idea de
    # qué dato está fresco. Si el chat y la pantalla de alertas pudieran discrepar,
    # la discrepancia aparecería justo durante una demo.
    #
    # Se construye SIEMPRE, aunque el interruptor esté a falso. Dos motivos: el coste
    # de memoria es el mismo con y sin él, y así se puede medir; y encender el chat
    # pasa a ser cambiar una variable y reiniciar, sin ninguna rama de arranque
    # distinta que no se haya ejecutado nunca.
    estado["chat"] = chat_mod.AsistenteChat(
        catalogo=cat,
        cache=cache,
        almacen_alertas=almacen,
        fuente_meteo=fuente_meteo,
        fuente_posiciones=posiciones,
        predictor=estado["predictor"],
        historico=hist,
        # Se pasa la función, no su resultado: el diagnóstico tiene que calcularse
        # en el momento de preguntarlo, no en el arranque.
        fn_salud=lambda: salud(),
    )
    log.info(
        "Asistente conversacional: habilitado=%s · modelo=%s · presupuesto %d/día",
        chat_mod.habilitado(), chat_mod.MODELO, chat_mod.PRESUPUESTO_DIA,
    )

    tarea = asyncio.create_task(_refresco_periodico(cache))
    tarea_mapa = asyncio.create_task(_refresco_posiciones(posiciones))
    tarea_meteo = asyncio.create_task(_refresco_meteo(fuente_meteo))
    log.info(
        "API lista. Refresco de contexto cada %d s, de posiciones cada %d s, "
        "de meteorología cada %d s.",
        INTERVALO_REFRESCO_S, INTERVALO_MAPA_S, meteo_mod.INTERVALO_METEO_S,
    )

    yield

    tarea_meteo.cancel()
    tarea_mapa.cancel()
    tarea.cancel()
    estado["alertas"].detener()  # type: ignore[union-attr]
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


# El colector escribe una captura por minuto. Refrescar cada 30 s acota la antigüedad
# percibida sin releer nada de más: si el epoch del último fichero no ha cambiado,
# `refrescar` sale sin abrirlo.
INTERVALO_MAPA_S = 30


async def _refresco_posiciones(fuente: FuentePosiciones) -> None:
    """Refresca la última foto de posiciones en segundo plano, para siempre.

    Separada de `_refresco_periodico` a propósito: el mapa es una pantalla, el estado
    de red es una entrada del modelo. Un fallo en la primera no puede arrastrar a la
    segunda.
    """
    while True:
        try:
            await asyncio.sleep(INTERVALO_MAPA_S)
            await asyncio.to_thread(fuente.refrescar)
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001
            log.exception("Error refrescando posiciones; se reintenta en el siguiente ciclo")


async def _refresco_meteo(fuente) -> None:
    """Refresca la observación de AEMET en segundo plano, para siempre.

    Cada ciclo compara el nombre del último fichero con el ya leído: mientras el
    colector no escriba una captura nueva, el ciclo no abre nada. El coste real es
    un `scandir` cada diez minutos y un parseo de 49 ms una vez por hora.
    """
    while True:
        try:
            await asyncio.sleep(meteo_mod.INTERVALO_METEO_S)
            await asyncio.to_thread(fuente.refrescar)
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001
            log.exception("Error refrescando meteorología; se reintenta en el siguiente ciclo")


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
        allow_origins=["https://cercanias-madrid.es", "https://www.cercanias-madrid.es"],
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

    # Dominio del modelo: se descartan los trenes cuyo trip tardará más de media hora
    # en arrancar. Fuera de ahí el modelo extrapola y devuelve una cifra que no
    # describe nada (ver contrato.ANTELACION_MAXIMA_SALIDA_S). Se prefiere no enseñar
    # un tren antes que enseñarlo con una predicción inventada.
    if trayectos:
        trayectos, fuera_de_dominio = features.filtrar_por_dominio(trayectos, cat, t0)
        if fuera_de_dominio and not aviso:
            aviso = (
                "Solo se muestran los trenes que ya circulan o que salen en la próxima "
                "media hora: son aquellos sobre los que el modelo puede predecir."
                if trayectos else
                "No hay trenes que salgan en la próxima media hora para ese trayecto. "
                "El modelo solo predice sobre trenes que ya circulan o están a punto "
                "de salir."
            )

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
    filas = features.construir_filas(
        tramos, t0, cat.gtfs_version, cache, request_id,
        fuente_meteo=estado["meteo"],
        almacen_alertas=estado["alertas"],
    )

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

            # Líneas fuera del entrenamiento (C9): el modelo predice con la categoría
            # a nulo, así que el número sale del resto del contexto y no describe esa
            # línea. Se anula y se avisa. El horario oficial sigue siendo válido y es
            # lo único que se enseña.
            if contrato.linea_sin_prediccion(tramo.line_id):
                retraso_s = 0.0
                if not aviso:
                    aviso = (
                        f"La {tramo.line_id} está excluida del modelo por obras "
                        f"prolongadas, así que no se predice su retraso. Se muestra "
                        f"el horario oficial."
                    )

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


@app.get("/api/alertas")
def consultar_alertas(linea: str | None = None):
    """Incidencias del día de servicio en curso, clasificadas.

    Sin filtro devuelve todas. Con `?linea=C3` devuelve solo las de esa línea
    más las que no tienen línea identificable (avisos de red).
    """
    almacen: alertas.AlmacenAlertas = estado["alertas"]  # type: ignore[assignment]
    resultado = almacen.estado()

    if linea:
        objetivo = linea.strip().lower()
        resultado["incidencias"] = [
            i for i in resultado["incidencias"]
            if not i["lineas"] or objetivo in [l.lower() for l in i["lineas"]]
        ]
        resultado["filtro_linea"] = linea

    return resultado


@app.get("/api/mapa")
def mapa():
    """Trenes del núcleo de Madrid en circulación ahora mismo.

    El retraso NO se lee aquí: lo sirve la caché de contexto, que ya aplica el umbral
    de plausibilidad y el cruce feed<->catálogo. Un solo sitio decide qué retraso es
    creíble.
    """
    fuente: FuentePosiciones = estado["posiciones"]  # type: ignore[assignment]
    cache: CacheContexto = estado["cache"]  # type: ignore[assignment]

    datos = fuente.estado()
    for tren in datos["trenes"]:
        # OJO: se pasa el trip_id TAL CUAL viene del feed. `estado_propio` aplica
        # `nucleo_trip` por dentro, y esa función NO es idempotente: aplicada dos
        # veces sobre "3053S23573C1" devuelve "1" y el cruce se pierde en silencio.
        propio = cache.estado_propio(tren["trip_id"])
        tren["retraso_s"] = propio["own_delay_s"] if propio else None
        tren["retraso_edad_s"] = propio["own_delay_age_s"] if propio else None

    # no-store: una posición cacheada por el navegador es una posición falsa.
    return JSONResponse(datos, headers={"Cache-Control": "no-store"})


def _salud_alertas() -> dict:
    """Bloque de incidencias para el diagnóstico.

    Se compone aquí y no se delega entero a `AlmacenAlertas.estado()` porque ese
    método serializa el texto de cada aviso, que no pinta nada en una sonda de
    salud. De él se toman solo la frescura del feed y los recuentos.

    Se añade lo que NO está en ninguna otra parte: cuántas líneas tienen
    incidencias en la ventana de 30 minutos que consume el modelo. Es el
    termómetro de que las seis columnas de alerta están llegando de verdad, y
    distingue "el feed va bien y hoy no hay incidencias" de "el feed no llega".
    """
    almacen = estado["alertas"]
    st = almacen.estado()  # type: ignore[attr-defined]
    ventana = almacen.ventana_modelo()  # type: ignore[attr-defined]
    return {
        "feed": st["feed"],
        "resumen": st["resumen"],
        "ventana_modelo": {
            "disponible": ventana is not None,
            "ventana_min": alertas.VENTANA_MODELO_S // 60,
            "lineas_con_incidencia": sorted(ventana) if ventana else [],
        },
    }


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
        "posiciones": estado["posiciones"].estado()["feed"],  # type: ignore[attr-defined]
        "meteo": estado["meteo"].salud(),  # type: ignore[attr-defined]
        "alertas": _salud_alertas(),
        "predictor": {"backend": estado["predictor"].backend_nombre},  # type: ignore[attr-defined]
        # Estado del asistente: si está encendido, con qué versión de modelo y
        # cuánto presupuesto diario queda. Es lo que permite comprobar el
        # interruptor desde fuera sin entrar en la máquina.
        "chat": (
            estado["chat"].diagnostico()  # type: ignore[attr-defined]
            if "chat" in estado else {"habilitado": False}
        ),
    }


@app.post("/api/chat")
def conversar(peticion: ConsultaChat, request: Request):
    """Asistente conversacional sobre el servicio.

    El modelo de lenguaje SOLO clasifica el texto contra un conjunto cerrado de
    intenciones. La respuesta la compone una plantilla de `chat.py` sobre datos
    que vienen del resolutor, del modelo y de las cachés en vivo, así que el
    asistente no puede inventar una hora de llegada ni una incidencia. Ver la
    cabecera de chat.py.
    """
    if not chat_mod.habilitado():
        # 503 y no 404: la ruta existe, el servicio está apagado a propósito. La
        # interfaz distingue las dos cosas y oculta el acceso al asistente.
        raise HTTPException(
            status_code=503,
            detail="El asistente conversacional no está activo.",
        )

    # El servicio escucha en 127.0.0.1 y quien habla con el exterior es Caddy, así
    # que `request.client.host` es siempre la propia máquina. Sin leer la cabecera
    # reenviada, el límite por IP se convertiría en un límite global y bastaría un
    # visitante activo para dejar sin asistente a todos los demás.
    reenviada = request.headers.get("x-forwarded-for", "")
    ip = reenviada.split(",")[0].strip() or (
        request.client.host if request.client else ""
    )

    asistente: chat_mod.AsistenteChat = estado["chat"]  # type: ignore[assignment]
    respuesta = asistente.responder(
        texto=peticion.texto,
        ip=ip,
        historial=[m.model_dump() for m in peticion.historial],
        sesion=peticion.sesion,
    )
    # no-store por el mismo motivo que en /api/mapa: una respuesta cacheada sobre
    # el estado de la red es una respuesta falsa un minuto después.
    return JSONResponse(respuesta, headers={"Cache-Control": "no-store"})


# La web estática se monta al final para que no capture las rutas /api/*.
# En desarrollo puede no existir todavía; no es motivo para no arrancar la API.
if RUTA_WEB.is_dir():
    app.mount("/", StaticFiles(directory=RUTA_WEB, html=True), name="web")
