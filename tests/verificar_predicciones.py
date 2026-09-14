#!/usr/bin/env python3
"""
verificar_predicciones.py — Contrasta las predicciones capturadas contra el feed real.

Lee los ficheros de /home/tfm/validacion/predicciones_*.json, busca cada tren en las
capturas de raw/trip_updates y calcula el error de la predicción.

QUÉ SE MIDE Y CON QUÉ DATO

El feed publica, para cada tren, las paradas próximas con `arrival.time` y
`arrival.delay`. Esa es la ÚLTIMA ESTIMACIÓN que RENFE publicó antes de que el tren
pasara por la parada, no una confirmación posterior: el operador no publica llegadas
confirmadas. Es el mejor dato disponible y la comparación hay que leerla así.

Dos métricas distintas, que no deben mezclarse:

  · error del retraso  = retraso predicho − retraso observado, en segundos.
    Es lo comparable con el MAE del conjunto de prueba.
  · error de la llegada = llegada estimada − llegada observada.
    Es lo que le importa al viajero y lo que enseña la pantalla.

Y un tercer número que vale tanto como los otros dos: cuántos trenes NO aparecen en el
feed. Eso es cobertura, no error del modelo, y hay que separarlo antes de calcular nada.

DOS VÍAS DE MEDIDA, por orden de preferencia:
  1. `arrival` de la parada de destino dentro de `stopTimeUpdate`. Directa y exacta.
  2. `delay` a nivel de tren en la captura más cercana anterior a la llegada teórica.
     Aproximada: es el retraso del tren en ese momento, no necesariamente en el destino.
El script informa de cuál se usó en cada caso y separa las métricas por vía.

Solo lee ficheros. No toca el servicio ni los colectores.

Uso (VPS, desde api/ para poder importar los módulos del proyecto):
    cd /home/tfm/renfe-delay-app/api
    sudo -u tfm /usr/bin/python3 ../tests/verificar_predicciones.py

TFM Cercanías RENFE · UCM · 2026
"""

from __future__ import annotations

import csv
import glob
import gzip
import json
import os
import re
import statistics
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

AQUI = Path(__file__).resolve().parent
sys.path.insert(0, str(AQUI.parent / "api"))

from estado_red import nucleo_trip  # noqa: E402

DIR_PRED = Path(os.getenv("DIR_VALIDACION", "/home/tfm/validacion"))
DIR_RAW = Path(os.getenv("DIR_TRIP_UPDATES", "/home/tfm/data-renfe/raw/trip_updates"))
SALIDA_CSV = DIR_PRED / "verificacion.csv"

# Margen alrededor de la llegada teórica para acotar qué capturas hay que abrir.
# Leer las 1.900 capturas del día entero costaría minutos sin aportar nada.
MARGEN_ANTES_MIN = 120
MARGEN_DESPUES_MIN = 60

# Mismo umbral de plausibilidad que usa fuente_raw: por encima de esto el valor del
# feed no es un retraso, es un error de publicación.
DELAY_MAX_PLAUSIBLE_S = 3 * 3600

_RE_EPOCH = re.compile(r"_ft(\d+)\.json\.gz$")


def epoch_de_nombre(nombre: str) -> int | None:
    m = _RE_EPOCH.search(nombre)
    return int(m.group(1)) if m else None


def desde_iso(texto: str) -> datetime:
    dt = datetime.fromisoformat(texto.replace("Z", "+00:00"))
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


