#!/usr/bin/env bash
#
# regenerar_catalogo.sh — Regenera el catálogo GTFS de la aplicación web.
#
# POR QUÉ EXISTE. El cron de las 03:45 descarga el GTFS estático nuevo cada día, pero
# nadie regeneraba el catálogo que la aplicación carga al arrancar. Resultado: entre el
# 29/08 y el 12/09 la web sirvió horarios caducados, incluido el cambio de horario del
# inicio del curso escolar (07/09), sin que ningún error lo delatara. Este script cierra
# ese eslabón.
#
# CÓMO. Genera el catálogo en un fichero temporal, lo VERIFICA, y solo si pasa la
# verificación lo pone en producción y reinicia el servicio. Un GTFS corrupto o de
# cobertura incompleta no llega a producción: se queda el catálogo anterior, que está
# viejo pero funciona. Degradar en silencio es peor que no actualizar.
#
# Se ejecuta como usuario tfm, encadenado tras la descarga del GTFS.
#
# TFM Cercanías RENFE · UCM · 2026

set -uo pipefail

APP="/home/tfm/renfe-delay-app"
GTFS_DIR="/home/tfm/data-renfe/gtfs_static"
CATALOGO="$APP/datos/catalogo.json"
TEMPORAL="$APP/datos/catalogo_tmp.json"
LOG="/home/tfm/logs/regenerar_catalogo.log"
PYTHON="/usr/bin/python3"

# Umbrales de aceptación. Salen de lo medido el 12/09 sobre un catálogo bueno:
# 95 estaciones, 12 líneas, 36.623 trips. Se dejan holgados para tolerar la variación
# normal entre versiones semanales del GTFS, pero no un catálogo medio vacío.
MIN_ESTACIONES=90
MIN_LINEAS=11
MIN_TRIPS=20000

registrar() { echo "[$(date -u '+%Y-%m-%d %H:%M:%S UTC')] $*"; }

registrar "=== Inicio de la regeneración del catálogo ==="

# --- 1. GTFS más reciente por fecha de modificación -----------------------------------
ZIP="$(ls -t "$GTFS_DIR"/*.zip 2>/dev/null | head -1)"
if [[ -z "$ZIP" ]]; then
    registrar "ERROR: no hay ningún zip de GTFS en $GTFS_DIR. Se conserva el catálogo actual."
    exit 1
fi
registrar "GTFS de origen: $(basename "$ZIP")"

# --- 2. Generar en temporal, nunca encima del bueno -----------------------------------
if ! "$PYTHON" "$APP/scripts/generar_catalogo.py" --gtfs "$ZIP" --salida "$TEMPORAL"; then
    registrar "ERROR: generar_catalogo.py falló. Se conserva el catálogo actual."
    rm -f "$TEMPORAL"
    exit 1
fi

# --- 3. Verificar antes de publicar ---------------------------------------------------
# Comprueba que el JSON es legible y que la cobertura es la esperada. Sin esto, un GTFS
# truncado o de un núcleo equivocado dejaría la web sin estaciones y solo nos
# enteraríamos con alguien delante.
VERIFICACION=$(cd "$APP/api" && "$PYTHON" - "$TEMPORAL" "$MIN_ESTACIONES" "$MIN_LINEAS" "$MIN_TRIPS" <<'PY'
import sys

# Se carga con la MISMA clase que usa la aplicación al arrancar, no leyendo el JSON a
# mano: así la verificación prueba exactamente lo que hará el servicio, y un cambio de
# formato del catálogo se detecta aquí y no en el arranque.
from catalogo import Catalogo

ruta, min_est, min_lin, min_trips = sys.argv[1], *map(int, sys.argv[2:5])

try:
    cat = Catalogo(ruta)
except Exception as exc:  # noqa: BLE001 — cualquier fallo de carga invalida el catálogo
    print(f"FALLO el catálogo no se puede cargar: {exc}")
    raise SystemExit(1)

n_est = len(cat.estaciones)
n_lin = len(cat.lineas)
n_trips = len(cat.trips)

problemas = []
if n_est < min_est:
    problemas.append(f"estaciones {n_est} < {min_est}")
if n_lin < min_lin:
    problemas.append(f"lineas {n_lin} < {min_lin}")
if n_trips < min_trips:
    problemas.append(f"trips {n_trips} < {min_trips}")

resumen = f"{n_est} estaciones · {n_lin} lineas · {n_trips} trips · gtfs {cat.gtfs_version}"
if problemas:
    print(f"FALLO {resumen} · {'; '.join(problemas)}")
    raise SystemExit(1)

print(f"OK {resumen}")
PY
)
ESTADO=$?
registrar "Verificación: $VERIFICACION"

if [[ $ESTADO -ne 0 ]]; then
    registrar "ERROR: el catálogo nuevo no pasa la verificación. Se conserva el actual."
    rm -f "$TEMPORAL"
    exit 1
fi

# --- 4. Publicar -----------------------------------------------------------------------
# Copia de seguridad del anterior (solo la última) y sustitución atómica con mv, que en
# el mismo sistema de ficheros es un renombrado: la aplicación nunca ve medio fichero.
if [[ -f "$CATALOGO" ]]; then
    cp -p "$CATALOGO" "$CATALOGO.anterior"
fi
mv "$TEMPORAL" "$CATALOGO"
registrar "Catálogo publicado en $CATALOGO"

# --- 5. Reiniciar: el catálogo solo se lee al arrancar ---------------------------------
if sudo -n systemctl restart tfm-app; then
    sleep 8
    VERSION=$(curl -s --max-time 10 http://127.0.0.1:8000/api/salud \
              | "$PYTHON" -c "import json,sys; print(json.load(sys.stdin)['catalogo']['gtfs_version'])" 2>/dev/null)
    if [[ -n "$VERSION" ]]; then
        registrar "Servicio reiniciado y respondiendo · gtfs_version en uso: $VERSION"
    else
        registrar "AVISO: el servicio se reinició pero /api/salud no respondió. Revisar journalctl -u tfm-app."
    fi
else
    registrar "ERROR: no se pudo reiniciar tfm-app. El catálogo nuevo está en disco pero la aplicación sigue con el viejo en memoria."
    exit 1
fi

registrar "=== Fin ==="
