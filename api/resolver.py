"""
resolver.py — De (origen, destino, hora) a trenes candidatos.

Traduce lo que pide el usuario a una lista de trayectos concretos del catálogo GTFS,
listos para que se les calculen features y se les pida una predicción.

DECISIÓN DE DISEÑO: un trayecto es siempre una LISTA DE TRAMOS, aunque en la versión 1
solo se resuelvan directos y la lista tenga siempre un elemento. Medido sobre el
catálogo del 25/08: los directos cubren el 24,5 % de los pares origen-destino y el
61,9 % entre las 15 estaciones principales. Con un transbordo en Atocha se llega al
88,2 %, y sumando Chamartín, El Escorial y Cercedilla al 91,2 %, que es exactamente el
techo del caso general. Los transbordos se implementarán tras el despliegue público;
modelar ya la forma general evita reescribir resolutor e interfaz cuando llegue.

TFM Cercanías RENFE · UCM · 2026
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime, timedelta

from catalogo import Catalogo, Conexion
from tiempo import (
    fecha_de_servicio,
    hora_gtfs_a_utc,
    utc_a_segundos_de_servicio,
)

# Posiciones del formato compacto de trips. El catálogo valida 'formato_trips' al
# cargarse, así que estas posiciones están garantizadas o la carga habría fallado.
I_LLEGADAS, I_SALIDAS = 5, 6

# Cuánto tiempo hacia delante se buscan trenes desde la hora pedida.
VENTANA_BUSQUEDA_MIN = 90

# Cuántos trenes se ofrecen al usuario. Más de cuatro no caben en una pantalla de móvil
# sin scroll y cada uno cuesta una predicción.
MAX_OPCIONES = 4


@dataclass
class Tramo:
    """Un tren concreto entre dos paradas."""
    idx_trip: int
    trip_id: str
    line_id: str
    service_id: str
    service_date: date

    origen_stop_id: str
    origen_nombre: str
    salida_teorica_utc: datetime

    destino_stop_id: str
    destino_nombre: str
    llegada_teorica_utc: datetime

    dest_stop_sequence: int      # posición del destino en el recorrido (1..N)
    trip_total_stops: int
    paradas_intermedias: int
    regime: str                  # 'A' tren ya en circulación · 'B' aún no salido
    horizon_s: int               # segundos entre t0 y la llegada teórica al destino


@dataclass
class Trayecto:
    """Una forma de hacer el viaje: uno o varios tramos encadenados."""
    tramos: list[Tramo] = field(default_factory=list)

    @property
    def n_transbordos(self) -> int:
        return max(0, len(self.tramos) - 1)

    @property
    def salida_teorica_utc(self) -> datetime:
        return self.tramos[0].salida_teorica_utc

    @property
    def llegada_teorica_utc(self) -> datetime:
        return self.tramos[-1].llegada_teorica_utc


def _construir_tramo(
    cat: Catalogo,
    con: Conexion,
    fecha_serv: date,
    t0_utc: datetime,
) -> Tramo:
    """Convierte una conexión del catálogo en un tramo con instantes absolutos."""
    trip = cat.trips[con.idx_trip]
    salida_utc = hora_gtfs_a_utc(fecha_serv, con.salida_s)
    llegada_utc = hora_gtfs_a_utc(fecha_serv, con.llegada_s)

    # Régimen: ¿el tren ya ha salido de su cabecera en el instante de la consulta?
    # En régimen A el modelo dispone del estado propio del tren; en B, no.
    inicio_trip_s = trip[I_SALIDAS][0]
    t0_s = utc_a_segundos_de_servicio(t0_utc, fecha_serv)
    regime = "A" if inicio_trip_s <= t0_s else "B"

    paradas = cat.paradas_de(con.idx_trip)
    return Tramo(
        idx_trip=con.idx_trip,
        trip_id=con.trip_id,
        line_id=con.line_id,
        service_id=con.service_id,
        service_date=fecha_serv,
        origen_stop_id=paradas[con.i_origen],
        origen_nombre=cat.estaciones[paradas[con.i_origen]]["nombre"],
        salida_teorica_utc=salida_utc,
        destino_stop_id=paradas[con.i_destino],
        destino_nombre=cat.estaciones[paradas[con.i_destino]]["nombre"],
        llegada_teorica_utc=llegada_utc,
        dest_stop_sequence=con.i_destino + 1,
        trip_total_stops=len(paradas),
        paradas_intermedias=con.i_destino - con.i_origen - 1,
        regime=regime,
        horizon_s=int((llegada_utc - t0_utc).total_seconds()),
    )


def resolver_trayecto(
    cat: Catalogo,
    origen: str,
    destino: str,
    desde_utc: datetime,
    max_opciones: int = MAX_OPCIONES,
    ventana_min: int = VENTANA_BUSQUEDA_MIN,
) -> tuple[list[Trayecto], str | None]:
    """Devuelve los trayectos candidatos y, si procede, un aviso para el usuario.

    Busca trenes que salgan del origen entre la hora pedida y `ventana_min` minutos
    después. Si el día de servicio se agota sin encontrar suficientes (consulta de
    madrugada, o últimos trenes del día), continúa en el día de servicio siguiente:
    de lo contrario, quien consulta a las 23:50 no vería nunca los trenes de las 00:15,
    que pertenecen al día de servicio anterior.
    """
    if origen == destino:
        return [], "El origen y el destino son la misma estación."
    if origen not in cat.estaciones or destino not in cat.estaciones:
        return [], "Alguna de las estaciones no pertenece al núcleo de Madrid."

    hasta_utc = desde_utc + timedelta(minutes=ventana_min)
    trayectos: list[Trayecto] = []
    hubo_conexiones = False

    fecha_serv = fecha_de_servicio(desde_utc)
    for dia in (fecha_serv, fecha_serv + timedelta(days=1)):
        if len(trayectos) >= max_opciones:
            break

        conexiones = cat.conexiones_directas(origen, destino, dia)
        if conexiones:
            hubo_conexiones = True

        for con in conexiones:
            salida_utc = hora_gtfs_a_utc(dia, con.salida_s)
            if salida_utc < desde_utc or salida_utc > hasta_utc:
                continue
            trayectos.append(Trayecto(tramos=[_construir_tramo(cat, con, dia, desde_utc)]))
            if len(trayectos) >= max_opciones:
                break

        trayectos.sort(key=lambda t: (t.salida_teorica_utc, t.llegada_teorica_utc))

    # Avisos: se distingue "no hay servicio entre estas estaciones" de "no hay trenes
    # a esta hora". Para el usuario son problemas muy distintos.
    aviso = None
    if not trayectos:
        if hubo_conexiones:
            aviso = (
                f"No hay trenes directos en los {ventana_min} minutos siguientes a la "
                f"hora indicada. Prueba con otra hora."
            )
        else:
            aviso = (
                "No hay tren directo entre estas dos estaciones. Los trayectos con "
                "transbordo aún no están disponibles."
            )
    return trayectos, aviso


if __name__ == "__main__":
    import sys
    import time as _time

    from tiempo import ahora_utc, formatear_local, iso_utc

    cat = Catalogo(sys.argv[1] if len(sys.argv) > 1 else "../datos/catalogo.json")

    def id_de(nombre: str) -> str:
        return cat.buscar_estaciones(nombre, limite=1)[0]["stop_id"]

    pruebas = [
        ("atocha", "villaverde alto"),      # mismo corredor, muchos directos
        ("atocha", "alcala de henares"),    # corredor este
        ("chamartin", "leganes"),           # SIN directo: requiere transbordo
    ]

    ahora = ahora_utc()
    print(f"Consulta a las {formatear_local(ahora)} de Madrid "
          f"(fecha de servicio {fecha_de_servicio(ahora)})\n")

    for nombre_o, nombre_d in pruebas:
        o, d = id_de(nombre_o), id_de(nombre_d)
        t0 = _time.perf_counter()
        trayectos, aviso = resolver_trayecto(cat, o, d, ahora)
        ms = (_time.perf_counter() - t0) * 1000

        print(f"{cat.estaciones[o]['nombre']} -> {cat.estaciones[d]['nombre']}  ({ms:.1f} ms)")
        if aviso:
            print(f"  aviso: {aviso}")
        for t in trayectos:
            tr = t.tramos[0]
            print(
                f"  {tr.line_id:<4} {tr.trip_id:<16} "
                f"{formatear_local(tr.salida_teorica_utc)} -> "
                f"{formatear_local(tr.llegada_teorica_utc)} · "
                f"{tr.paradas_intermedias} paradas intermedias · "
                f"régimen {tr.regime} · horizonte {tr.horizon_s // 60} min"
            )
        print()
