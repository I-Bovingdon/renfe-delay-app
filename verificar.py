#!/usr/bin/env python3
"""
verificar.py — Comprobación de coherencia entre los ficheros de la aplicación.

POR QUÉ EXISTE. El 13/09 la aplicación quedó rota en producción porque `app.js` se
actualizó en una conversación y `index.html` en otra: el JavaScript enganchaba un
listener a `$("filtros-mapa")`, que no existía en el HTML, y la excepción mataba todo
el código posterior (autocompletado, colores de línea, versión GTFS). La página
cargaba y parecía correcta, pero no funcionaba nada.

Ese fallo es invisible a la vista y evidente a una comprobación automática. Este
script hace esa comprobación en dos segundos.

USO
    PC Windows (PowerShell), desde la raíz del repositorio:
        python verificar.py

    VPS Ubuntu, antes de reiniciar el servicio:
        cd /home/tfm/renfe-delay-app && python3 verificar.py

Devuelve código de salida 0 si todo cuadra y 1 si hay algún fallo, de modo que
puede encadenarse:
        python3 verificar.py && sudo systemctl restart tfm-app

TFM Cercanías RENFE · UCM · 2026
"""

from __future__ import annotations

import ast
import base64
import re
import subprocess
import sys
import zlib
from pathlib import Path

RAIZ = Path(__file__).resolve().parent
WEB = RAIZ / "web"
API = RAIZ / "api"

# Clases que el JavaScript genera en tiempo de ejecución. Si una no tiene estilo,
# el elemento aparece sin formato y el fallo solo se ve mirando la pantalla.
CLASES_GENERADAS = {
    "filtro--activo", "filtro--vacio", "marcador-tren", "alerta__leer-mas",
    "alerta__texto--expandido", "nav__item--activo", "panel--mapa", "mapa__pie",
    "alerta--resuelta", "alerta__etiqueta--planificada",
}

fallos: list[str] = []


def comprobar(descripcion: str, condicion: bool, detalle: str = "") -> None:
    marca = "ok  " if condicion else "FALLA"
    print(f"  [{marca}] {descripcion}" + (f" — {detalle}" if detalle and not condicion else ""))
    if not condicion:
        fallos.append(descripcion)


def claves_de_bloque(i18n: str, inicio: str, fin: str) -> set[str]:
    """Claves de un bloque de idioma de i18n.js, delimitado por sus comentarios."""
    bloque = i18n.split(f"// ---- {inicio} ----", 1)[1].split(f"// ---- {fin} ----", 1)[0]
    return set(re.findall(r'^\s*"([\w.]+)":', bloque, flags=re.MULTILINE))


def comprobar_idiomas(html: str, js: str, i18n: str) -> None:
    """Un texto sin traducir no rompe la página (t() devuelve la clave), pero se ve
    en pantalla. Estas comprobaciones lo detectan antes de desplegar."""
    es = claves_de_bloque(i18n, "ES", "EN")
    en = claves_de_bloque(i18n, "EN", "FIN")
    comprobar(f"español e inglés tienen las mismas {len(es)} claves", es == en,
              f"solo ES: {sorted(es - en)} · solo EN: {sorted(en - es)}")

    # Claves literales que usa app.js: t("x") y cadenas con forma de clave
    # (las que se eligen con un ternario antes de llamar a t).
    prefijos = {c.split(".")[0] for c in es}
    usadas_js = {c for c in re.findall(r'"([a-z]+\.[\w.]+)"', js)
                 if c.split(".")[0] in prefijos}
    faltan = sorted(usadas_js - es)
    comprobar(f"las {len(usadas_js)} claves que usa app.js existen", not faltan,
              f"faltan: {faltan}")

    usadas_html = set(re.findall(r'data-i18n(?:-[a-z-]+)?="([^"]+)"', html))
    faltan = sorted(usadas_html - es)
    comprobar(f"las {len(usadas_html)} claves que usa index.html existen", not faltan,
              f"faltan: {faltan}")

    # Claves que app.js compone en tiempo de ejecución a partir de códigos de la
    # API. Si la API añade un tipo de incidencia, tiene que añadirse aquí.
    alertas_py = (API / "alertas.py").read_text(encoding="utf-8")
    bloque = alertas_py.split("IMPACTO_POR_TIPO = {", 1)[1].split("}", 1)[0]
    tipos = set(re.findall(r'"([A-Z_]+)":', bloque))
    impactos = set(re.findall(r'"([A-Z_]+)"\s*,?\s*(?:#.*)?$', bloque, flags=re.MULTILINE))
    dinamicas = {f"tipo.{x}" for x in tipos} | {f"impacto.{x}" for x in impactos}
    faltan = sorted(dinamicas - es)
    comprobar(f"los {len(tipos)} tipos y {len(impactos)} impactos de la API tienen texto",
              not faltan, f"faltan: {faltan}")


