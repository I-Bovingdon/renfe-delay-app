#!/usr/bin/env python3
"""
robustez_degradacion.py — Qué hace cada pieza cuando su fuente no está.

Instancia los módulos en un proceso APARTE, apuntando a directorios vacíos o a
ficheros corruptos. No toca el servicio en marcha, no escribe en los directorios
de datos y no habla con ninguna API externa. Lo único que escribe está bajo
/tmp y se borra al terminar.

Estos caminos están escritos en el código desde el principio pero casi nunca se
ejercitan: solo se recorren el día que una fuente falla, que es precisamente el
día en que uno no quiere descubrir que no funcionaban.

Uso (VPS, desde la carpeta api/):
    cd /home/tfm/renfe-delay-app/api
    sudo -u tfm /home/tfm/renfe-delay-app/.venv/bin/python \\
        ../tests/robustez_degradacion.py

Se usa el intérprete del entorno virtual porque la última prueba carga el modelo
real y necesita lightgbm.

TFM Cercanías RENFE · UCM · 2026
"""

from __future__ import annotations

import gzip
import json
import os
import shutil
import sys
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

# El script vive en tests/ pero los módulos están en api/. Se añade api/ a la ruta
# de búsqueda para poder importarlos desde donde sea que se lance.
AQUI = Path(__file__).resolve().parent
sys.path.insert(0, str(AQUI.parent / "api"))

# Los módulos registran cada fallo de refresco con WARNING, que es lo correcto en
# producción y ruido aquí: esta batería provoca esos fallos a propósito y ya informa
# ella misma del error. Se silencian para que la salida se pueda leer.
import logging
logging.disable(logging.WARNING)

VERDE, ROJO, AMBAR, FIN = "\033[92m", "\033[91m", "\033[93m", "\033[0m"
resultados: list[tuple[bool, str, str]] = []


def registrar(ok: bool, nombre: str, detalle: str = "") -> None:
    color = VERDE if ok else ROJO
    resultados.append((ok, nombre, detalle))
    print(f"  [{color}{'OK ' if ok else 'FALLO'}{FIN}] {nombre}" +
          (f"  ·  {detalle}" if detalle else ""))


RUTA_CATALOGO = os.getenv("RUTA_CATALOGO", "../datos/catalogo.json")


# =========================================================================== 1 ===
def bloque_meteo():
    print("\n--- 1. Meteorología sin fuente ---")
    import meteo

    tmp = Path(tempfile.mkdtemp(prefix="robustez_meteo_"))
    try:
        class CatFalso:
            estaciones = {"18000": {"lat": 40.4065, "lon": -3.6895}}

        # a) Directorio que no existe
        f = meteo.FuenteMeteo(CatFalso(), tmp / "no_existe")
        ok = f.refrescar() is False
        registrar(ok, "directorio inexistente: refrescar devuelve False, no lanza",
                  str(f.ultimo_error)[:60])
        registrar(f.observacion("18000") is None, "sin datos: la observación es nula")
        salud = f.salud()
        registrar(salud["hay_datos"] is False and salud["vigente"] is False,
                  "la salud lo declara, no lo disimula",
                  f"error: {str(salud['ultimo_error'])[:40]}")

        # b) Directorio que existe pero está vacío
        carpeta = tmp / "vacio" / "2026-09-13"
        carpeta.mkdir(parents=True)
        f2 = meteo.FuenteMeteo(CatFalso(), tmp / "vacio")
        registrar(f2.refrescar() is False, "carpeta sin capturas: refrescar devuelve False",
                  str(f2.ultimo_error)[:50])

        # c) Fichero corrupto: bytes que no son gzip
        (carpeta / "observacion_nacional_20260913T092614Z.json.gz").write_bytes(b"no soy gzip")
        registrar(f2.refrescar() is False, "captura corrupta: refrescar devuelve False",
                  str(f2.ultimo_error)[:50])

        # d) Captura válida pero sin ninguna estación del corredor
        ajenas = [{"idema": "9999", "fint": "2026-09-13T08:00:00+0000",
                   "lat": 41.0, "lon": -2.0, "ta": 5.0, "prec": 0.0, "vv": 1.0}]
        ruta = carpeta / "observacion_nacional_20260913T102614Z.json.gz"
        with gzip.open(ruta, "wt", encoding="utf-8") as fh:
            json.dump(ajenas, fh)
        ok = f2.refrescar() is False
        registrar(ok, "captura sin estaciones del corredor: se rechaza",
                  str(f2.ultimo_error)[:60])

        # e) Conserva el último dato bueno ante un fallo posterior
        buenas = [{"idema": "3195", "fint": "2026-09-13T08:00:00+0000",
                   "lat": 40.4114, "lon": -3.6781, "ta": 20.1, "prec": 0.0, "vv": 2.6}]
        ruta2 = carpeta / "observacion_nacional_20260913T112614Z.json.gz"
        with gzip.open(ruta2, "wt", encoding="utf-8") as fh:
            json.dump(buenas, fh)
        meteo.MARGEN_FEED_CADUCO_S = 10 ** 9      # que no caduque por ser de otro día
        f2.refrescar()
        habia = f2.observacion("18000") is not None
        (carpeta / "observacion_nacional_20260913T122614Z.json.gz").write_bytes(b"roto")
        f2.refrescar()
        sigue = f2.observacion("18000") is not None
        registrar(habia and sigue,
                  "un fallo posterior NO borra la última foto válida")

        # f) Caducidad: lo que decide es la antigüedad, no el error
        meteo.MARGEN_FEED_CADUCO_S = 1
        registrar(f2.observacion("18000") is None,
                  "dato viejo: deja de servirse aunque no haya habido error")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


