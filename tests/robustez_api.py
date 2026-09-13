#!/usr/bin/env python3
"""
robustez_api.py — Batería de robustez de caja negra contra el servicio en producción.

Solo hace peticiones HTTP. No escribe ficheros, no toca el servicio, no toca los
colectores. Pasa por Caddy y por HTTPS, de modo que ejercita la misma cadena que
verá cualquiera que use la aplicación.

Uso (VPS):
    /usr/bin/python3 robustez_api.py
    /usr/bin/python3 robustez_api.py --base http://127.0.0.1:8000   # sin pasar por Caddy

Cada prueba declara qué espera. Una prueba en rojo no significa necesariamente un
fallo grave: significa que el comportamiento no es el que esperábamos y hay que
mirarlo antes de la defensa.

TFM Cercanías RENFE · UCM · 2026
"""

from __future__ import annotations

import argparse
import json
import statistics
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor

BASE = "https://cercanias-madrid.es"
TIMEOUT = 20

VERDE, ROJO, AMBAR, FIN = "\033[92m", "\033[91m", "\033[93m", "\033[0m"
resultados: list[tuple[str, str, str]] = []   # (estado, nombre, detalle)


def registrar(ok: bool | None, nombre: str, detalle: str = "") -> None:
    estado = "OK" if ok is True else ("AVISO" if ok is None else "FALLO")
    color = VERDE if ok is True else (AMBAR if ok is None else ROJO)
    resultados.append((estado, nombre, detalle))
    print(f"  [{color}{estado:<5}{FIN}] {nombre}" + (f"  ·  {detalle}" if detalle else ""))


def pedir(metodo: str, ruta: str, cuerpo=None, crudo: bytes | None = None):
    """Devuelve (codigo, datos, ms). Nunca lanza: un error HTTP es un resultado."""
    url = BASE + ruta
    datos_envio = crudo if crudo is not None else (
        json.dumps(cuerpo).encode("utf-8") if cuerpo is not None else None
    )
    req = urllib.request.Request(url, data=datos_envio, method=metodo)
    if datos_envio is not None:
        req.add_header("Content-Type", "application/json")

    t0 = time.perf_counter()
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:
            ms = (time.perf_counter() - t0) * 1000
            texto = resp.read().decode("utf-8", errors="replace")
            try:
                return resp.status, json.loads(texto), ms
            except json.JSONDecodeError:
                return resp.status, texto, ms
    except urllib.error.HTTPError as e:
        ms = (time.perf_counter() - t0) * 1000
        texto = e.read().decode("utf-8", errors="replace")
        try:
            return e.code, json.loads(texto), ms
        except json.JSONDecodeError:
            return e.code, texto, ms
    except Exception as exc:  # noqa: BLE001 — timeout, DNS, TLS...
        ms = (time.perf_counter() - t0) * 1000
        return 0, f"{type(exc).__name__}: {exc}", ms


def id_de(nombre: str) -> str | None:
    codigo, datos, _ = pedir("GET", f"/api/estaciones?q={urllib.parse.quote(nombre)}")
    if codigo == 200 and isinstance(datos, list) and datos:
        return datos[0]["stop_id"]
    return None


# =========================================================================== 1 ===
def bloque_salud():
    print("\n--- 1. Endpoints básicos y diagnóstico ---")

    codigo, datos, ms = pedir("GET", "/api/salud")
    registrar(codigo == 200, "GET /api/salud responde 200", f"{ms:.0f} ms")
    if codigo != 200 or not isinstance(datos, dict):
        return

    for bloque in ("catalogo", "contexto", "posiciones", "meteo", "alertas"):
        registrar(bloque in datos, f"salud incluye el bloque '{bloque}'")

    meteo = datos.get("meteo", {})
    registrar(meteo.get("vigente") is True, "meteo vigente",
              f"{meteo.get('estaciones_corredor')} estaciones · "
              f"{meteo.get('paradas_cubiertas')} paradas")

    cat = datos.get("catalogo", {})
    registrar(cat.get("estaciones") == 95, "catálogo con 95 estaciones",
              f"gtfs {cat.get('gtfs_version')}")

    for ruta, clave in (("/api/lineas", "lineas"), ("/api/alertas", None),
                        ("/api/mapa", None)):
        codigo, _, ms = pedir("GET", ruta)
        registrar(codigo == 200, f"GET {ruta} responde 200", f"{ms:.0f} ms")