# ============================================================== predicciones ===
def cargar_predicciones() -> dict[tuple[str, str], list[dict]]:
    """Agrupa todas las predicciones por (núcleo del tren, parada de destino).

    Un mismo tren aparece en varias capturas con distinto horizonte y, a menudo, con
    distinto régimen. Esa repetición es justamente lo que permite el análisis pareado.
    """
    ficheros = sorted(DIR_PRED.glob("predicciones_*.json"))
    print(f"Ficheros de predicciones: {len(ficheros)}")
    if not ficheros:
        raise SystemExit(f"No hay predicciones en {DIR_PRED}")

    por_tren: dict[tuple[str, str], list[dict]] = {}
    total = 0
    for f in ficheros:
        doc = json.loads(f.read_text(encoding="utf-8"))
        for p in doc["predicciones"]:
            clave = (nucleo_trip(p["trip_id"]), p["destino_stop_id"])
            p["_fichero"] = f.name
            p["_t0"] = desde_iso(p["consultado_en_utc"])
            p["_llegada_teorica"] = desde_iso(p["llegada_teorica_utc"])
            p["_llegada_estimada"] = desde_iso(p["llegada_estimada_utc"])
            p["_horizonte_min"] = (p["_llegada_teorica"] - p["_t0"]).total_seconds() / 60
            por_tren.setdefault(clave, []).append(p)
            total += 1
    for lista in por_tren.values():
        lista.sort(key=lambda p: p["_t0"])
    print(f"  {total} predicciones sobre {len(por_tren)} trenes distintos")
    return por_tren


# ======================================================================= raw ===
def ficheros_en_ventana(desde: datetime, hasta: datetime) -> list[Path]:
    """Capturas cuyo epoch de nombre cae dentro de la ventana."""
    e0, e1 = int(desde.timestamp()), int(hasta.timestamp())
    salida = []
    for carpeta in sorted(p for p in DIR_RAW.iterdir() if p.is_dir()):
        with os.scandir(carpeta) as entradas:
            for e in entradas:
                ep = epoch_de_nombre(e.name)
                if ep is not None and e0 <= ep <= e1:
                    salida.append((ep, Path(e.path)))
    salida.sort()
    return [ruta for _, ruta in salida]


def observar(por_tren: dict[tuple[str, str], list[dict]]) -> dict[tuple[str, str], dict]:
    """Recorre las capturas y se queda con la última observación de cada tren.

    Para cada tren se guardan dos cosas: la última publicación de la parada de
    destino (vía directa) y el último `delay` de tren anterior a la llegada teórica
    (vía de respaldo). Se prefiere la primera cuando existe.
    """
    llegadas = [p[0]["_llegada_teorica"] for p in por_tren.values()]
    desde = min(llegadas) - timedelta(minutes=MARGEN_ANTES_MIN)
    hasta = max(llegadas) + timedelta(minutes=MARGEN_DESPUES_MIN)
    ficheros = ficheros_en_ventana(desde, hasta)
    print(f"\nVentana de búsqueda: {desde:%d/%m %H:%M} a {hasta:%d/%m %H:%M} UTC")
    print(f"  {len(ficheros)} capturas de trip_updates por revisar")

    nucleos = {n for n, _ in por_tren}
    destinos_por_nucleo: dict[str, set[str]] = {}
    for n, d in por_tren:
        destinos_por_nucleo.setdefault(n, set()).add(d)

    obs: dict[tuple[str, str], dict] = {}
    paradas_vistas: dict[str, set[str]] = {}
    leidos = 0
    for ruta in ficheros:
        ep = epoch_de_nombre(ruta.name) or 0
        try:
            with gzip.open(ruta, "rt", encoding="utf-8") as fh:
                doc = json.load(fh)
        except Exception:  # noqa: BLE001 — una captura corrupta no aborta el análisis
            continue
        leidos += 1

        for entidad in (doc.get("payload") or {}).get("entity") or []:
            tu = entidad.get("tripUpdate") or {}
            tid = (tu.get("trip") or {}).get("tripId")
            if not tid:
                continue
            nucleo = nucleo_trip(str(tid))
            if nucleo not in nucleos:
                continue

            delay_tren = tu.get("delay")
            for s in tu.get("stopTimeUpdate") or []:
                sid = str(s.get("stopId") or "")
                paradas_vistas.setdefault(nucleo, set()).add(sid)
                if sid not in destinos_por_nucleo[nucleo]:
                    continue
                llegada = s.get("arrival") or {}
                if llegada.get("time") is None:
                    continue
                clave = (nucleo, sid)
                registro = obs.setdefault(clave, {})
                # La última publicación es la más cercana a la realidad: cuando el
                # tren pasa, la parada desaparece del feed.
                registro["via"] = "parada"
                registro["epoch_obs"] = ep
                registro["arrival_epoch"] = int(llegada["time"])
                registro["arrival_delay"] = llegada.get("delay")
                registro["capturas"] = registro.get("capturas", 0) + 1

            # Respaldo: último delay de tren anterior a la llegada teórica.
            if delay_tren is not None and abs(float(delay_tren)) <= DELAY_MAX_PLAUSIBLE_S:
                for destino in destinos_por_nucleo[nucleo]:
                    clave = (nucleo, destino)
                    teorica = por_tren[clave][0]["_llegada_teorica"].timestamp()
                    if ep > teorica:
                        continue
                    r = obs.setdefault(clave, {})
                    if r.get("epoch_delay", 0) < ep:
                        r["epoch_delay"] = ep
                        r["delay_tren"] = float(delay_tren)

    print(f"  {leidos} capturas leídas · {len(obs)} trenes localizados en el feed")

    # Diagnóstico: si un tren se ve en el feed pero su parada de destino no aparece
    # nunca, conviene saberlo y no confundirlo con un tren ausente.
    sin_parada = [c for c in por_tren
                  if c[0] in paradas_vistas and obs.get(c, {}).get("via") != "parada"]
    if sin_parada:
        print(f"  {len(sin_parada)} trenes vistos pero sin publicación de su destino")
    return obs


