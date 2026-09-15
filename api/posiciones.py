"""
posiciones.py — Posiciones de los trenes en circulación, para el mapa de la red.

Lee la ÚLTIMA captura de raw/vehicle_positions/ (el Parquet del día no existe hasta la
compactación de la madrugada siguiente) y devuelve los trenes del núcleo de Madrid con
su línea, su dirección y su rumbo.

TRES COSAS QUE EL FEED NO DA Y HAY QUE DERIVAR

  1. `bearing`, `speed` y `currentStopSequence` vienen 100% nulos, verificado sobre los
     Parquet reales. La dirección se DERIVA del recorrido del catálogo: es el rumbo
     del TRAMO de vía que el tren está recorriendo, de estación a estación.

     CORRECCIÓN DEL 14/09/2026. Antes el rumbo se calculaba desde la posición GPS del
     tren hasta una parada. Eso hacía depender la flecha del dato menos fiable del
     feed: emparejando capturas consecutivas por `tripId`, 20 de 37 desplazamientos
     implicaban velocidades imposibles para Cercanías. Y cuando el tren está entrando
     en una estación, la separación entre su posición y esa parada es de decenas de
     metros (mediana de 34 m), así que el ángulo lo decidía el error de posición y la
     flecha apuntaba hacia atrás con frecuencia.

     Ahora la posición GPS NO interviene en el rumbo. Se toma el segmento
     `paradas[i] -> paradas[i+1]` del recorrido, con las coordenadas exactas del
     catálogo. Dos propiedades lo hacen robusto:

       · El orden de las paradas está verificado: de los 36.548 trips del catálogo,
         cero tienen llegadas no crecientes, cero paradas repetidas y cero
         incoherencias entre salida y llegada (medido el 14/09).
       · Un error de una parada en el índice apenas mueve el resultado, porque dos
         tramos consecutivos de vía son casi colineales. Así que da igual si el
         `stopId` del feed es la parada siguiente o la recién dejada: la dirección
         de avance es la misma.

     La posición GPS se sigue usando para SITUAR el tren, que es para lo que sirve y
     donde es buena: antigüedad mediana de 4 s y máxima de 5 s, medido el 14/09.

  2. `route_id` viene 100% nulo. La línea exacta (incluida la rama a/b, que el sufijo del
     trip_id no distingue) sale del catálogo cruzando por núcleo del trip_id. Solo si el
     trip no casa se recurre al sufijo, y entonces la rama queda sin resolver.

  3. La estación de destino no viaja en el feed. Es la última parada del recorrido en el
     catálogo. Es el dato que de verdad desambigua dos trenes en el mismo andén: la
     flecha es refuerzo visual, el texto es la información.

POR QUÉ UNA CAPTURA Y NO UNA VENTANA. Al contrario que FuenteRaw, que agrega 30 minutos
para calcular el estado de la línea, aquí interesa dónde está cada tren AHORA. Leer un
único fichero de decenas de KB por ciclo es coste despreciable.

EL RETRASO NO SE LEE AQUÍ. Lo sirve la caché de contexto que ya existe
(`CacheContexto.estado_propio`), que lo extrae de trip_updates. Duplicar esa lectura
sería duplicar también el criterio de plausibilidad y el cruce por núcleo.

TFM Cercanías RENFE · UCM · 2026
"""

from __future__ import annotations

import gzip
import json
import logging
import math
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

# Se reutilizan los helpers de fuente_raw a propósito: la derivación de la línea desde
# el trip_id tiene que ser LA MISMA en el estado de red y en el mapa. Dos regex
# equivalentes en dos módulos son dos regex que acabarán divergiendo.
from estado_red import nucleo_trip
from fuente_raw import _epoch_de_nombre, _lineas_del_trip, _mapa_lineas
from tiempo import ahora_utc, iso_utc

log = logging.getLogger(__name__)

DIR_VEHICLE_POSITIONS = Path(
    os.getenv("DIR_VEHICLE_POSITIONS", "/home/tfm/data-renfe/raw/vehicle_positions")
)

# Mismo umbral que la pantalla de alertas: por encima de esto el dato deja de
# presentarse como situación actual.
MARGEN_FEED_CADUCO_S = 10 * 60