# =========================================================================== 2 ===
def bloque_alertas():
    print("\n--- 2. Incidencias sin fuente ---")
    import alertas

    al = alertas.AlmacenAlertas(paradas_madrid=frozenset({"18000"}))

    registrar(al.ventana_modelo() is None,
              "almacén vacío: la ventana del modelo es nula, no un cero falso")

    ahora = datetime.now(timezone.utc)
    al._indice = {
        "A1": {"id": "A1", "texto": "#MadC7 Avería en la infraestructura",
               "tipo": "AVERIA", "tipo_modelo": "AVERIA", "impacto": "ALTO",
               "planificada": False, "accesibilidad": False, "lineas": ["C7"],
               "paradas": [], "inicio_declarado": None,
               "primera_vez": ahora - timedelta(minutes=40),
               "ultima_vez": ahora - timedelta(minutes=1)},
    }

    al._ultima_captura = ahora - timedelta(minutes=30)
    registrar(al.ventana_modelo() is None,
              "feed caduco con datos en memoria: sigue devolviendo nulo",
              "la antigüedad manda sobre el contenido")

    al._ultima_captura = ahora - timedelta(seconds=30)
    v = al.ventana_modelo()
    registrar(v is not None and v["C7"]["alert_averia_30m"] == 1,
              "feed fresco: la ventana vuelve a estar disponible")

    # Una línea sin incidencias recibe ceros, nunca nulos: el modelo no vio nulos aquí.
    cero = alertas.ALERTAS_CERO
    registrar(all(valor == 0 for valor in cero.values()) and len(cero) == 6,
              "línea sin incidencias: seis ceros, ningún nulo", str(list(cero)))


