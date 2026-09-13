#!/usr/bin/env python3
"""
generar_puntualidad.py — Resumen histórico de puntualidad por línea.

Se ejecuta UNA VEZ y produce un JSON pequeño (unos pocos KB) que el servicio carga
en memoria al arrancar. El servicio NUNCA lee Parquet: recorrer 90 días con pandas
cuesta segundos y cientos de megas de pico, y el proceso va limitado a 400 MB
compartiendo dos vCPU con la captura 24/7.

QUÉ MAGNITUD SE MIDE, Y POR QUÉ ESTA Y NO LA OTRA
--------------------------------------------------
En el proyecto conviven dos medidas del retraso:

  (a) El TARGET del modelo: retraso observado, reconstruido cruzando
      `vehicle_positions` con el horario teórico del GTFS de la semana
      correspondiente. Es el que aprende el modelo.
  (b) La ESTIMACIÓN PUBLICADA por Renfe en `trip_updates.arrival_delay_s`.

Aquí se usa (b), a propósito. El asistente ya informa del estado en vivo de cada
línea con esa misma magnitud, porque `fuente_raw.py` lee el feed. Si el histórico
se calculara con (a), el usuario compararía "la C10 lleva 12 min ahora" con "la C10
tiene 3 min de media" y estaría comparando dos cosas distintas sin saberlo.

La consecuencia hay que declararla y no esconderla: **esto describe lo que Renfe
publica, no el retraso reconstruido que modela el TFM.** Va en la respuesta del
asistente y en el anexo.

CRITERIOS REPLICADOS DE OTRAS PIEZAS (no reinventados aquí)
-----------------------------------------------------------
  · Umbral de plausibilidad 14.400 s: el mismo DELAY_MAX_PLAUSIBLE_S de
    `fuente_raw.py`. Filtra además el glitch sistemático de ±86.400 s.
  · Días excluidos: los mismos de `cercanias_pipeline.py` (12/07 y 19/07, con el
    feed vacío por fallo del emisor; y la franja 12:20-12:52 UTC del 27/07, corte
    de red del servidor).
  · Filtro de núcleo TOPOLÓGICO por stop_id contra las 95 paradas del catálogo.
    El feed es nacional y existe un C1 en Sevilla: filtrar por el sufijo del
    trip_id mezclaría núcleos.
  · Agregación por código base de línea (C4a y C4b cuentan como C4), igual que
    hace el asistente con el estado en vivo.

Ejecutar (VPS, una sola vez, con el intérprete del servicio):

    sudo -u tfm nice -n 19 ionice -c3 \\
        /home/tfm/renfe-delay-app/.venv/bin/python \\
        /home/tfm/renfe-delay-app/scripts/generar_puntualidad.py

TFM Cercanías RENFE · UCM · 2026
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
from datetime import date, datetime, timezone
from pathlib import Path

import pandas as pd

# --- Criterios replicados de otros módulos -------------------------------------
DELAY_MAX_PLAUSIBLE_S = 4 * 3600          # fuente_raw.DELAY_MAX_PLAUSIBLE_S
UMBRAL_PUNTUAL_S = 180                    # criterio propio: 3 min, ver más abajo

DIAS_EXCLUIDOS = {"2026-07-12", "2026-07-19"}
CORTE_PARCIAL = ("2026-07-27", "12:20", "12:52")   # UTC

# Sufijo de línea del trip_id: "...C4b" -> "C4b". El feed no distingue rama en la
# práctica, pero el patrón la admite por si apareciera.
RE_LINEA = re.compile(r"C(\d+[ab]?)$", re.IGNORECASE)

# Mínimo de observaciones para que una línea entre en el resumen. Sin esto, una
# línea con servicio suspendido (la C9 en esta ventana) se colaría en el ranking
# con una media calculada sobre cuatro trenes. Mismo criterio que el ranking en
# vivo del asistente, trasladado a la escala del histórico.
MIN_OBSERVACIONES = 500


def codigo_base(linea: str) -> str:
    """'C4b' -> 'C4'. La rama no se distingue en el feed y al viajero no le dice nada."""
    linea = linea.upper()
    return linea[:-1] if linea and linea[-1] in ("A", "B") else linea


def paradas_de_madrid(ruta_catalogo: Path) -> set[str]:
    """Las 95 paradas del núcleo, del mismo catálogo que usa el servicio."""
    with open(ruta_catalogo, encoding="utf-8") as f:
        datos = json.load(f)
    return {str(e["stop_id"]) for e in datos["estaciones"]}


def procesar_dia(ruta: Path, paradas: set[str]) -> pd.DataFrame | None:
    """Devuelve una fila por (captura, tren) con su línea y su retraso publicado.

    Se lee SOLO las cuatro columnas necesarias. Un día son ~48.000 filas y unos
    pocos MB: el pico de memoria se mantiene en el orden de decenas de MB aunque
    la serie completa sean 90 días.
    """
    try:
        df = pd.read_parquet(
            ruta, columns=["fetched_at_utc", "trip_id", "stop_id", "arrival_delay_s"]
        )
    except Exception as exc:  # noqa: BLE001 — un día ilegible no aborta la serie
        print(f"  aviso: {ruta.name} ilegible ({exc})", file=sys.stderr)
        return None

    dia = ruta.stem.replace("trip_updates_", "")
    if dia in DIAS_EXCLUIDOS:
        print(f"  {dia}: excluido (feed vacío por fallo del emisor)")
        return None

    df = df.dropna(subset=["arrival_delay_s", "trip_id"])
    if df.empty:
        return None

    # Franja de corte de red del 27/07.
    if dia == CORTE_PARCIAL[0]:
        momento = pd.to_datetime(df["fetched_at_utc"], utc=True, errors="coerce")
        hhmm = momento.dt.strftime("%H:%M")
        antes = len(df)
        df = df[~((hhmm >= CORTE_PARCIAL[1]) & (hhmm <= CORTE_PARCIAL[2]))]
        print(f"  {dia}: excluidas {antes - len(df)} filas de la franja sin red")

    # Núcleo de Madrid: filtro topológico, no por sufijo de línea.
    df = df[df["stop_id"].astype(str).isin(paradas)]
    if df.empty:
        return None

    # Una observación por (captura, tren): si un tren publica varias paradas en la
    # misma captura, contarlas todas ponderaría de más a los trenes con más
    # actualizaciones, que no es lo que mide "el retraso medio de la línea".
    df = df.drop_duplicates(subset=["fetched_at_utc", "trip_id"], keep="first")

    extraida = df["trip_id"].astype(str).str.extract(RE_LINEA, expand=False)
    df = df.assign(linea=("C" + extraida.astype(str)).map(codigo_base))
    df = df[extraida.notna()]

    df["arrival_delay_s"] = pd.to_numeric(df["arrival_delay_s"], errors="coerce")
    df = df[df["arrival_delay_s"].abs() <= DELAY_MAX_PLAUSIBLE_S]
    if df.empty:
        return None

    df["dia"] = dia
    return df[["dia", "linea", "arrival_delay_s"]]


def resumir(df: pd.DataFrame) -> dict:
    """Agrega por línea. Se guardan varios estadísticos, no solo la media.

    La media sola es mala consejera con una distribución de cola larga como esta
    (mediana ~0 s, p99 ~28 min): un puñado de trenes muy retrasados la desplaza y
    la línea parece peor de lo que la vive un viajero. La mediana y el porcentaje
    de trenes puntuales describen mejor la experiencia real, y por eso el
    asistente responde con el porcentaje, no con la media.
    """
    salida = {}
    for linea, g in df.groupby("linea"):
        n = len(g)
        if n < MIN_OBSERVACIONES:
            print(f"  {linea}: {n} observaciones, por debajo del mínimo; excluida")
            continue
        d = g["arrival_delay_s"]
        salida[linea] = {
            "observaciones": int(n),
            "dias": int(g["dia"].nunique()),
            "retraso_medio_s": round(float(d.mean()), 1),
            "retraso_mediana_s": round(float(d.median()), 1),
            "retraso_p90_s": round(float(d.quantile(0.90)), 1),
            "pct_puntual": round(float((d <= UMBRAL_PUNTUAL_S).mean() * 100), 1),
        }
    return salida


def main() -> int:
    p = argparse.ArgumentParser(description="Resumen de puntualidad por línea")
    p.add_argument("--processed",
                   default="/home/tfm/data-renfe/processed/trip_updates")
    p.add_argument("--catalogo",
                   default=os.getenv("RUTA_CATALOGO",
                                     "/home/tfm/renfe-delay-app/datos/catalogo.json"))
    p.add_argument("--salida",
                   default="/home/tfm/renfe-delay-app/datos/puntualidad.json")
    args = p.parse_args()

    carpeta = Path(args.processed)
    ficheros = sorted(carpeta.glob("trip_updates_*.parquet"))
    if not ficheros:
        print(f"No hay Parquet en {carpeta}", file=sys.stderr)
        return 1

    paradas = paradas_de_madrid(Path(args.catalogo))
    print(f"{len(ficheros)} días a procesar · {len(paradas)} paradas del núcleo\n")

    partes = []
    for ruta in ficheros:
        parte = procesar_dia(ruta, paradas)
        if parte is not None:
            partes.append(parte)

    if not partes:
        print("Ningún día utilizable", file=sys.stderr)
        return 1

    df = pd.concat(partes, ignore_index=True)
    del partes

    lineas = resumir(df)
    dias = sorted(df["dia"].unique())

    documento = {
        "generado_utc": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "magnitud": "estimacion_publicada_renfe",
        "ventana": {"desde": dias[0], "hasta": dias[-1], "dias": len(dias)},
        "umbral_puntual_s": UMBRAL_PUNTUAL_S,
        "observaciones_totales": int(len(df)),
        "excluidos": sorted(DIAS_EXCLUIDOS),
        "lineas": lineas,
    }

    Path(args.salida).parent.mkdir(parents=True, exist_ok=True)
    with open(args.salida, "w", encoding="utf-8") as f:
        json.dump(documento, f, ensure_ascii=False, indent=1)

    print(f"\nVentana {dias[0]} a {dias[-1]} · {len(dias)} días · "
          f"{len(df):,} observaciones".replace(",", "."))
    print(f"{'línea':>6} {'obs':>10} {'medio':>8} {'mediana':>8} {'p90':>8} {'puntual':>9}")
    for linea, v in sorted(lineas.items(), key=lambda kv: -kv[1]["pct_puntual"]):
        print(f"{linea:>6} {v['observaciones']:>10} {v['retraso_medio_s']:>8.0f} "
              f"{v['retraso_mediana_s']:>8.0f} {v['retraso_p90_s']:>8.0f} "
              f"{v['pct_puntual']:>8.1f}%")
    print(f"\nEscrito en {args.salida} "
          f"({Path(args.salida).stat().st_size / 1024:.1f} KB)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