def main() -> int:
    html = (WEB / "index.html").read_text(encoding="utf-8")
    js = (WEB / "app.js").read_text(encoding="utf-8")
    i18n = (WEB / "i18n.js").read_text(encoding="utf-8")
    css = (WEB / "estilos.css").read_text(encoding="utf-8")

    print("Coherencia HTML <-> JavaScript")

    # 1. Todo id que el JS busca tiene que existir en el HTML. Es el fallo que
    #    rompió la aplicación: $("x") devuelve null y .addEventListener revienta.
    ids_html = set(re.findall(r'id="([^"]+)"', html))
    ids_js = set(re.findall(r'\$\("([^"]+)"\)', js))
    ids_js |= set(re.findall(r'mostrar\("([^"]+)"', js))
    ausentes = sorted(ids_js - ids_html)
    comprobar(f"los {len(ids_js)} id que usa el JS existen en el HTML",
              not ausentes, f"faltan: {ausentes}")

    # 2. Contenedor que Leaflet monta por nombre, no por referencia.
    objetivo = re.search(r'L\.map\("([^"]+)"', js)
    if objetivo:
        comprobar(f'el contenedor L.map("{objetivo.group(1)}") existe',
                  objetivo.group(1) in ids_html)

    # 3. Orden de carga: app.js referencia L al construir el mapa y usa t() de
    #    i18n.js desde la primera línea que pinta texto.
    i_lib, i_app = html.find("leaflet.js"), html.find('src="app.js"')
    i_i18n = html.find('src="i18n.js"')
    comprobar("leaflet.js se carga antes que app.js", 0 < i_lib < i_app)
    comprobar("i18n.js se carga antes que app.js", 0 < i_i18n < i_app)
    comprobar("leaflet.css está enlazado", "leaflet.css" in html)

    # 4. El botón de Mapa no puede quedarse deshabilitado tras activar la pantalla.
    comprobar("el botón de Mapa está habilitado",
              'data-pantalla="mapa" type="button" disabled' not in html)

    print("\nIdiomas")
    comprobar_idiomas(html, js, i18n)

    print("\nCoherencia JavaScript <-> CSS")
    sin_estilo = sorted(c for c in CLASES_GENERADAS if f".{c}" not in css)
    comprobar("las clases que genera el JS tienen estilo",
              not sin_estilo, f"sin estilo: {sin_estilo}")

    print("\nIntegridad de recursos incrustados")
    # El base64 del logo se corrompió una vez al reescribir el HTML a mano: un solo
    # carácter cambiado rompe el zlib y el navegador pinta media imagen.
    b64 = re.search(r'base64,([A-Za-z0-9+/=]+)"', html)
    if b64:
        try:
            datos = base64.b64decode(b64.group(1))
            i, idat = 8, b""
            while i < len(datos):
                ln = int.from_bytes(datos[i:i + 4], "big")
                if datos[i + 4:i + 8] == b"IDAT":
                    idat += datos[i + 8:i + 8 + ln]
                i += 12 + ln
            filas = len(zlib.decompress(idat)) // 149
            comprobar(f"el PNG del logo descomprime entero ({filas}/148 filas)",
                      filas == 148)
        except (zlib.error, ValueError) as exc:
            comprobar("el PNG del logo descomprime entero", False, str(exc))

    print("\nSintaxis")
    for nombre in ("i18n.js", "app.js"):
        try:
            r = subprocess.run(["node", "--check", str(WEB / nombre)],
                               capture_output=True, text=True)
            comprobar(nombre, r.returncode == 0, r.stderr.strip()[:200])
        except FileNotFoundError:
            print(f"  [salta] {nombre} — node no está instalado en esta máquina")

    for fichero in sorted(API.glob("*.py")) if API.is_dir() else []:
        try:
            ast.parse(fichero.read_text(encoding="utf-8"))
            comprobar(fichero.name, True)
        except SyntaxError as exc:
            comprobar(fichero.name, False, f"línea {exc.lineno}: {exc.msg}")

    print()
    if fallos:
        print(f"{len(fallos)} comprobaciones fallidas. NO despliegues:")
        for f in fallos:
            print(f"  - {f}")
        return 1
    print("Todo coherente. Listo para desplegar.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