# =========================================================================== 3 ===
def bloque_fila_degradada():
    print("\n--- 3. Fila de features con TODO el contexto caído ---")
    from catalogo import Catalogo
    from contrato import REQUIRED_FEATURES, validate_row
    from resolver import resolver_trayecto
    from tiempo import ahora_utc
    import features

    cat = Catalogo(RUTA_CATALOGO)
    ahora = ahora_utc()
    trayectos, aviso = resolver_trayecto(cat, "18000", "70103", ahora)
    if not trayectos:
        registrar(False, "no hay trayectos de prueba a esta hora", str(aviso))
        return

    tramo = trayectos[0].tramos[0]

    # Peor caso imaginable: las cuatro fuentes de contexto caídas a la vez.
    fila = features.construir_fila(
        tramo=tramo, t0_utc=ahora, gtfs_version=cat.gtfs_version,
        estado_linea=None, meteo=None, alertas=None, estado_propio=None,
    )

    registrar(True, "construir_fila no lanza con todo el contexto a nulo")

    esperados = {"estado_red", "meteo", "alertas"}
    degradados = set(fila["degraded_blocks"])
    registrar(esperados <= degradados, "declara los tres bloques degradados",
              str(sorted(degradados)))

    faltan = [f for f in REQUIRED_FEATURES if fila.get(f) is None]
    registrar(not faltan, "las features obligatorias siguen completas", str(faltan))

    try:
        validate_row(fila, strict_unknown=False)
        registrar(True, "la fila degradada sigue cumpliendo el contrato")
    except Exception as exc:  # noqa: BLE001
        registrar(False, "la fila degradada sigue cumpliendo el contrato", str(exc)[:70])

    # La regla que distingue alertas de meteorología.
    alertas_cero = all(fila.get(c) == 0 for c in (
        "alerts_line_30m", "alert_supresion_30m", "alert_averia_30m",
        "alert_servicio_bus_30m", "alert_obras_30m", "alert_retraso_30m"))
    registrar(alertas_cero,
              "con el feed caído, las alertas van a CERO (el modelo no vio nulos)")
    meteo_nula = all(fila.get(c) is None for c in
                     ("temp_c", "precip_mm_1h", "wind_speed_ms"))
    registrar(meteo_nula,
              "con el feed caído, la meteo va a NULO (el modelo sí vio nulos)")


# =========================================================================== 4 ===
def bloque_horas_frontera():
    print("\n--- 4. Horas frontera del día de servicio ---")
    from catalogo import Catalogo
    from resolver import resolver_trayecto
    from tiempo import MADRID, fecha_de_servicio, formatear_local

    cat = Catalogo(RUTA_CATALOGO)
    hoy = datetime.now(MADRID).date()

    casos = [
        (23, 50, "23:50, los trenes de después de medianoche"),
        (0, 30, "00:30, pertenece al día de servicio anterior"),
        (3, 0, "03:00, sin servicio"),
        (5, 30, "05:30, primeros trenes"),
        (8, 0, "08:00, hora punta"),
    ]
    for hora, minuto, etiqueta in casos:
        momento = datetime(hoy.year, hoy.month, hoy.day, hora, minuto,
                           tzinfo=MADRID).astimezone(timezone.utc)
        try:
            trayectos, aviso = resolver_trayecto(cat, "18000", "70103", momento)
            fserv = fecha_de_servicio(momento)
            detalle = f"{len(trayectos)} trayectos · día de servicio {fserv}"
            if trayectos:
                t = trayectos[0].tramos[0]
                detalle += (f" · primero {formatear_local(t.salida_teorica_utc)}"
                            f"->{formatear_local(t.llegada_teorica_utc)}"
                            f" · régimen {t.regime}")
            elif aviso:
                detalle += f" · {aviso[:45]}"
            registrar(True, etiqueta, detalle)
        except Exception as exc:  # noqa: BLE001
            registrar(False, etiqueta, f"{type(exc).__name__}: {exc}")


# =========================================================================== 5 ===
def bloque_predictor():
    print("\n--- 5. El modelo no responde ---")
    from predictor import _BackendHTTP, PredictorError

    # Puerto cerrado a propósito: simula el backend caído sin tocar el real.
    backend = _BackendHTTP("http://127.0.0.1:9/invocations", nombre="inexistente")
    try:
        backend.score([{"line_id": "C7"}])
        registrar(False, "backend caído: debería lanzar PredictorError")
    except PredictorError as exc:
        registrar(True, "backend caído: lanza PredictorError, no cuelga",
                  str(exc)[:55])
    except Exception as exc:  # noqa: BLE001
        registrar(False, "backend caído: excepción inesperada",
                  f"{type(exc).__name__}")