# ================================================================ resultados ===
def analizar(por_tren, obs):
    filas = []
    for clave, predicciones in por_tren.items():
        nucleo, destino = clave
        o = obs.get(clave)
        base = predicciones[0]
        if not o:
            filas.append({**{k: base[k] for k in
                             ("line_id", "origen_nombre", "destino_nombre")},
                          "nucleo": nucleo, "estado": "no_visto_en_feed"})
            continue

        teorica = base["_llegada_teorica"]
        if o.get("via") == "parada":
            llegada_real = datetime.fromtimestamp(o["arrival_epoch"], tz=timezone.utc)
            retraso_real = (o.get("arrival_delay")
                            if o.get("arrival_delay") is not None
                            else (llegada_real - teorica).total_seconds())
            via = "parada"
        elif "delay_tren" in o:
            retraso_real = o["delay_tren"]
            llegada_real = teorica + timedelta(seconds=retraso_real)
            via = "delay_tren"
        else:
            filas.append({"nucleo": nucleo, "line_id": base["line_id"],
                          "estado": "sin_dato_utilizable"})
            continue

        retraso_real = float(retraso_real)
        if abs(retraso_real) > DELAY_MAX_PLAUSIBLE_S:
            filas.append({"nucleo": nucleo, "line_id": base["line_id"],
                          "estado": "retraso_implausible",
                          "retraso_real_s": retraso_real})
            continue

        for p in predicciones:
            filas.append({
                "nucleo": nucleo,
                "line_id": p["line_id"],
                "origen_nombre": p["origen_nombre"],
                "destino_nombre": p["destino_nombre"],
                "llegada_teorica_utc": p["llegada_teorica_utc"],
                "consultado_en_utc": p["consultado_en_utc"],
                "horizonte_min": round(p["_horizonte_min"], 1),
                "regime": p["regime"],
                "degradado": ";".join(p["degraded_blocks"]),
                "retraso_predicho_s": p["retraso_p50_s"],
                "retraso_real_s": round(retraso_real, 1),
                "error_retraso_s": round(p["retraso_p50_s"] - retraso_real, 1),
                "llegada_estimada_utc": p["llegada_estimada_utc"],
                "llegada_real_utc": llegada_real.strftime("%Y-%m-%dT%H:%M:%SZ"),
                "error_llegada_s": round(
                    (p["_llegada_estimada"] - llegada_real).total_seconds(), 1),
                "via": via,
                "estado": "ok",
            })
    return filas


def resumir(etiqueta, filas):
    if not filas:
        return
    err = [abs(f["error_retraso_s"]) for f in filas]
    sesgo = [f["error_retraso_s"] for f in filas]
    errl = [abs(f["error_llegada_s"]) for f in filas]
    print(f"  {etiqueta:<28} n={len(filas):>4}  "
          f"MAE retraso {statistics.mean(err):>7.1f} s  "
          f"sesgo {statistics.mean(sesgo):>+8.1f} s  "
          f"mediana |err| {statistics.median(err):>6.1f} s  "
          f"MAE llegada {statistics.mean(errl):>7.1f} s")