# =========================================================================== 2 ===
def bloque_estaciones():
    print("\n--- 2. Buscador de estaciones: entradas raras ---")

    codigo, datos, _ = pedir("GET", "/api/estaciones")
    registrar(codigo == 200 and isinstance(datos, list) and len(datos) == 95,
              "listado completo devuelve 95 estaciones",
              f"{len(datos) if isinstance(datos, list) else '?'}")

    casos = [
        ("chamar", True, "prefijo normal"),
        ("alcalá", True, "con acento"),
        ("alcala", True, "sin acento"),
        ("ALCALA", True, "en mayúsculas"),
        ("  atocha  ", True, "con espacios alrededor"),
        ("a", None, "una sola letra"),
        ("", None, "cadena vacía"),
        ("zzzzzzzz", None, "sin coincidencias"),
        ("'; DROP TABLE estaciones; --", None, "intento de inyección"),
        ("<script>alert(1)</script>", None, "intento de script"),
        ("ñ" * 500, None, "500 caracteres"),
        ("%%%", None, "caracteres de formato"),
    ]
    for consulta, espera_resultados, etiqueta in casos:
        codigo, datos, _ = pedir("GET", f"/api/estaciones?q={urllib.parse.quote(consulta)}")
        n = len(datos) if isinstance(datos, list) else -1
        if espera_resultados:
            registrar(codigo == 200 and n > 0, f"búsqueda: {etiqueta}", f"{n} resultados")
        else:
            # Sin resultados es correcto. Lo que no puede pasar es un 500.
            ok = codigo in (200, 422)
            registrar(True if ok else False, f"búsqueda: {etiqueta}",
                      f"HTTP {codigo}, {n} resultados")

    codigo, datos, _ = pedir("GET", "/api/alertas?linea=C99")
    registrar(codigo == 200, "alertas de una línea inexistente no rompen", f"HTTP {codigo}")