# Estados que publica el feed. IN_TRANSIT_TO e INCOMING_AT son "en marcha" a efectos
# de dibujo; solo STOPPED_AT se pinta como detenido.
ESTADO_PARADO = "STOPPED_AT"

# Franja en la que se espera que circulen trenes. Es la MISMA que usa el watchdog de
# contenido del colector, para que colector y mapa consideren anormal lo mismo.
# Comprobado sobre el feed real: de madrugada el payload viene con CERO entidades a
# nivel nacional, así que un payload vacío solo es sospechoso dentro de esta franja.
HORA_INICIO_SERVICIO = 6
HORA_FIN_SERVICIO = 23
MADRID = ZoneInfo("Europe/Madrid")


def en_horario_de_servicio(momento: datetime) -> bool:
    hora = momento.astimezone(MADRID).hour
    return HORA_INICIO_SERVICIO <= hora < HORA_FIN_SERVICIO


# Separación mínima entre los dos extremos del tramo para que su rumbo signifique
# algo. Las estaciones consecutivas de la red están a cientos de metros o a varios
# kilómetros, así que este umbral no descarta tramos reales: solo protege del caso
# degenerado de dos paradas con coordenadas coincidentes en el catálogo.
DISTANCIA_MINIMA_TRAMO_M = 50.0