def main() -> int:
    print("=" * 92)
    print("VERIFICACIÓN DE PREDICCIONES CONTRA EL FEED REAL")
    print("=" * 92)

    por_tren = cargar_predicciones()
    obs = observar(por_tren)
    filas = analizar(por_tren, obs)

    ok = [f for f in filas if f.get("estado") == "ok"]
    estados: dict[str, int] = {}
    for f in filas:
        if f.get("estado") != "ok":
            estados[f["estado"]] = estados.get(f["estado"], 0) + 1

    print("\n--- Cobertura (esto NO es error del modelo) ---")
    trenes_ok = {f["nucleo"] for f in ok}
    print(f"  trenes con dato utilizable : {len(trenes_ok)} de {len(por_tren)}")
    for estado, n in sorted(estados.items()):
        print(f"  {estado:<28} {n}")

    if not ok:
        print("\nSin datos utilizables. No se calculan métricas.")
        return 1

    print("\n--- Error global ---")
    resumir("todas las predicciones", ok)

    print("\n--- Por régimen ---")
    for r, nombre in (("A", "A · tren ya circulaba"), ("B", "B · aún no había salido")):
        resumir(nombre, [f for f in ok if f["regime"] == r])

    print("\n--- Por horizonte ---")
    tramos = [(0, 15), (15, 30), (30, 60), (60, 120), (120, 10 ** 6)]
    for a, b in tramos:
        sub = [f for f in ok if a <= f["horizonte_min"] < b]
        etiqueta = f"{a}-{b} min" if b < 10 ** 6 else f"más de {a} min"
        resumir(etiqueta, sub)

    print("\n--- Por línea ---")
    for linea in sorted({f["line_id"] for f in ok}):
        resumir(linea, [f for f in ok if f["line_id"] == linea])

    print("\n--- Por vía de medida ---")
    for via, nombre in (("parada", "arrival de la parada"),
                        ("delay_tren", "delay de tren (aprox.)")):
        resumir(nombre, [f for f in ok if f["via"] == via])

    # --- Análisis pareado: el mismo tren visto en los dos regímenes ---
    print("\n--- Pareado: mismo tren, predicho antes de salir y ya en marcha ---")
    pares = []
    for nucleo in trenes_ok:
        suyas = [f for f in ok if f["nucleo"] == nucleo]
        en_b = [f for f in suyas if f["regime"] == "B"]
        en_a = [f for f in suyas if f["regime"] == "A"]
        if en_b and en_a:
            # La peor condición de B (horizonte mayor) frente a la mejor de A.
            b = max(en_b, key=lambda f: f["horizonte_min"])
            a = min(en_a, key=lambda f: f["horizonte_min"])
            pares.append((abs(b["error_retraso_s"]), abs(a["error_retraso_s"])))
    if pares:
        mb = statistics.mean(p[0] for p in pares)
        ma = statistics.mean(p[1] for p in pares)
        mejoran = sum(1 for b, a in pares if a < b)
        print(f"  {len(pares)} trenes con predicción en los dos regímenes")
        print(f"  error medio antes de salir : {mb:7.1f} s")
        print(f"  error medio ya circulando  : {ma:7.1f} s")
        print(f"  mejoran al circular        : {mejoran} de {len(pares)}")
    else:
        print("  No hay ningún tren con predicción en ambos regímenes.")

    columnas = ["nucleo", "line_id", "origen_nombre", "destino_nombre",
                "llegada_teorica_utc", "consultado_en_utc", "horizonte_min", "regime",
                "degradado", "retraso_predicho_s", "retraso_real_s", "error_retraso_s",
                "llegada_estimada_utc", "llegada_real_utc", "error_llegada_s",
                "via", "estado"]
    with open(SALIDA_CSV, "w", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(fh, fieldnames=columnas, extrasaction="ignore")
        w.writeheader()
        w.writerows(filas)
    print(f"\nDetalle guardado en {SALIDA_CSV} ({len(filas)} filas)")
    print("=" * 92)
    return 0


if __name__ == "__main__":
    sys.exit(main())
