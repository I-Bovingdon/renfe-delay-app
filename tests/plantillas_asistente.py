#!/usr/bin/env python3
"""
plantillas_asistente.py — Prueba de no regresión de las plantillas del asistente.

POR QUÉ EXISTE. El multiidioma obliga a sacar todos los textos de chat.py a un
redactor por idioma. Es una refactorización que toca las once respuestas, y un
error de copia (un espacio, una tilde, un plural) no lo detecta la batería de
contención, que solo mira la intención. Esta prueba sí: ejecuta cada manejador
con datos simulados y compara el texto EXACTO con una referencia grabada antes
de la refactorización.

NO LLAMA AL PROVEEDOR. El clasificador se sustituye por una función fija, así que
la prueba es gratuita, determinista y se puede repetir las veces que haga falta.
La batería real (prueba_asistente.py) sigue siendo necesaria para medir el
clasificador; esta mide solo la redacción.

USO
    PC Windows (PowerShell), desde la raíz del repositorio:
        python tests/plantillas_asistente.py --comprobar
    Grabar la referencia (solo con el código ANTERIOR a un cambio de textos):
        python tests/plantillas_asistente.py --grabar
    Ver las respuestas en inglés para revisarlas a ojo:
        python tests/plantillas_asistente.py --idioma en --mostrar

Devuelve 0 si el español coincide carácter a carácter con la referencia.

TFM Cercanías RENFE · UCM · 2026
"""

from __future__ import annotations

import argparse
import inspect
import json
import sys
import tempfile
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

RAIZ = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(RAIZ / "api"))

import chat  # noqa: E402
import historico as historico_mod  # noqa: E402

REFERENCIA = Path(__file__).resolve().parent / "plantillas_asistente_es.json"


# =============================================================== datos simulados ===
def utc(h: int, m: int) -> datetime:
    return datetime(2026, 9, 18, h, m, tzinfo=timezone.utc)


@dataclass
class Tramo:
    line_id: str
    salida_teorica_utc: datetime
    llegada_teorica_utc: datetime


@dataclass
class Trayecto:
    tramos: list


ESTACIONES = {
    "atocha": [{"stop_id": "18000", "nombre": "Atocha"}],
    "alcala": [{"stop_id": "70102", "nombre": "Alcalá de Henares"}],
    "chamartin": [{"stop_id": "17000", "nombre": "Chamartín"}],
    "sol": [{"stop_id": "18002", "nombre": "Sol"}],
    "san": [{"stop_id": "1", "nombre": "San Cristóbal"},
            {"stop_id": "2", "nombre": "San Fernando"},
            {"stop_id": "3", "nombre": "San Yago"}],
}


class Catalogo:
    gtfs_version = "abc123"
    lineas = [{"line_id": l} for l in
              ("C1", "C2", "C3", "C4a", "C4b", "C5", "C7", "C8a", "C9", "C10")]

    def buscar_estaciones(self, texto, limite=5):
        return ESTACIONES.get(chat._normalizar(texto), [])[:limite]


class Cache:
    def __init__(self, datos):
        self.datos = datos

    def estado_linea(self, linea):
        return self.datos.get(linea)


class Alertas:
    def __init__(self, estado):
        self._estado = estado

    def estado(self):
        return self._estado


class Predictor:
    def __init__(self, preds):
        self.preds = preds

    def predict(self, filas):
        return self.preds[: len(filas)]


def incidencia(tipo, lineas, texto, estado="ACTIVA"):
    return {"tipo": tipo, "lineas": lineas, "texto": texto, "estado": estado}


TEXTO_LARGO = ("Por avería en la infraestructura entre Atocha y Villaverde Bajo los "
               "trenes pueden sufrir retrasos. Se recomienda consultar los paneles de "
               "información de las estaciones y utilizar medios alternativos si es "
               "posible durante toda la mañana de hoy.")