def distancia_m(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """Distancia aproximada en metros. Proyección plana con corrección de coseno.

    Sobre las decenas de kilómetros del núcleo de Madrid el error frente a la
    fórmula esférica es muy inferior al de cualquier dato que manejamos.
    """
    dy = (lat2 - lat1) * 111320.0
    dx = (lon2 - lon1) * 111320.0 * math.cos(math.radians((lat1 + lat2) / 2.0))
    return math.hypot(dx, dy)


def rumbo_grados(lat1: float, lon1: float, lat2: float, lon2: float) -> float | None:
    """Rumbo inicial ortodrómico de un punto a otro. 0 = norte, 90 = este.

    Devuelve None si los dos puntos están más cerca de DISTANCIA_MINIMA_TRAMO_M.
    El guardarraíl anterior comparaba 1e-6 grados, que son unos 11 cm, y no
    filtraba nada en la práctica.
    """
    if distancia_m(lat1, lon1, lat2, lon2) < DISTANCIA_MINIMA_TRAMO_M:
        return None
    f1, f2 = math.radians(lat1), math.radians(lat2)
    dl = math.radians(lon2 - lon1)
    y = math.sin(dl) * math.cos(f2)
    x = math.cos(f1) * math.sin(f2) - math.sin(f1) * math.cos(f2) * math.cos(dl)
    return round((math.degrees(math.atan2(y, x)) + 360.0) % 360.0, 1)


class FuentePosiciones:
    """Última foto de los trenes de Madrid en circulación.

    La frescura se evalúa en el momento de la petición, no al refrescar: así el estado
    caduca solo aunque el colector deje de escribir y nadie vuelva a llamar a
    `refrescar`. Es el mismo criterio de la pantalla de alertas.
    """

    def __init__(self, catalogo: Any, directorio: Path | str = DIR_VEHICLE_POSITIONS):
        self.cat = catalogo
        self.paradas_madrid = frozenset(catalogo.estaciones)
        self.directorio = Path(directorio)
        self._mapa_lineas = _mapa_lineas([l["line_id"] for l in catalogo.lineas])

        self._trenes: list[dict[str, Any]] = []
        self._captura_utc: datetime | None = None
        self._epoch_leido: int | None = None
        self._entidades_ultima: int = 0
        self._sin_casar: int = 0
        self.ultimo_error: str | None = None

    # ------------------------------------------------------------- ficheros ---
    def _ultimo_fichero(self) -> tuple[Path, int] | None:
        """Fichero de captura más reciente, por el epoch del nombre.

        Se miran las dos carpetas de fecha más recientes porque la carpeta cambia a
        medianoche UTC, que en horario de verano son las 02:00 de Madrid: a esa hora
        todavía hay trenes y el último fichero puede seguir en la carpeta anterior.
        """
        if not self.directorio.is_dir():
            raise FileNotFoundError(f"No existe {self.directorio}")
        carpetas = sorted(
            (d for d in self.directorio.iterdir() if d.is_dir()), key=lambda d: d.name
        )[-2:]

        mejor: tuple[Path, int] | None = None
        for carpeta in carpetas:
            with os.scandir(carpeta) as entradas:
                for e in entradas:
                    epoch = _epoch_de_nombre(e.name)
                    if epoch is not None and (mejor is None or epoch > mejor[1]):
                        mejor = (Path(e.path), epoch)
        return mejor

    # -------------------------------------------------------------- lectura ---
    def _parsear(self, ruta: Path) -> list[dict[str, Any]]:
        with gzip.open(ruta, "rt", encoding="utf-8") as fh:
            doc = json.load(fh)

        entidades = (doc.get("payload") or {}).get("entity") or []
        self._entidades_ultima = len(entidades)

        fecha_servicio = None
        try:
            from tiempo import fecha_de_servicio

            fecha_servicio = fecha_de_servicio(ahora_utc())
        except Exception:  # noqa: BLE001 — sin fecha se resuelve igual, solo peor
            pass

        por_vehiculo: dict[str, dict[str, Any]] = {}
        sin_casar = 0

        for entidad in entidades:
            v = entidad.get("vehicle") or {}
            pos = v.get("position") or {}
            lat, lon = pos.get("latitude"), pos.get("longitude")
            if lat is None or lon is None:
                continue

            # Filtro de núcleo TOPOLÓGICO, igual que en fuente_raw: la parada que
            # publica el feed tiene que ser una de las 95 del catálogo de Madrid.
            # Descartado el bounding box de coordenadas: dejaba fuera Guadalajara.
            stop_id = str(v.get("stopId") or "")
            if stop_id not in self.paradas_madrid:
                continue

            trip_id = str((v.get("trip") or {}).get("tripId") or "")
            descriptor = v.get("vehicle") or {}      # anidado con el mismo nombre
            etiqueta = str(descriptor.get("label") or "")
            vehiculo_id = str(descriptor.get("id") or entidad.get("id") or trip_id)
            estado = str(v.get("currentStatus") or "")
            lat, lon = float(lat), float(lon)

            # --- Línea, recorrido y destino, vía catálogo ---
            idx = self.cat.trip_por_nucleo(nucleo_trip(trip_id), fecha_servicio)
            if idx is not None:
                line_id = self.cat.linea_de_trip(idx)
                paradas = self.cat.paradas_de(idx)
                sentido = self.cat.sentido_de_trip(idx)
            else:
                # Respaldo: el sufijo del trip_id. No distingue rama (C4 vs C4a/C4b),
                # así que se toma la primera y se cuenta como no casado para poder
                # medir cuántos trenes quedan sin resolver.
                candidatas = _lineas_del_trip(trip_id, self._mapa_lineas)
                line_id = candidatas[0] if candidatas else None
                paradas, sentido = [], None
                sin_casar += 1

            destino = None
            if paradas:
                ultima = self.cat.estacion(paradas[-1])
                destino = ultima["nombre"] if ultima else None

            # --- Rumbo del TRAMO de recorrido. La posición GPS no interviene ---
            # Se toma el segmento de vía en el que está el tren, de estación a
            # estación, y se usa su rumbo. En la última parada del recorrido no hay
            # tramo siguiente, así que se usa el anterior: el tren llegó por ahí y
            # ese es su sentido de avance. Ver la corrección del 14/09 en la cabecera.
            rumbo = None
            siguiente = None     # parada hacia la que avanza un tren en marcha
            if paradas and stop_id in paradas:
                i = paradas.index(stop_id)
                if i + 1 < len(paradas):
                    desde, hasta = paradas[i], paradas[i + 1]
                    siguiente = hasta
                elif i > 0:
                    desde, hasta = paradas[i - 1], paradas[i]
                else:
                    desde = hasta = None          # recorrido de una sola parada
                if desde and hasta:
                    a, b = self.cat.estacion(desde), self.cat.estacion(hasta)
                    if a and b:
                        rumbo = rumbo_grados(a["lat"], a["lon"], b["lat"], b["lon"])

            # Qué parada se nombra en la ficha del mapa. Si el tren está detenido,
            # el stopId es la estación donde está. Si está en marcha, el stopId es la
            # parada que ACABA DE DEJAR (174° de desviación mediana, medición del
            # 14/09), así que se nombra la siguiente del recorrido, que es la misma
            # que orienta la flecha. Sin recorrido casado no se nombra ninguna y la
            # interfaz muestra el texto genérico.
            if estado == ESTADO_PARADO:
                est_actual = self.cat.estacion(stop_id)
            else:
                est_actual = self.cat.estacion(siguiente) if siguiente else None
            por_vehiculo[vehiculo_id] = {
                "id": vehiculo_id,
                "trip_id": trip_id,
                "linea": line_id,
                "lat": round(lat, 5),
                "lon": round(lon, 5),
                "rumbo": rumbo,
                "parado": estado == ESTADO_PARADO,
                "parada": est_actual["nombre"] if est_actual else None,
                "destino": destino,
                "sentido": sentido,
                "etiqueta": etiqueta,
            }

        self._sin_casar = sin_casar
        return sorted(por_vehiculo.values(), key=lambda t: (t["linea"] or "~", t["id"]))

    def refrescar(self) -> bool:
        """Relee la última captura si hay una nueva. Nunca lanza excepción.

        Devuelve si el ciclo fue bien. Ante un fallo se CONSERVA la última foto
        válida: lo que decide si el dato sirve es su antigüedad, no que haya habido
        un error, igual que en CacheContexto.
        """
        try:
            ultimo = self._ultimo_fichero()
            if ultimo is None:
                raise RuntimeError(f"Sin capturas en {self.directorio}")
            ruta, epoch = ultimo
            if epoch == self._epoch_leido:
                return True  # el colector aún no ha escrito una captura nueva

            self._trenes = self._parsear(ruta)
            self._captura_utc = datetime.fromtimestamp(epoch, tz=timezone.utc)
            self._epoch_leido = epoch
            self.ultimo_error = None
            return True
        except Exception as exc:  # noqa: BLE001 — el refresco nunca tumba la API
            self.ultimo_error = str(exc)
            log.warning("Fallo al refrescar posiciones: %s", exc)
            return False

    # ---------------------------------------------------------------- salida ---
    def estado(self, ahora: datetime | None = None) -> dict[str, Any]:
        """Payload del endpoint, con la frescura calculada en este instante."""
        ahora = ahora or ahora_utc()

        if self._captura_utc is None:
            estado_feed, antiguedad = "SIN_DATOS", None
        else:
            antiguedad = (ahora - self._captura_utc).total_seconds()
            if antiguedad > MARGEN_FEED_CADUCO_S:
                estado_feed = "CADUCO"
            elif self._entidades_ultima == 0 and en_horario_de_servicio(ahora):
                # Captura fresca y payload vacío EN HORARIO: es el fallo del emisor
                # documentado (12/07, HTTP 200 sin contenido durante 29 h). Fuera de
                # horario el feed viene vacío de forma legítima —verificado a las
                # 00:45 del 13/09— y eso sale como OK con la lista de trenes vacía.
                # Sin esta distinción, el mapa acusaría a RENFE todas las noches.
                estado_feed = "EMISOR_VACIO"
            else:
                estado_feed = "OK"

        return {
            "feed": {
                "estado": estado_feed,
                "ultima_captura": iso_utc(self._captura_utc) if self._captura_utc else None,
                "antiguedad_s": round(antiguedad) if antiguedad is not None else None,
                "entidades_feed": self._entidades_ultima,
                "sin_casar": self._sin_casar,
                "ultimo_error": self.ultimo_error,
            },
            "n_trenes": len(self._trenes),
            "trenes": self._trenes,
        }


if __name__ == "__main__":
    # Prueba en seco. Se ejecuta en el VPS desde la carpeta api/, SIN reiniciar el
    # servicio: solo lee ficheros.
    import sys

    from catalogo import Catalogo

    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    cat = Catalogo(sys.argv[1] if len(sys.argv) > 1 else "../datos/catalogo.json")
    fuente = FuentePosiciones(cat)
    fuente.refrescar()
    st = fuente.estado()

    print(f"feed: {st['feed']}")
    print(f"trenes de Madrid: {st['n_trenes']}")
    for t in st["trenes"][:10]:
        flecha = f"{t['rumbo']:5.1f}°" if t["rumbo"] is not None else "  -  "
        print(
            f"  {t['linea'] or '??':<4} {t['etiqueta']:<16} "
            f"{'parado en' if t['parado'] else 'hacia   '} {t['parada']:<22} "
            f"dir. {t['destino'] or '?':<22} rumbo {flecha}"
        )
