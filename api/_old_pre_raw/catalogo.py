"""
catalogo.py — Catálogo de la red cargado en memoria.

Carga catalogo.json UNA sola vez al arrancar el proceso y construye los índices que la
API necesita para responder en milisegundos: búsqueda de estaciones por nombre, trenes
que paran en cada estación y calendario de servicio por fecha.

Por qué en memoria y no en disco: la consulta del usuario no puede tocar disco. Medido,
el catálogo ocupa unos 116 MB de proceso, holgado frente al límite de 512 MB con el que
se ejecutará el servicio para no interferir con la captura 24/7.

TFM Cercanías RENFE · UCM · 2026
"""

from __future__ import annotations

import json
import sys
import unicodedata
from collections import defaultdict
from datetime import date
from pathlib import Path
from typing import Any, NamedTuple

from tiempo import fecha_de_servicio, ahora_utc

# Posiciones dentro de cada trip del formato compacto. Se leen del propio catálogo
# ('formato_trips') para que un cambio de formato falle de forma ruidosa, no silenciosa.
_FORMATO_ESPERADO = [
    "trip_id", "line_id", "service_id", "direction_id",
    "stop_ids", "llegadas_s", "salidas_s",
]


class Conexion(NamedTuple):
    """Un tren que une dos estaciones en el orden pedido."""
    idx_trip: int          # posición del trip en self.trips
    trip_id: str
    line_id: str
    service_id: str
    i_origen: int          # índice de la parada de origen dentro del recorrido
    i_destino: int         # índice de la parada de destino
    salida_s: int          # segundos del día de servicio, salida del origen
    llegada_s: int         # segundos del día de servicio, llegada al destino


def normalizar(texto: str) -> str:
    """'Príncipe Pío' -> 'principe pio'. Para buscar sin acentos ni mayúsculas.

    Nadie escribe los acentos en el buscador de un móvil, y varias estaciones de la red
    los llevan (Chamartín, Méndez Álvaro, Alcalá de Henares).
    """
    sin_acentos = unicodedata.normalize("NFKD", texto)
    sin_acentos = "".join(c for c in sin_acentos if not unicodedata.combining(c))
    return sin_acentos.lower().strip()