ALERTAS_VARIAS = {"feed": {"estado": "OK"}, "incidencias": [
    incidencia("AVERIA", ["C3", "C4"], TEXTO_LARGO),
    incidencia("SERVICIO_BUS", ["C8"], "Servicio por carretera entre Cercedilla y Los Cotos."),
    incidencia("OBRAS", [], "Obras en la red de ancho ibérico."),
    incidencia("RETRASO", ["C4"], "Retrasos en la línea C4."),
    incidencia("SUPRESION", ["C7"], "Supresión de trenes."),
    incidencia("OTRO", ["C2"], "Resuelta.", estado="RESUELTA"),
]}

CACHE_VARIADA = {
    "C1": {"line_delay_mean_30m_s": 60, "line_active_trains_30m": 8},
    "C2": {"line_delay_mean_30m_s": 30, "line_active_trains_30m": 12},
    "C3": {"line_delay_mean_30m_s": 400, "line_active_trains_30m": 10},
    "C4a": {"line_delay_mean_30m_s": 300, "line_active_trains_30m": 6},
    "C4b": {"line_delay_mean_30m_s": 500, "line_active_trains_30m": 4},
    "C5": {"line_delay_mean_30m_s": 700, "line_active_trains_30m": 3},
    "C7": {"line_delay_mean_30m_s": 200, "line_active_trains_30m": 2},
    "C9": {"line_delay_mean_30m_s": 2600, "line_active_trains_30m": 2},
    "C10": {"line_delay_mean_30m_s": 50, "line_active_trains_30m": 9},
}

FILA = {"horizon_s": 1500, "regime": "A", "line_delay_mean_30m_s": 240,
        "alert_averia_30m": 1, "alert_obras_30m": 1, "temp_c": 21.4,
        "precip_mm_1h": 0.6}


def historico_simulado():
    doc = {"ventana": {"desde": "2026-07-01", "hasta": "2026-09-10", "dias": 72},
           "umbral_puntual_s": 180, "observaciones_totales": 1000,
           "lineas": {
               "C4": {"pct_puntual": 81.2, "retraso_mediana_s": 30, "retraso_p90_s": 420},
               "C9": {"pct_puntual": 38.2, "retraso_mediana_s": 900, "retraso_p90_s": 2400},
               "C2": {"pct_puntual": 93.0, "retraso_mediana_s": 0, "retraso_p90_s": 150},
           }}
    tmp = Path(tempfile.mkdtemp()) / "puntualidad.json"
    tmp.write_text(json.dumps(doc), encoding="utf-8")
    return historico_mod.HistoricoPuntualidad(tmp)


def salud():
    return {"contexto": {"vigente": True, "trenes_en_feed": 87},
            "posiciones": {"estado": "OK"},
            "alertas": {"feed": {"estado": "CADUCO"}},
            "predictor": {"backend": "lightgbm"}}


def asistente(cache=None, alertas=None, preds=None, historico="sin"):
    return chat.AsistenteChat(
        catalogo=Catalogo(), cache=Cache(cache or {}),
        almacen_alertas=Alertas(alertas or {"feed": {"estado": "OK"}, "incidencias": []}),
        fuente_meteo=None, fuente_posiciones=None,
        predictor=Predictor(preds or []), fn_salud=salud,
        historico=None if historico == "sin" else historico,
    )


# ====================================================== sustitución de dependencias ===
ESCENARIO_RESOLVER = {"trayectos": [], "aviso": None, "fuera": 0, "vaciar": False}


def resolver_falso(cat, o, d, t0):
    return ESCENARIO_RESOLVER["trayectos"], ESCENARIO_RESOLVER["aviso"]


def filtrar_falso(trayectos, cat, t0):
    if ESCENARIO_RESOLVER["vaciar"]:
        return [], ESCENARIO_RESOLVER["fuera"]
    return trayectos, ESCENARIO_RESOLVER["fuera"]


def construir_falso(tramos, *a, **k):
    return [dict(FILA, regime="A" if i == 0 else "B") for i, _ in enumerate(tramos)]


chat.resolver_trayecto = resolver_falso
chat.features.filtrar_por_dominio = filtrar_falso
chat.features.construir_filas = construir_falso


def llamar(a, nombre, ent, sesion, idioma):
    """Funciona con el código anterior (sin idioma) y con el nuevo."""
    h = getattr(a, nombre)
    if "idioma" in inspect.signature(h).parameters:
        return h(ent, sesion, idioma=idioma)
    return h(ent, sesion)


