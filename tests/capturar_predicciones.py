#!/usr/bin/env python3
"""
capturar_predicciones.py — Congela las predicciones de hoy para verificarlas mañana.

Consulta la API desplegada para un conjunto de trayectos, deduplica por tren y guarda
en un JSON todo lo que hará falta para comprobar mañana si la llegada estimada acertó:
identificador del tren, horario oficial de llegada, retraso predicho EN SEGUNDOS (sin
redondear), régimen, bloques degradados y versión del modelo.

Por qué no basta con las capturas de pantalla: el distintivo de la interfaz está
redondeado a minutos y "En hora" esconde cualquier valor por debajo del umbral. Medir
el error contra un valor redondeado mete ruido propio en la medición.

Solo hace peticiones HTTP y escribe un fichero bajo /home/tfm/validacion.
No toca el servicio ni los colectores.

Uso (VPS):
    sudo -u tfm /usr/bin/python3 capturar_predicciones.py

TFM Cercanías RENFE · UCM · 2026
"""

from __future__ import annotations

import json
import os
import sys
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path

BASE = os.getenv("BASE_API", "https://cercanias-madrid.es")
SALIDA = Path(os.getenv("DIR_VALIDACION", "/home/tfm/validacion"))
TIMEOUT = 25

# Los seis trayectos de las capturas. Se nombran por texto y se resuelven contra el
# buscador de la propia API, para no fijar identificadores a mano.
TRAYECTOS = [
    ("atocha",                  "pinar de las rozas"),
    ("principe pio",            "alcala de henares"),
    ("cercedilla",              "chamartin"),
    ("villalba de guadarrama",  "piramides"),
    ("aranjuez",                "chamartin"),
    ("alcobendas",              "parla"),
]

# Horizontes en minutos. Los mismos que ofrecen las fichas de la interfaz, más dos
# saltos largos para alcanzar los trenes de la noche.
HORIZONTES_MIN = [0, 15, 30, 60, 120, 180]


def pedir(metodo: str, ruta: str, cuerpo=None):
    url = BASE + ruta
    datos = json.dumps(cuerpo).encode("utf-8") if cuerpo is not None else None
    req = urllib.request.Request(url, data=datos, method=metodo)
    if datos is not None:
        req.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:
            return resp.status, json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        return e.code, None
    except Exception as exc:  # noqa: BLE001
        print(f"  error de red: {exc}")
        return 0, None


def estacion(nombre: str):
    codigo, datos = pedir("GET", f"/api/estaciones?q={urllib.parse.quote(nombre)}")
    if codigo == 200 and datos:
        return datos[0]
    return None


def main() -> int:
    ahora = datetime.now(timezone.utc)
    SALIDA.mkdir(parents=True, exist_ok=True)

    codigo, salud = pedir("GET", "/api/salud")
    if codigo != 200:
        print("La API no responde. Se aborta.")
        return 1

    print(f"Capturando predicciones · {BASE}")
    print(f"  gtfs {salud['catalogo']['gtfs_version']} · "
          f"modelo {salud['predictor']['backend']}")

    registros: dict[str, dict] = {}   # clave: trip_id + destino, para deduplicar
    for origen_txt, destino_txt in TRAYECTOS:
        o, d = estacion(origen_txt), estacion(destino_txt)
        if not o or not d:
            print(f"  [aviso] no se resuelve {origen_txt} -> {destino_txt}")
            continue
        print(f"\n{o['nombre']} -> {d['nombre']}")

        for salto in HORIZONTES_MIN:
            momento = ahora + timedelta(minutes=salto)
            codigo, resp = pedir("POST", "/api/consulta", {
                "origen": o["stop_id"], "destino": d["stop_id"],
                "salida_desde_utc": momento.strftime("%Y-%m-%dT%H:%M:%SZ"),
            })
            if codigo != 200 or not resp:
                print(f"  +{salto:>3} min: HTTP {codigo}")
                continue
            nuevos = 0
            for opcion in resp.get("opciones", []):
                for tramo in opcion["tramos"]:
                    clave = f"{tramo['trip_id']}|{tramo['destino']['stop_id']}"
                    if clave in registros:
                        continue
                    registros[clave] = {
                        # --- identificación del tren, para cruzar con el raw ---
                        "trip_id": tramo["trip_id"],
                        "line_id": tramo["line_id"],
                        "origen_stop_id": tramo["origen"]["stop_id"],
                        "origen_nombre": tramo["origen"]["nombre"],
                        "destino_stop_id": tramo["destino"]["stop_id"],
                        "destino_nombre": tramo["destino"]["nombre"],
                        # --- horario oficial ---
                        "salida_teorica_utc": tramo["origen"]["hora_teorica_utc"],
                        "llegada_teorica_utc": tramo["destino"]["hora_teorica_utc"],
                        "paradas_intermedias": tramo["paradas_intermedias"],
                        # --- lo que predijo el modelo, sin redondear ---
                        "retraso_p10_s": tramo["retraso_s"]["p10"],
                        "retraso_p50_s": tramo["retraso_s"]["p50"],
                        "retraso_p90_s": tramo["retraso_s"]["p90"],
                        "con_intervalo": tramo["retraso_s"]["con_intervalo"],
                        "llegada_estimada_utc": tramo["llegada_estimada_utc"],
                        # --- contexto de la predicción ---
                        "regime": tramo["regime"],
                        "degraded_blocks": tramo["degraded_blocks"],
                        "consultado_en_utc": resp["consultado_en_utc"],
                        "gtfs_version": resp["gtfs_version"],
                    }
                    nuevos += 1
            print(f"  +{salto:>3} min: {nuevos} trenes nuevos "
                  f"(acumulado {len(registros)})")

    if not registros:
        print("\nNo se ha capturado ningún tren.")
        return 1

    documento = {
        "capturado_utc": ahora.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "base": BASE,
        "gtfs_version": salud["catalogo"]["gtfs_version"],
        "backend_modelo": salud["predictor"]["backend"],
        "salud_en_captura": {
            "meteo": salud.get("meteo", {}).get("vigente"),
            "alertas": salud.get("alertas", {}).get("feed", {}).get("estado"),
            "posiciones": salud.get("posiciones", {}).get("estado"),
            "contexto_vigente": salud.get("contexto", {}).get("vigente"),
        },
        "n_predicciones": len(registros),
        "predicciones": sorted(registros.values(),
                               key=lambda r: r["llegada_teorica_utc"]),
    }

    nombre = f"predicciones_{ahora:%Y%m%d_%H%M}.json"
    ruta = SALIDA / nombre
    ruta.write_text(json.dumps(documento, ensure_ascii=False, indent=2),
                    encoding="utf-8")

    print("\n" + "=" * 74)
    print(f"{len(registros)} predicciones guardadas en {ruta}")
    por_regimen: dict[str, int] = {}
    por_linea: dict[str, int] = {}
    for r in registros.values():
        por_regimen[r["regime"]] = por_regimen.get(r["regime"], 0) + 1
        por_linea[r["line_id"]] = por_linea.get(r["line_id"], 0) + 1
    print(f"  por régimen: {por_regimen}   (A = ya circulaba · B = aún sin salir)")
    print(f"  por línea  : {dict(sorted(por_linea.items()))}")
    degradadas = sum(1 for r in registros.values() if r["degraded_blocks"])
    print(f"  con algún bloque degradado: {degradadas}")
    print("=" * 74)
    return 0


if __name__ == "__main__":
    sys.exit(main())