# =========================================================================== 6 ===
def bloque_catalogo_roto():
    print("\n--- 6. Catálogo ilegible ---")
    from catalogo import Catalogo

    tmp = Path(tempfile.mkdtemp(prefix="robustez_cat_"))
    try:
        roto = tmp / "catalogo.json"
        roto.write_text("{esto no es json", encoding="utf-8")
        try:
            Catalogo(roto)
            registrar(False, "catálogo corrupto: debería fallar al cargar")
        except Exception as exc:  # noqa: BLE001
            registrar(True, "catálogo corrupto: falla ruidosamente al cargar",
                      type(exc).__name__)

        formato_malo = tmp / "formato.json"
        formato_malo.write_text(json.dumps({"formato_trips": ["otra", "cosa"]}),
                                encoding="utf-8")
        try:
            Catalogo(formato_malo)
            registrar(False, "formato inesperado: debería fallar")
        except ValueError as exc:
            registrar(True, "formato inesperado: error explicativo, no críptico",
                      str(exc)[:55])
        except Exception as exc:  # noqa: BLE001
            registrar(False, "formato inesperado: excepción poco clara",
                      type(exc).__name__)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


# =========================================================================== 7 ===
def bloque_linea_no_entrenada():
    print("\n--- 7. Consulta de la C9, que el modelo no conoce ---")
    ruta = os.getenv("RUTA_MODELO_PKL", "/home/tfm/modelo/modelo_retrasos.pkl")
    if not Path(ruta).exists():
        registrar(False, "no se encuentra el modelo", ruta)
        return
    try:
        from adaptador_modelo import ModeloRetrasos
        from contrato import empty_row
    except ImportError as exc:
        registrar(False, "no se ha podido importar el adaptador",
                  f"{exc} · ¿se ha lanzado con el python del entorno virtual?")
        return

    m = ModeloRetrasos(ruta)
    registrar("C9" not in m.lineas_conocidas,
              "el modelo efectivamente NO conoce la C9",
              f"{len(m.lineas_conocidas)} líneas entrenadas")

    fila = empty_row()
    fila.update({
        "trip_id": "prueba", "service_date": "2026-09-18",
        "t0_utc": "2026-09-18T06:30:00Z", "sched_arrival_utc": "2026-09-18T07:12:00Z",
        "gtfs_version": "x", "line_id": "C9", "origin_stop_id": "1", "dest_stop_id": "2",
        "dest_stop_sequence": 5, "trip_total_stops": 10, "stops_to_dest": 4,
        "horizon_s": 2520, "dow": 4, "hour_local": 9, "minute_of_day_local": 552,
        "is_weekend": False, "is_holiday": False, "is_school_period": True,
        "regime": "B", "alerts_line_30m": 1, "alert_servicio_bus_30m": 1,
        "alert_supresion_30m": 0, "alert_averia_30m": 0,
        "alert_obras_30m": 0, "alert_retraso_30m": 0,
    })
    salida = m.predict([fila])[0]
    registrar(salida["delay_s_p50"] is not None,
              "la C9 se predice igualmente con el resto del contexto",
              f"{salida['delay_s_p50']:.0f} s")
    registrar(any("linea_no_entrenada" in a for a in salida["avisos"]),
              "y queda el aviso explícito de línea no entrenada",
              str(salida["avisos"]))


def main() -> int:
    print("=" * 78)
    print("PRUEBAS DE DEGRADACIÓN · módulos aislados, sin tocar el servicio")
    print("=" * 78)

    for bloque in (bloque_meteo, bloque_alertas, bloque_fila_degradada,
                   bloque_horas_frontera, bloque_predictor,
                   bloque_catalogo_roto, bloque_linea_no_entrenada):
        try:
            bloque()
        except Exception as exc:  # noqa: BLE001 — un bloque roto no tumba la batería
            registrar(False, f"el bloque {bloque.__name__} se ha interrumpido",
                      f"{type(exc).__name__}: {exc}")

    print("\n" + "=" * 78)
    n_ok = sum(1 for ok, _, _ in resultados if ok)
    n_ko = len(resultados) - n_ok
    print(f"RESUMEN: {n_ok} OK · {n_ko} fallos  (de {len(resultados)})")
    if n_ko:
        print("\nPruebas en rojo:")
        for ok, nombre, detalle in resultados:
            if not ok:
                print(f"  · {nombre}  ·  {detalle}")
    print("=" * 78)
    return 1 if n_ko else 0


if __name__ == "__main__":
    sys.exit(main())