def responder(a, texto, idioma, ip="1.1.1.1"):
    if "idioma" in inspect.signature(a.responder).parameters:
        return a.responder(texto, ip, sesion="s", idioma=idioma)
    return a.responder(texto, ip, sesion="s")


# ======================================================================= casos ===
def casos(idioma: str) -> dict[str, object]:
    out: dict[str, object] = {}

    def reg(clave, valor):
        out[clave] = valor

    # ---- trayecto
    a = asistente(preds=[
        {"delay_s_p50": 320, "degraded_blocks": ["meteo"]},
        {"delay_s_p50": -900, "degraded_blocks": ["alertas"]},
    ])
    reg("tray_falta", llamar(a, "_h_trayecto", {"origen": "atocha"}, "s", idioma))
    reg("tray_inexistente", llamar(a, "_h_trayecto",
                                   {"origen": "Narnia", "destino": "atocha"}, "s", idioma))
    reg("tray_ambigua", llamar(a, "_h_trayecto",
                               {"origen": "atocha", "destino": "san"}, "s", idioma))
    ESCENARIO_RESOLVER.update(trayectos=[], aviso=None, fuera=0)
    reg("tray_sin_aviso", llamar(a, "_h_trayecto",
                                 {"origen": "atocha", "destino": "alcala"}, "s", idioma))
    for aviso in ("El origen y el destino son la misma estación.",
                  "Alguna de las estaciones no pertenece al núcleo de Madrid.",
                  "No hay trenes directos entre estas estaciones en los próximos 90 minutos.",
                  "No hay tren directo entre estas dos estaciones. Los trayectos con "
                  "transbordo aún no están disponibles."):
        ESCENARIO_RESOLVER.update(trayectos=[], aviso=aviso, fuera=0)
        reg(f"tray_aviso_{aviso[:20]}", llamar(
            a, "_h_trayecto", {"origen": "atocha", "destino": "alcala"}, "s", idioma))
    ESCENARIO_RESOLVER.update(trayectos=[Trayecto([Tramo("C2", utc(9, 0), utc(9, 30))])],
                              aviso=None, fuera=3, vaciar=True)
    reg("tray_todo_fuera", llamar(a, "_h_trayecto",
                                  {"origen": "atocha", "destino": "alcala"}, "s", idioma))
    ESCENARIO_RESOLVER.update(vaciar=False)
    ESCENARIO_RESOLVER.update(trayectos=[
        Trayecto([Tramo("C2", utc(6, 5), utc(6, 40))]),
        Trayecto([Tramo("C9", utc(6, 20), utc(7, 0))]),
    ], aviso=None, fuera=2)
    reg("tray_normal_c9", llamar(a, "_h_trayecto",
                                 {"origen": "atocha", "destino": "alcala"}, "s", idioma))
    ESCENARIO_RESOLVER.update(fuera=0, trayectos=[
        Trayecto([Tramo("C4a", utc(6, 5), utc(6, 40))]),
        Trayecto([Tramo("C4b", utc(6, 20), utc(7, 0))]),
    ])
    a2 = asistente(preds=[{"delay_s_p50": 30}, {"delay_s_p50": 75}])
    reg("tray_normal_limpio", llamar(a2, "_h_trayecto",
                                     {"origen": "chamartin", "destino": "sol"}, "s", idioma))

    # ---- explicar
    reg("explicar_vacio", llamar(asistente(), "_h_explicar", {}, "nadie", idioma))
    reg("explicar", llamar(a, "_h_explicar", {}, "s", idioma))
    a._ultima["s2"] = {"fila": {"horizon_s": 60, "regime": "B"},
                       "pred": {"delay_s_p50": 50}, "linea": "C4a", "destino": "Sol"}
    reg("explicar_minimo", llamar(a, "_h_explicar", {}, "s2", idioma))

    # ---- alertas de red
    reg("red_caduco", llamar(asistente(alertas={"feed": {"estado": "CADUCO"},
                                                "incidencias": []}),
                             "_h_alertas_red", {}, "s", idioma))
    reg("red_vacia", llamar(asistente(), "_h_alertas_red", {}, "s", idioma))
    una = {"feed": {"estado": "OK"},
           "incidencias": [incidencia("SUPRESION", ["C7"], "Supresión de trenes.")]}
    reg("red_una", llamar(asistente(alertas=una), "_h_alertas_red", {}, "s", idioma))
    reg("red_varias", llamar(asistente(alertas=ALERTAS_VARIAS),
                             "_h_alertas_red", {}, "s", idioma))

    # ---- alertas de línea
    av = asistente(alertas=ALERTAS_VARIAS)
    reg("linea_sin", llamar(av, "_h_alertas_linea", {"linea": None}, "s", idioma))
    reg("linea_ninguna", llamar(av, "_h_alertas_linea", {"linea": "C1"}, "s", idioma))
    reg("linea_una", llamar(av, "_h_alertas_linea", {"linea": "C7"}, "s", idioma))
    reg("linea_dos", llamar(av, "_h_alertas_linea", {"linea": "c4"}, "s", idioma))

    # ---- estado de línea
    ac = asistente(cache=CACHE_VARIADA)
    reg("estado_sin", llamar(ac, "_h_estado_linea", {}, "s", idioma))
    reg("estado_sin_datos", llamar(ac, "_h_estado_linea", {"linea": "C8"}, "s", idioma))
    reg("estado_pocos", llamar(ac, "_h_estado_linea", {"linea": "C7"}, "s", idioma))
    reg("estado_c9", llamar(ac, "_h_estado_linea", {"linea": "C9"}, "s", idioma))
    reg("estado_c4", llamar(ac, "_h_estado_linea", {"linea": "C4"}, "s", idioma))
    reg("estado_c2", llamar(ac, "_h_estado_linea", {"linea": "C2"}, "s", idioma))

    # ---- ranking
    reg("ranking_vacio", llamar(asistente(cache={"C1": CACHE_VARIADA["C1"]}),
                                "_h_ranking", {}, "s", idioma))
    dos = {"C1": CACHE_VARIADA["C1"], "C2": CACHE_VARIADA["C2"]}
    reg("ranking_dos", llamar(asistente(cache=dos), "_h_ranking", {}, "s", idioma))
    reg("ranking_completo", llamar(ac, "_h_ranking", {}, "s", idioma))
    una_escasa = dict(dos, C3=CACHE_VARIADA["C3"], C7=CACHE_VARIADA["C7"])
    reg("ranking_una_escasa", llamar(asistente(cache=una_escasa),
                                     "_h_ranking", {}, "s", idioma))

    # ---- histórico
    reg("hist_ausente", llamar(asistente(), "_h_historico", {}, "s", idioma))
    ah = asistente(historico=historico_simulado())
    reg("hist_general", llamar(ah, "_h_historico", {"linea": None}, "s", idioma))
    reg("hist_linea", llamar(ah, "_h_historico", {"linea": "c4b"}, "s", idioma))
    reg("hist_linea_rapida", llamar(ah, "_h_historico", {"linea": "C2"}, "s", idioma))
    reg("hist_desconocida", llamar(ah, "_h_historico", {"linea": "C77"}, "s", idioma))

    # ---- sistema y navegación
    reg("sistema", llamar(a, "_h_sistema", {}, "s", idioma))
    for p in ("llegada", "alertas", "mapa", "cocina"):
        reg(f"navegar_{p}", llamar(a, "_h_navegar", {"pantalla": p}, "s", idioma))

    # ---- fachada: textos fijos y degradaciones
    b = asistente()
    reg("fachada_vacio", responder(b, "   ", idioma))
    b._clasificar = lambda t, h: ({"intencion": "AYUDA"}, {})
    reg("fachada_ayuda", responder(b, "ayuda", idioma))
    b._clasificar = lambda t, h: ({"intencion": "FUERA_DE_ALCANCE"}, {})
    reg("fachada_fuera", responder(b, "poema", idioma))
    b._clasificar = lambda t, h: ({"intencion": "ESTADO_LINEA", "linea": "C1"}, {})
    b.cache = None   # provoca el fallo al componer
    reg("fachada_fallo", responder(b, "c1", idioma))

    def caida(t, h):
        raise RuntimeError("sin red")
    b._clasificar = caida
    reg("fachada_degradado", responder(b, "hola", idioma))
    b.presupuesto.maximo = 0
    reg("fachada_sin_cuota", responder(b, "hola", idioma, ip="2.2.2.2"))
    b.limitador.maximo = 0
    reg("fachada_rapido", responder(b, "hola", idioma, ip="3.3.3.3"))
    return out


