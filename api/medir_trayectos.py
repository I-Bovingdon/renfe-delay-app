#!/usr/bin/env python3
"""
medir_trayectos.py — Mide qué porcentaje de trayectos tiene tren directo.

Esta medición decide el alcance de la aplicación ANTES de construirla. Si la mayoría de
los pares origen-destino de la red se cubren con un tren directo, "sin transbordos" es una
limitación menor y perfectamente defendible. Si no, hay que replantear el alcance.

Medir antes de construir.

Uso (PC o VPS):
    python3 medir_trayectos.py --catalogo datos/catalogo.json

TFM Cercanías RENFE · UCM · 2026
"""

from __future__ import annotations

import argparse
import json
from collections import defaultdict
from itertools import combinations


def main() -> None:
    parser = argparse.ArgumentParser(description="Cobertura de trayectos directos")
    parser.add_argument("--catalogo", required=True)
    parser.add_argument(
        "--top", type=int, default=15,
        help="nº de estaciones con más trenes para la cobertura ponderada",
    )
    args = parser.parse_args()

    with open(args.catalogo, encoding="utf-8") as f:
        cat = json.load(f)

    estaciones = {e["stop_id"]: e["nombre"] for e in cat["estaciones"]}
    n = len(estaciones)

    # Pares (origen, destino) ORDENADOS cubiertos por algún tren directo.
    # Ordenados porque ir de A a B no implica que exista servicio de B a A.
    pares_directos: set[tuple[str, str]] = set()
    trenes_por_parada: dict[str, int] = defaultdict(int)

    for trip in cat["trips"]:
        ids = [p["stop_id"] for p in trip["paradas"]]
        for sid in ids:
            trenes_por_parada[sid] += 1
        # Todo par (i, j) con i antes que j en el recorrido es un trayecto directo.
        for i, j in combinations(range(len(ids)), 2):
            pares_directos.add((ids[i], ids[j]))

    posibles = n * (n - 1)
    cobertura = len(pares_directos) / posibles if posibles else 0.0

    print(f"Estaciones: {n}")
    print(f"Pares ordenados posibles: {posibles:,}")
    print(f"Pares con tren directo:   {len(pares_directos):,}")
    print(f"\n>>> COBERTURA GLOBAL: {cobertura:.1%}\n")

    # Cobertura entre las estaciones más transitadas: es lo que de verdad va a probar
    # el tribunal (nadie pide Zarzalejo -> Santa María de la Alameda en una demo).
    top = sorted(trenes_por_parada, key=trenes_por_parada.get, reverse=True)[: args.top]
    pares_top = [(a, b) for a in top for b in top if a != b]
    cubiertos_top = sum(1 for p in pares_top if p in pares_directos)
    print(f">>> COBERTURA ENTRE LAS {args.top} ESTACIONES PRINCIPALES: "
          f"{cubiertos_top / len(pares_top):.1%}")
    print("\nEstaciones principales por nº de trenes:")
    for sid in top:
        print(f"  {trenes_por_parada[sid]:>5} trenes · {estaciones.get(sid, sid)}")

    # Ejemplos concretos de trayectos SIN directo entre estaciones principales:
    # sirven para redactar honestamente la limitación en la memoria.
    sin_directo = [p for p in pares_top if p not in pares_directos][:10]
    if sin_directo:
        print("\nEjemplos de trayectos principales SIN tren directo:")
        for a, b in sin_directo:
            print(f"  {estaciones.get(a, a)}  ->  {estaciones.get(b, b)}")

    print("\n--- CRITERIO DE DECISIÓN ---")
    if cobertura >= 0.75:
        print("≥ 75%: alcance 'solo directos' CONFIRMADO. Documentar y seguir a F2.")
    elif cobertura >= 0.50:
        print("50-75%: añadir UN transbordo en los intercambiadores del eje central.")
    else:
        print("< 50%: replantear alcance. Reservar 2 días para transbordos.")


if __name__ == "__main__":
    main()