# =========================================================================== 3 ===
def bloque_consulta(ids: dict[str, str]):
    print("\n--- 3. Consulta de trayecto: casos límite ---")

    atocha = ids.get("atocha")
    alcala = ids.get("alcala")
    chamartin = ids.get("chamartin")
    leganes = ids.get("leganes")

    if not atocha or not alcala:
        registrar(False, "no se han podido resolver las estaciones de prueba")
        return

    # --- caso normal, de referencia ---
    codigo, datos, ms = pedir("POST", "/api/consulta",
                              {"origen": atocha, "destino": alcala})
    ok = codigo == 200 and isinstance(datos, dict) and datos.get("opciones")
    detalle = ""
    if ok:
        t = datos["opciones"][0]["tramos"][0]
        detalle = (f"{t['line_id']} · degradados {t['degraded_blocks']} · "
                   f"p50 {t['retraso_s']['p50']} s · {ms:.0f} ms")
    registrar(bool(ok), "trayecto normal con directos", detalle)

    if ok:
        registrar(t["degraded_blocks"] == [], "sin bloques degradados",
                  str(t["degraded_blocks"]))

    # --- origen igual a destino ---
    codigo, datos, _ = pedir("POST", "/api/consulta",
                             {"origen": atocha, "destino": atocha})
    ok = codigo == 200 and isinstance(datos, dict) and datos.get("aviso")
    registrar(bool(ok), "origen igual a destino: aviso, no error",
              str(datos.get("aviso"))[:60] if isinstance(datos, dict) else str(datos)[:60])

    # --- estación inexistente ---
    codigo, datos, _ = pedir("POST", "/api/consulta",
                             {"origen": "99999", "destino": alcala})
    registrar(codigo == 404, "estación inexistente devuelve 404", f"HTTP {codigo}")

    # --- sin tren directo ---
    if chamartin and leganes:
        codigo, datos, _ = pedir("POST", "/api/consulta",
                                 {"origen": chamartin, "destino": leganes})
        ok = codigo == 200 and isinstance(datos, dict) and datos.get("aviso")
        registrar(bool(ok), "trayecto sin directo: se explica, no se inventa",
                  str(datos.get("aviso"))[:70] if isinstance(datos, dict) else "")

    # --- fechas ---
    fechas = [
        ("2026-09-18T07:30:00Z", 200, "fecha válida futura"),
        ("2026-09-18T07:30:00+02:00", 200, "con desplazamiento explícito"),
        ("mañana por la mañana", 400, "texto libre"),
        ("2026-13-45T99:99:99Z", 400, "fecha imposible"),
        ("", 200, "cadena vacía (equivale a ahora)"),
        ("2020-01-01T00:00:00Z", 200, "muy en el pasado"),
        ("2030-01-01T00:00:00Z", 200, "muy en el futuro"),
    ]
    for valor, esperado, etiqueta in fechas:
        codigo, datos, _ = pedir("POST", "/api/consulta",
                                 {"origen": atocha, "destino": alcala,
                                  "salida_desde_utc": valor})
        extra = ""
        if codigo == 200 and isinstance(datos, dict):
            extra = f"{len(datos.get('opciones', []))} opciones"
            if not datos.get("opciones") and datos.get("aviso"):
                extra += f" · {str(datos['aviso'])[:40]}"
        registrar(codigo == esperado, f"fecha: {etiqueta}", f"HTTP {codigo} {extra}")

    # --- cuerpos malformados ---
    malos = [
        ({"origen": atocha}, 422, "falta el destino"),
        ({}, 422, "cuerpo vacío"),
        ({"origen": 12345, "destino": alcala}, (200, 404, 422), "origen numérico"),
        ({"origen": "x" * 5000, "destino": alcala}, (404, 422), "origen larguísimo"),
        ({"origen": None, "destino": alcala}, 422, "origen nulo"),
    ]
    for cuerpo, esperado, etiqueta in malos:
        codigo, _, _ = pedir("POST", "/api/consulta", cuerpo)
        esperados = esperado if isinstance(esperado, tuple) else (esperado,)
        registrar(codigo in esperados, f"cuerpo: {etiqueta}",
                  f"HTTP {codigo} (esperado {esperados})")

    codigo, _, _ = pedir("POST", "/api/consulta", crudo=b"{esto no es json")
    registrar(codigo in (400, 422), "JSON malformado", f"HTTP {codigo}")

    codigo, _, _ = pedir("GET", "/api/consulta")
    registrar(codigo == 405, "método incorrecto devuelve 405", f"HTTP {codigo}")


