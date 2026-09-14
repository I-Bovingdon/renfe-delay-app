"""
medir_rumbo.py — Contrasta la direccion derivada contra el desplazamiento real.

Se ejecuta EN EL VPS. Empareja dos capturas consecutivas del feed por `tripId`,
restringe al nucleo de Madrid y compara, para cada tren que se ha movido lo
suficiente, el rumbo que produciria cada regla candidata con el rumbo real de su
desplazamiento. Escribe `medicion_rumbo.csv`, que es lo que consume el cuaderno.

    sudo -u tfm /usr/bin/python3 medir_rumbo.py > /dev/null
    # deja medicion_rumbo.csv en el directorio actual

Por que se mide asi y no contra el catalogo: el catalogo es justamente lo que se
quiere validar. El desplazamiento observado entre dos capturas es independiente de
el, asi que es la unica referencia que no da la respuesta por buena de antemano.

TFM Cercanias RENFE · UCM · 2026
"""
from __future__ import annotations

import csv, gzip, json, math, os, re, sys
from pathlib import Path

DIR_VP = Path(os.getenv("DIR_VEHICLE_POSITIONS",
                        "/home/tfm/data-renfe/raw/vehicle_positions"))
RUTA_CATALOGO = Path(os.getenv("RUTA_CATALOGO", "../datos/catalogo.json"))
SALIDA = Path("medicion_rumbo.csv")

# Movimiento minimo para que el rumbo real signifique algo. Por debajo, el angulo lo
# decide el error de posicion y no el avance del tren.
MIN_DESPLAZAMIENTO_M = 150.0
# Velocidad por encima de la cual el emparejamiento es sospechoso y se descarta.
MAX_VELOCIDAD_KMH = 140.0

_PREFIJO = re.compile(r"^\d+[A-Za-z]")


def metros(p, q) -> float:
    dy = (q[0] - p[0]) * 111320.0
    dx = (q[1] - p[1]) * 111320.0 * math.cos(math.radians((p[0] + q[0]) / 2))
    return math.hypot(dx, dy)


def rumbo(p, q) -> float:
    f1, f2 = math.radians(p[0]), math.radians(q[0])
    dl = math.radians(q[1] - p[1])
    y = math.sin(dl) * math.cos(f2)
    x = math.cos(f1) * math.sin(f2) - math.sin(f1) * math.cos(f2) * math.cos(dl)
    return (math.degrees(math.atan2(y, x)) + 360.0) % 360.0


def desviacion(a: float, b: float) -> float:
    """Diferencia angular absoluta, en [0, 180]."""
    return abs((a - b + 180.0) % 360.0 - 180.0)


def leer(fichero: Path, paradas_madrid: set[str]) -> dict:
    pl = json.load(gzip.open(fichero, "rt", encoding="utf-8")).get("payload") or {}
    salida = {}
    for e in pl.get("entity") or []:
        v = e.get("vehicle") or {}
        pos = v.get("position") or {}
        trip = str((v.get("trip") or {}).get("tripId") or "")
        stop = str(v.get("stopId") or "")
        # Filtro de nucleo TOPOLOGICO: la parada publicada tiene que ser del catalogo
        # de Madrid. Sin el se cuelan trenes de otros nucleos, porque el feed es
        # nacional y el numero corto de vehiculo no es unico a escala nacional.
        if trip and stop in paradas_madrid and pos.get("latitude") is not None:
            salida[trip] = {
                "lat": pos["latitude"], "lon": pos["longitude"], "stop": stop,
                "estado": str(v.get("currentStatus") or ""),
                "ts": int(v.get("timestamp") or 0),
            }
    return salida


def main() -> int:
    cat = json.load(open(RUTA_CATALOGO, encoding="utf-8"))
    I = {n: i for i, n in enumerate(cat["formato_trips"])}
    est = {e["stop_id"]: (e["lat"], e["lon"]) for e in cat["estaciones"]}
    madrid = set(est)
    recorrido = {}
    for t in cat["trips"]:
        recorrido.setdefault(_PREFIJO.sub("", t[I["trip_id"]]), t[I["stop_ids"]])

    carpeta = sorted(d for d in DIR_VP.iterdir() if d.is_dir())[-1]
    ficheros = sorted(carpeta.iterdir())[-2:]
    antes, ahora = leer(ficheros[0], madrid), leer(ficheros[1], madrid)
    print(f"capturas   : {[f.name for f in ficheros]}")
    print(f"trenes Madrid: {len(antes)} -> {len(ahora)}")

    filas, descartados = [], 0
    for trip, q in ahora.items():
        p = antes.get(trip)
        if not p:
            continue
        pq = ((p["lat"], p["lon"]), (q["lat"], q["lon"]))
        d = metros(*pq)
        dt = max(1, q["ts"] - p["ts"])
        if d < MIN_DESPLAZAMIENTO_M:
            continue
        if d / dt * 3.6 > MAX_VELOCIDAD_KMH:
            descartados += 1
            continue

        real = rumbo(*pq)
        actual = (q["lat"], q["lon"])
        fila = {"trip_id": trip, "estado": q["estado"], "desplazamiento_m": round(d),
                "rumbo_real": round(real, 1),
                "desv_publicado": "", "desv_siguiente": "", "desv_tramo": ""}

        # Regla 1: desde el tren hasta la parada que publica el feed.
        e = est.get(q["stop"])
        if e:
            fila["desv_publicado"] = round(desviacion(rumbo(actual, e), real), 1)

        paradas = recorrido.get(_PREFIJO.sub("", trip))
        if paradas and q["stop"] in paradas:
            i = paradas.index(q["stop"])
            # Regla 2: desde el tren hasta la parada SIGUIENTE del recorrido.
            if i + 1 < len(paradas) and paradas[i + 1] in est:
                fila["desv_siguiente"] = round(
                    desviacion(rumbo(actual, est[paradas[i + 1]]), real), 1)
            # Regla 3 (la implantada): rumbo del TRAMO de via, de estacion a
            # estacion. La posicion del tren NO interviene.
            if i + 1 < len(paradas):
                a, b = paradas[i], paradas[i + 1]
            elif i > 0:
                a, b = paradas[i - 1], paradas[i]
            else:
                a = b = None
            if a in est and b in est and metros(est[a], est[b]) >= 50:
                fila["desv_tramo"] = round(desviacion(rumbo(est[a], est[b]), real), 1)
        filas.append(fila)

    with open(SALIDA, "w", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(fh, fieldnames=list(filas[0]) if filas else
                           ["trip_id", "estado", "desplazamiento_m", "rumbo_real",
                            "desv_publicado", "desv_siguiente", "desv_tramo"])
        w.writeheader(); w.writerows(filas)

    print(f"comparables: {len(filas)} · descartados por velocidad: {descartados}")
    for col in ("desv_publicado", "desv_siguiente", "desv_tramo"):
        v = sorted(float(f[col]) for f in filas if f[col] != "")
        if v:
            print(f"  {col:<15} n={len(v):<4} mediana {v[len(v)//2]:5.1f}° "
                  f"invertidos(>135°) {sum(1 for x in v if x > 135)}")
    print(f"escrito: {SALIDA.resolve()}")
    return 0 if filas else 1


if __name__ == "__main__":
    sys.exit(main())