class Catalogo:
    """Catálogo de la red en memoria, con sus índices."""

    def __init__(self, ruta: str | Path):
        ruta = Path(ruta)
        with open(ruta, encoding="utf-8") as f:
            datos: dict[str, Any] = json.load(f)

        formato = datos.get("formato_trips", [])
        if formato != _FORMATO_ESPERADO:
            raise ValueError(
                f"El formato de trips del catálogo no es el esperado.\n"
                f"  esperado: {_FORMATO_ESPERADO}\n  encontrado: {formato}\n"
                f"Regenera el catálogo con la versión actual de generar_catalogo.py."
            )
        (self._I_TRIP, self._I_LINEA, self._I_SERVICIO, self._I_SENTIDO,
         self._I_STOPS, self._I_LLEGADAS, self._I_SALIDAS) = range(7)

        self.gtfs_version: str = datos["gtfs_version"]
        self.generado_utc: str = datos["generado_utc"]
        self.trips: list[list[Any]] = datos["trips"]
        self.lineas: list[dict[str, Any]] = datos["lineas"]
        self.servicios: dict[str, dict[str, Any]] = datos["servicios"]

        self.estaciones: dict[str, dict[str, Any]] = {
            e["stop_id"]: e for e in datos["estaciones"]
        }

        # --- Índice de búsqueda por nombre normalizado ---
        self._busqueda: list[tuple[str, str]] = [
            (normalizar(e["nombre"]), e["stop_id"]) for e in datos["estaciones"]
        ]

        # --- Índice parada -> trips que paran en ella ---
        # sys.intern sobre los stop_id: solo hay 95 cadenas distintas repetidas ~564.000
        # veces. Internarlas hace que todas apunten al mismo objeto y ahorra memoria.
        self.trips_por_parada: dict[str, list[int]] = defaultdict(list)
        for idx, trip in enumerate(self.trips):
            trip[self._I_STOPS] = [sys.intern(s) for s in trip[self._I_STOPS]]
            for sid in set(trip[self._I_STOPS]):
                self.trips_por_parada[sid].append(idx)

        # --- Caché de servicios activos por fecha ---
        # Se consulta en cada petición y solo hay unas pocas fechas distintas en juego.
        self._cache_servicios: dict[date, frozenset[str]] = {}

    # ------------------------------------------------------------------ estaciones ---
    def estacion(self, stop_id: str) -> dict[str, Any] | None:
        return self.estaciones.get(stop_id)

    def listar_estaciones(self) -> list[dict[str, Any]]:
        """Todas las estaciones, ordenadas alfabéticamente para el selector."""
        return sorted(self.estaciones.values(), key=lambda e: normalizar(e["nombre"]))

    def buscar_estaciones(self, texto: str, limite: int = 8) -> list[dict[str, Any]]:
        """Búsqueda por prefijo o subcadena, sin acentos ni mayúsculas.

        Se priorizan las coincidencias por el principio del nombre: quien escribe 'cha'
        busca Chamartín, no una estación que lleve 'cha' en mitad del nombre.
        """
        q = normalizar(texto)
        if not q:
            return []
        prefijo, contiene = [], []
        for nombre, stop_id in self._busqueda:
            if nombre.startswith(q):
                prefijo.append(stop_id)
            elif q in nombre:
                contiene.append(stop_id)
        return [self.estaciones[s] for s in (prefijo + contiene)[:limite]]

    # ------------------------------------------------------------------- calendario ---
    def servicios_activos(self, fecha_servicio: date) -> frozenset[str]:
        """service_id que circulan en una fecha de servicio dada.

        El GTFS de RENFE no publica calendar_dates.txt, así que no hay excepciones: la
        pertenencia se decide solo por el patrón semanal y el rango de validez. Como
        consecuencia, los festivos NO los conoce el GTFS y deben tratarse aparte.
        """
        if fecha_servicio in self._cache_servicios:
            return self._cache_servicios[fecha_servicio]

        clave = fecha_servicio.strftime("%Y%m%d")
        dia_semana = fecha_servicio.weekday()  # 0 = lunes, igual que en el catálogo
        activos = {
            sid
            for sid, s in self.servicios.items()
            if s["dias"][dia_semana] == 1 and s["desde"] <= clave <= s["hasta"]
        }
        resultado = frozenset(activos)
        self._cache_servicios[fecha_servicio] = resultado
        return resultado

    # -------------------------------------------------------------------- conexiones ---
    def conexiones_directas(
        self,
        origen: str,
        destino: str,
        fecha_servicio: date | None = None,
    ) -> list[Conexion]:
        """Trenes que llevan de 'origen' a 'destino' sin transbordo, en ese orden.

        Devuelve la lista completa del día, sin filtrar por hora: quien filtra por hora
        es el resolutor, que además decide cuántos candidatos mostrar.
        """
        if origen == destino:
            return []
        idx_origen = set(self.trips_por_parada.get(origen, ()))
        idx_destino = set(self.trips_por_parada.get(destino, ()))
        candidatos = idx_origen & idx_destino
        if not candidatos:
            return []

        activos = self.servicios_activos(fecha_servicio) if fecha_servicio else None

        conexiones: list[Conexion] = []
        for idx in candidatos:
            trip = self.trips[idx]
            if activos is not None and trip[self._I_SERVICIO] not in activos:
                continue
            paradas = trip[self._I_STOPS]
            i_o = paradas.index(origen)
            i_d = paradas.index(destino)
            if i_o >= i_d:
                continue  # el tren pasa por ambas, pero en sentido contrario
            conexiones.append(
                Conexion(
                    idx_trip=idx,
                    trip_id=trip[self._I_TRIP],
                    line_id=trip[self._I_LINEA],
                    service_id=trip[self._I_SERVICIO],
                    i_origen=i_o,
                    i_destino=i_d,
                    salida_s=trip[self._I_SALIDAS][i_o],
                    llegada_s=trip[self._I_LLEGADAS][i_d],
                )
            )
        conexiones.sort(key=lambda c: c.salida_s)
        return conexiones

    def paradas_de(self, idx_trip: int) -> list[str]:
        return self.trips[idx_trip][self._I_STOPS]


if __name__ == "__main__":
    import time as _time

    ruta = sys.argv[1] if len(sys.argv) > 1 else "../datos/catalogo.json"

    t0 = _time.perf_counter()
    cat = Catalogo(ruta)
    print(f"Catálogo cargado en {(_time.perf_counter() - t0):.2f} s")
    print(f"  versión GTFS {cat.gtfs_version} · generado {cat.generado_utc}")
    print(f"  {len(cat.estaciones)} estaciones · {len(cat.trips)} trips\n")

    print("Búsqueda 'chamar' :", [e["nombre"] for e in cat.buscar_estaciones("chamar")])
    print("Búsqueda 'principe':", [e["nombre"] for e in cat.buscar_estaciones("principe")])
    print("Búsqueda 'atocha' :", [e["nombre"] for e in cat.buscar_estaciones("atocha")])

    hoy = fecha_de_servicio(ahora_utc())
    activos = cat.servicios_activos(hoy)
    print(f"\nFecha de servicio {hoy} ({hoy.strftime('%A')}): {len(activos)} servicios activos")

    # Trayecto de prueba: Atocha -> Villaverde Alto, ambos en el corredor sur.
    def id_de(nombre: str) -> str | None:
        r = cat.buscar_estaciones(nombre, limite=1)
        return r[0]["stop_id"] if r else None

    o, d = id_de("atocha"), id_de("villaverde alto")
    if o and d:
        t0 = _time.perf_counter()
        con = cat.conexiones_directas(o, d, hoy)
        ms = (_time.perf_counter() - t0) * 1000
        print(f"\nAtocha -> Villaverde Alto: {len(con)} trenes hoy ({ms:.1f} ms)")
        for c in con[:3]:
            print(f"  {c.line_id:<4} {c.trip_id:<16} "
                  f"salida {c.salida_s // 3600:02d}:{c.salida_s % 3600 // 60:02d} · "
                  f"llegada {c.llegada_s // 3600:02d}:{c.llegada_s % 3600 // 60:02d} · "
                  f"{c.i_destino - c.i_origen} paradas")