# =========================================================================== 4 ===
def bloque_rendimiento(ids: dict[str, str]):
    print("\n--- 4. Latencia y concurrencia ---")

    atocha, alcala = ids.get("atocha"), ids.get("alcala")
    if not atocha or not alcala:
        return
    cuerpo = {"origen": atocha, "destino": alcala}

    tiempos = []
    for _ in range(20):
        codigo, _, ms = pedir("POST", "/api/consulta", cuerpo)
        if codigo == 200:
            tiempos.append(ms)
    if tiempos:
        p50 = statistics.median(tiempos)
        p90 = sorted(tiempos)[int(len(tiempos) * 0.9) - 1]
        registrar(p90 < 1000, "20 consultas secuenciales por debajo de 1 s (p90)",
                  f"p50 {p50:.0f} ms · p90 {p90:.0f} ms · max {max(tiempos):.0f} ms")
    else:
        registrar(False, "20 consultas secuenciales", "ninguna respondió")

    # Concurrencia moderada: la demo puede tener varias personas mirando a la vez.
    # 10 simultáneas es realista y no estresa la cuota de CPU del servicio.
    t0 = time.perf_counter()
    with ThreadPoolExecutor(max_workers=10) as pool:
        futuros = [pool.submit(pedir, "POST", "/api/consulta", cuerpo) for _ in range(10)]
        salidas = [f.result() for f in futuros]
    total = (time.perf_counter() - t0) * 1000
    exitos = sum(1 for c, _, _ in salidas if c == 200)
    peor = max(ms for _, _, ms in salidas)
    registrar(exitos == 10, "10 consultas simultáneas",
              f"{exitos}/10 · tanda {total:.0f} ms · peor {peor:.0f} ms")

    # Mezcla de endpoints a la vez, que es lo que hace la interfaz al arrancar.
    with ThreadPoolExecutor(max_workers=6) as pool:
        futuros = [
            pool.submit(pedir, "GET", "/api/mapa"),
            pool.submit(pedir, "GET", "/api/alertas"),
            pool.submit(pedir, "GET", "/api/lineas"),
            pool.submit(pedir, "GET", "/api/estaciones"),
            pool.submit(pedir, "POST", "/api/consulta", cuerpo),
            pool.submit(pedir, "GET", "/api/salud"),
        ]
        salidas = [f.result() for f in futuros]
    exitos = sum(1 for c, _, _ in salidas if c == 200)
    registrar(exitos == 6, "seis endpoints distintos a la vez", f"{exitos}/6")


# =========================================================================== 5 ===
def bloque_estabilidad():
    print("\n--- 5. El servicio sigue sano después de todo lo anterior ---")

    codigo, datos, ms = pedir("GET", "/api/salud")
    if codigo != 200 or not isinstance(datos, dict):
        registrar(False, "salud tras la batería", f"HTTP {codigo}")
        return

    registrar(True, "salud responde tras la batería", f"{ms:.0f} ms")
    ctx = datos.get("contexto", {})
    registrar(ctx.get("vigente") is not False, "contexto de red sigue vigente",
              f"fallidos {ctx.get('refrescos_fallidos')}")
    meteo = datos.get("meteo", {})
    registrar(meteo.get("ultimo_error") is None, "meteo sin errores",
              str(meteo.get("ultimo_error")))


def main() -> int:
    global BASE
    p = argparse.ArgumentParser(description="Robustez de la API del TFM Cercanías")
    p.add_argument("--base", default=BASE)
    BASE = p.parse_args().base

    print("=" * 78)
    print(f"BATERÍA DE ROBUSTEZ · {BASE}")
    print("=" * 78)

    bloque_salud()
    bloque_estaciones()

    ids = {n: id_de(t) for n, t in (
        ("atocha", "atocha"), ("alcala", "alcala de henares"),
        ("chamartin", "chamartin"), ("leganes", "leganes"),
    )}
    print(f"\nEstaciones de prueba: {ids}")

    bloque_consulta(ids)
    bloque_rendimiento(ids)
    bloque_estabilidad()

    print("\n" + "=" * 78)
    n_ok = sum(1 for e, _, _ in resultados if e == "OK")
    n_av = sum(1 for e, _, _ in resultados if e == "AVISO")
    n_ko = sum(1 for e, _, _ in resultados if e == "FALLO")
    print(f"RESUMEN: {n_ok} OK · {n_av} avisos · {n_ko} fallos  (de {len(resultados)})")
    if n_ko:
        print("\nPruebas en rojo:")
        for estado, nombre, detalle in resultados:
            if estado == "FALLO":
                print(f"  · {nombre}  ·  {detalle}")
    print("=" * 78)
    return 1 if n_ko else 0


if __name__ == "__main__":
    sys.exit(main())