def normalizar(valor):
    """Las tuplas pasan a listas para poder comparar con el JSON grabado."""
    return json.loads(json.dumps(valor, ensure_ascii=False))


def comprobar_ingles(resultado: dict) -> int:
    """Dos redes de seguridad para el inglés, sin mirar la redacción a mano.

    1. Ninguna plantilla inglesa deja una frase en español. Las líneas con viñeta
       de las respuestas de incidencias se excluyen: llevan el texto de Renfe, que
       se enseña en español a propósito.
    2. Todo aviso fijo con traducción sigue existiendo, letra a letra, en el
       código que lo genera. Si alguien reescribe la frase en español y olvida la
       tabla, el inglés recibiría el aviso sin traducir; aquí se detecta antes.
    """
    import ast
    import plantillas

    fallos = []
    delatoras = (" la ", " el ", " los ", " está ", " línea ", "incidencia",
                 " retraso", " trenes", " hay ", "puedo", " según ")
    for clave, valor in resultado.items():
        texto = valor["respuesta"] if isinstance(valor, dict) else valor[0]
        if clave.startswith(("red_", "linea_")):
            texto = "\n".join(l for l in texto.splitlines() if not l.startswith("·"))
        for d in delatoras:
            if d in f" {texto.lower()} ":
                fallos.append(f"posible español en {clave}: «{d.strip()}»")

    literales: set[str] = set()
    for fichero in ("main.py", "resolver.py"):
        arbol = ast.parse((RAIZ / "api" / fichero).read_text(encoding="utf-8"))
        literales |= {n.value for n in ast.walk(arbol)
                      if isinstance(n, ast.Constant) and isinstance(n.value, str)}
    for frase in plantillas._AVISOS_FIJOS_EN:
        if frase not in literales:
            fallos.append(f"aviso traducido que ya no existe en el código: {frase[:60]}")

    for f in fallos:
        print("FALLA", f)
    print(f"Inglés: {len(resultado)} respuestas revisadas, "
          f"{len(plantillas._AVISOS_FIJOS_EN)} avisos fijos localizados, "
          f"{len(fallos)} fallos.")
    return 1 if fallos else 0


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--grabar", action="store_true")
    p.add_argument("--comprobar", action="store_true")
    p.add_argument("--mostrar", action="store_true")
    p.add_argument("--idioma", default="es", choices=("es", "en"))
    args = p.parse_args()

    resultado = normalizar(casos(args.idioma))

    if args.mostrar:
        for clave, valor in resultado.items():
            texto = valor["respuesta"] if isinstance(valor, dict) else valor[0]
            print(f"--- {clave}\n{texto}\n")

    if args.grabar:
        REFERENCIA.write_text(json.dumps(resultado, ensure_ascii=False, indent=1),
                              encoding="utf-8")
        print(f"Referencia grabada: {len(resultado)} casos en {REFERENCIA.name}")
        return 0

    if args.comprobar:
        referencia = json.loads(REFERENCIA.read_text(encoding="utf-8"))
        distintos = [c for c in referencia if resultado.get(c) != referencia[c]]
        faltan = sorted(set(referencia) - set(resultado))
        for c in distintos:
            print(f"DISTINTO  {c}\n  antes : {referencia[c]}\n  ahora : {resultado.get(c)}")
        print(f"\n{len(referencia) - len(distintos)} de {len(referencia)} respuestas "
              f"en español idénticas a la referencia.")
        return 1 if distintos or faltan else 0

    if args.idioma == "en" and not args.mostrar:
        return comprobar_ingles(resultado)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
