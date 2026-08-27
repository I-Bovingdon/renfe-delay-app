#!/usr/bin/env python3
"""
medir_transbordos.py — ¿Cuánta cobertura compra permitir UN transbordo?

La medición de trayectos directos dio 24,5% global y 61,9% entre las estaciones
principales: insuficiente para un producto de cara al usuario. Antes de invertir dos
días en implementar transbordos hay que saber dos cosas:

  1. Cuál es el techo: cobertura permitiendo un transbordo en CUALQUIER estación.
  2. Cuántos intercambiadores hacen falta de verdad para acercarse a ese techo.

Si con cuatro estaciones se alcanza el 90%, no tiene sentido implementar el caso general.

Uso:
    python medir_transbordos.py --catalogo datos/catalogo.json

TFM Cercanías RENFE · UCM · 2026
"""

from __future__ import annotations

import argparse
import json
from collections import defaultdict
from itertools import combinations


def main() -> None:
    parser = argparse.ArgumentParser(description="Cobertura con un transbordo")
    parser.add_argument("--catalogo", required=True)
    parser.add_argument("--top", type=int, default=15,
                        help="nº de estaciones principales para la cobertura ponderada")
    parser.add_argument("--intercambiadores", type=int, default=8,
                        help="nº de intercambiadores a seleccionar por avidez")
    args = parser.parse_args()

    with open(args.catalogo, encoding="utf-8") as f:
        cat = json.load(f)

    nombres = {e["stop_id"]: e["nombre"] for e in cat["estaciones"]}
    formato = cat.get("formato_trips", [])
    idx_stops = formato.index("stop_ids") if "stop_ids" in formato else 4

    # --- Recorridos distintos y alcance directo --------------------------------------
    patrones: set[tuple[str, ...]] = set()
    trenes_por_parada: dict[str, int] = defaultdict(int)
    for trip in cat["trips"]:
        ids = trip[idx_stops]
        for sid in ids:
            trenes_por_parada[sid] += 1
        patrones.add(tuple(ids))

    # alcance_directo[a] = estaciones a las que se llega desde 'a' sin transbordar.
    alcance_directo: dict[str, set[str]] = defaultdict(set)
    for patron in patrones:
        for i, j in combinations(range(len(patron)), 2):
            alcance_directo[patron[i]].add(patron[j])

    estaciones = sorted(nombres)
    n = len(estaciones)
    posibles = n * (n - 1)
    directos = sum(len(v) for v in alcance_directo.values())

    print(f"Estaciones: {n} · recorridos distintos: {len(patrones)}")
    print(f"Cobertura DIRECTA: {directos / posibles:.1%} "
          f"({directos:,} de {posibles:,} pares)\n")

    # --- Techo: un transbordo en cualquier estación -----------------------------------
    def cobertura(intercambiadores: set[str], universo: list[str] | None = None) -> set[tuple[str, str]]:
        """Pares (a, b) alcanzables directamente o con un transbordo en el conjunto dado."""
        origenes = universo if universo is not None else estaciones
        destinos_validos = set(universo) if universo is not None else None
        alcanzables: set[tuple[str, str]] = set()
        for a in origenes:
            llega = set(alcance_directo[a])
            for x in alcance_directo[a] & intercambiadores:
                llega |= alcance_directo[x]
            llega.discard(a)
            if destinos_validos is not None:
                llega &= destinos_validos
            alcanzables |= {(a, b) for b in llega}
        return alcanzables

    todas = set(estaciones)
    techo = cobertura(todas)
    print(f"TECHO con un transbordo en cualquier estación: "
          f"{len(techo) / posibles:.1%}\n")

    # --- ¿Cuántos intercambiadores hacen falta? (selección por avidez) ----------------
    # Se añade en cada paso la estación que más pares nuevos aporta. Así se ve la curva
    # de rendimientos decrecientes y se decide con cuántas parar.
    print("Intercambiadores por aportación marginal:")
    print(f"{'#':>2}  {'estación':<42} {'cobertura':>10}  {'ganancia':>9}")
    elegidos: set[str] = set()
    anterior = directos / posibles
    for k in range(1, args.intercambiadores + 1):
        mejor, mejor_cob = None, -1.0
        for cand in estaciones:
            if cand in elegidos:
                continue
            cob = len(cobertura(elegidos | {cand})) / posibles
            if cob > mejor_cob:
                mejor, mejor_cob = cand, cob
        if mejor is None:
            break
        elegidos.add(mejor)
        print(f"{k:>2}  {nombres.get(mejor, mejor):<42} {mejor_cob:>9.1%}  "
              f"{mejor_cob - anterior:>+8.1%}")
        anterior = mejor_cob

    # --- Lo mismo, restringido a las estaciones con más tráfico ------------------------
    top = sorted(trenes_por_parada, key=trenes_por_parada.get, reverse=True)[: args.top]
    pares_top = (len(top) * (len(top) - 1))
    cob_top_directo = len(cobertura(set(), universo=top)) / pares_top
    cob_top_transb = len(cobertura(elegidos, universo=top)) / pares_top
    print(f"\nEntre las {args.top} estaciones principales:")
    print(f"  solo directos          : {cob_top_directo:.1%}")
    print(f"  con los intercambiadores elegidos: {cob_top_transb:.1%}")

    print("\n--- CRITERIO ---")
    print("Si con 3-5 intercambiadores se supera el 85-90%, implementar UN transbordo")
    print("restringido a ese conjunto fijo. El caso general no compensa el plazo.")


if __name__ == "__main__":
    main()
