#!/usr/bin/env python3
"""
generar_catalogo.py — Construye el catálogo de red que alimenta la interfaz.

Toma un zip de GTFS estático de RENFE y produce un único JSON con lo que la aplicación
necesita: estaciones con coordenadas, trazado de cada línea para el mapa, y los trenes
del día con sus horarios teóricos.

Solo biblioteca estándar: se ejecuta con el python del sistema del VPS, sin instalar nada
y sin depender del entorno virtual de los colectores.

Uso (VPS, como usuario tfm):
    python3 generar_catalogo.py \
        --gtfs /home/tfm/data-renfe/gtfs_static/fomento_transit_<hash>.zip \
        --salida /home/tfm/catalogo.json

Notas de dominio (aprendidas a base de golpes en este proyecto):
  - Los valores del GTFS de RENFE vienen con relleno de ancho fijo ('10T0001C1  ').
    Sin .strip() ningún cruce casa. Se normaliza TODO al leer.
  - El núcleo Madrid se identifica por el prefijo del route_id ('10T'), no por
    coordenadas: el bounding box dejaba fuera Guadalajara.
  - Las horas del GTFS pueden pasar de 24:00 ('25:10:00'). NO se truncan: son parte de
    la fecha de servicio y truncarlas es lo que genera los errores de ±24 h.

TFM Cercanías RENFE · UCM · 2026
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import io
import json
import zipfile
from collections import defaultdict
from datetime import datetime, timezone
from typing import Any

# Prefijo de route_id del núcleo 10 (Madrid). Estructural y exacto.
PREFIJO_NUCLEO_MADRID = "10T"

# Colores oficiales aproximados de las líneas de Cercanías Madrid, para el mapa.
COLORES_LINEA = {
    "C1": "#78BE20", "C2": "#008E5B", "C3": "#7B2182", "C3a": "#B47EB3",
    "C4": "#004B8D", "C4a": "#004B8D", "C4b": "#4A90D9",
    "C5": "#F4A81D", "C7": "#D6001C", "C8": "#8C8C8C",
    "C8a": "#8C8C8C", "C8b": "#5A5A5A", "C9": "#E8739E", "C10": "#B7D433",
}


def leer_csv(zf: zipfile.ZipFile, nombre: str) -> list[dict[str, str]]:
    """Lee un fichero del zip y devuelve filas con claves y valores ya normalizados.

    Devuelve lista vacía si el fichero no existe (shapes.txt y calendar.txt son
    opcionales en el estándar GTFS y RENFE no siempre los publica).
    """
    if nombre not in zf.namelist():
        print(f"  · {nombre}: NO PRESENTE en el zip")
        return []

    crudo = zf.read(nombre)
    # RENFE publica en UTF-8 con BOM la mayoría de las veces; latin-1 como respaldo.
    try:
        texto = crudo.decode("utf-8-sig")
    except UnicodeDecodeError:
        texto = crudo.decode("latin-1")

    lector = csv.DictReader(io.StringIO(texto))
    filas = [
        {(k or "").strip(): (v or "").strip() for k, v in fila.items()}
        for fila in lector
    ]
    print(f"  · {nombre}: {len(filas):>7} filas")
    return filas


def hora_gtfs_a_segundos(hhmmss: str) -> int | None:
    """'25:10:00' -> 90600 segundos desde la medianoche de la FECHA DE SERVICIO.

    Se conservan las horas >= 24 deliberadamente: representan trenes que circulan
    después de medianoche pero pertenecen al día de servicio anterior.
    """
    try:
        h, m, s = (int(x) for x in hhmmss.split(":"))
        return h * 3600 + m * 60 + s
    except (ValueError, AttributeError):
        return None


def main() -> None:
    parser = argparse.ArgumentParser(description="Genera el catálogo de red desde el GTFS")
    parser.add_argument("--gtfs", required=True, help="ruta del zip de GTFS estático")
    parser.add_argument("--salida", required=True, help="ruta del catalogo.json a escribir")
    args = parser.parse_args()

    # --- Versión del catálogo: hash del zip. Es el gtfs_version del contrato. -----------
    with open(args.gtfs, "rb") as f:
        gtfs_version = hashlib.sha256(f.read()).hexdigest()[:12]
    print(f"GTFS: {args.gtfs}\nVersión (sha256 corto): {gtfs_version}\n")

    print("Leyendo ficheros del zip:")
    with zipfile.ZipFile(args.gtfs) as zf:
        routes = leer_csv(zf, "routes.txt")
        trips = leer_csv(zf, "trips.txt")
        stops = leer_csv(zf, "stops.txt")
        stop_times = leer_csv(zf, "stop_times.txt")
        calendar = leer_csv(zf, "calendar.txt")
        calendar_dates = leer_csv(zf, "calendar_dates.txt")
        shapes = leer_csv(zf, "shapes.txt")

    # --- 1. Rutas del núcleo Madrid ----------------------------------------------------
    rutas_madrid: dict[str, dict[str, str]] = {}
    for r in routes:
        rid = r.get("route_id", "")
        if rid.startswith(PREFIJO_NUCLEO_MADRID):
            # route_short_name es lo que el viajero reconoce: 'C5', 'C4a'...
            linea = r.get("route_short_name") or rid[-3:]
            rutas_madrid[rid] = {"line_id": linea, "nombre": r.get("route_long_name", "")}
    print(f"\nRutas del núcleo Madrid: {len(rutas_madrid)}")

    # --- 2. Trips de esas rutas --------------------------------------------------------
    trips_madrid: dict[str, dict[str, Any]] = {}
    for t in trips:
        rid = t.get("route_id", "")
        if rid in rutas_madrid:
            trips_madrid[t["trip_id"]] = {
                "trip_id": t["trip_id"],
                "route_id": rid,
                "line_id": rutas_madrid[rid]["line_id"],
                "service_id": t.get("service_id", ""),
                "direction_id": t.get("direction_id", "0"),
                "shape_id": t.get("shape_id", ""),
                "headsign": t.get("trip_headsign", ""),
                "paradas": [],
            }
    print(f"Trips del núcleo Madrid: {len(trips_madrid)}")

    # --- 3. Horarios: se recorre stop_times una sola vez (es el fichero grande) ---------
    stops_usados: set[str] = set()
    for st in stop_times:
        tid = st.get("trip_id", "")
        trip = trips_madrid.get(tid)
        if trip is None:
            continue
        llegada = hora_gtfs_a_segundos(st.get("arrival_time", ""))
        salida = hora_gtfs_a_segundos(st.get("departure_time", ""))
        if llegada is None:
            continue
        sid = st.get("stop_id", "")
        stops_usados.add(sid)
        trip["paradas"].append(
            {
                "stop_id": sid,
                "seq": int(st.get("stop_sequence", "0") or 0),
                "llegada_s": llegada,
                "salida_s": salida if salida is not None else llegada,
            }
        )

    # Ordenar por secuencia y descartar trips con menos de dos paradas.
    for trip in trips_madrid.values():
        trip["paradas"].sort(key=lambda p: p["seq"])
    trips_madrid = {k: v for k, v in trips_madrid.items() if len(v["paradas"]) >= 2}
    print(f"Trips con horario completo: {len(trips_madrid)}")

    # --- 4. Estaciones -----------------------------------------------------------------
    lineas_por_parada: dict[str, set[str]] = defaultdict(set)
    for trip in trips_madrid.values():
        for p in trip["paradas"]:
            lineas_por_parada[p["stop_id"]].add(trip["line_id"])

    estaciones = []
    for s in stops:
        sid = s.get("stop_id", "")
        if sid not in stops_usados:
            continue
        try:
            lat, lon = float(s["stop_lat"]), float(s["stop_lon"])
        except (KeyError, ValueError):
            print(f"  ! estación sin coordenadas, descartada: {sid}")
            continue
        estaciones.append(
            {
                "stop_id": sid,
                "nombre": s.get("stop_name", ""),
                "lat": lat,
                "lon": lon,
                "lineas": sorted(lineas_por_parada[sid]),
            }
        )
    estaciones.sort(key=lambda e: e["nombre"])
    print(f"Estaciones: {len(estaciones)}")

    coords = {e["stop_id"]: (e["lat"], e["lon"]) for e in estaciones}

    # --- 5. Trazado de cada línea para el mapa -----------------------------------------
    # Vía preferente: shapes.txt (trazado real por la vía).
    # Respaldo: unir las paradas del trip más largo de cada línea (más anguloso, sirve).
    puntos_shape: dict[str, list[tuple[int, float, float]]] = defaultdict(list)
    for sh in shapes:
        try:
            puntos_shape[sh["shape_id"]].append(
                (
                    int(sh.get("shape_pt_sequence", "0") or 0),
                    float(sh["shape_pt_lat"]),
                    float(sh["shape_pt_lon"]),
                )
            )
        except (KeyError, ValueError):
            continue

    trips_por_linea: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for trip in trips_madrid.values():
        trips_por_linea[trip["line_id"]].append(trip)

    lineas = []
    for line_id, sus_trips in sorted(trips_por_linea.items()):
        # El trip con más paradas representa mejor el recorrido completo de la línea.
        mas_largo = max(sus_trips, key=lambda t: len(t["paradas"]))
        trazado: list[list[float]] = []
        origen_trazado = "paradas"

        shape_id = mas_largo.get("shape_id", "")
        if shape_id and puntos_shape.get(shape_id):
            pts = sorted(puntos_shape[shape_id], key=lambda p: p[0])
            trazado = [[lat, lon] for _, lat, lon in pts]
            origen_trazado = "shapes"
        else:
            for p in mas_largo["paradas"]:
                if p["stop_id"] in coords:
                    lat, lon = coords[p["stop_id"]]
                    trazado.append([lat, lon])

        lineas.append(
            {
                "line_id": line_id,
                "color": COLORES_LINEA.get(line_id, "#666666"),
                "trazado": trazado,
                "origen_trazado": origen_trazado,
                "n_trips": len(sus_trips),
            }
        )
    print(f"Líneas: {len(lineas)} " f"(trazado desde {lineas[0]['origen_trazado'] if lineas else '-'})")

    # --- 6. Calendario de servicio -----------------------------------------------------
    dias = ("monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday")
    servicios = {
        c["service_id"]: {
            "dias": [int(c.get(d, "0") or 0) for d in dias],  # índice 0 = lunes
            "desde": c.get("start_date", ""),
            "hasta": c.get("end_date", ""),
        }
        for c in calendar
    }
    excepciones: dict[str, list[dict[str, str]]] = defaultdict(list)
    for cd in calendar_dates:
        # exception_type: 1 = servicio añadido ese día, 2 = servicio suprimido
        excepciones[cd.get("service_id", "")].append(
            {"fecha": cd.get("date", ""), "tipo": cd.get("exception_type", "")}
        )
    print(f"Servicios en calendar.txt: {len(servicios)} · "
          f"con excepciones: {len(excepciones)}")

    # --- 7. Escritura ------------------------------------------------------------------
    catalogo = {
        "gtfs_version": gtfs_version,
        "generado_utc": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "nucleo": "10 (Madrid)",
        "estaciones": estaciones,
        "lineas": lineas,
        "servicios": servicios,
        "excepciones": dict(excepciones),
        "trips": list(trips_madrid.values()),
    }
    with open(args.salida, "w", encoding="utf-8") as f:
        json.dump(catalogo, f, ensure_ascii=False, separators=(",", ":"))

    import os
    mb = os.path.getsize(args.salida) / 1_048_576
    print(f"\n✔ Catálogo escrito en {args.salida} ({mb:.1f} MB)")
    print(f"  {len(estaciones)} estaciones · {len(lineas)} líneas · {len(trips_madrid)} trips")


if __name__ == "__main__":
    main()
