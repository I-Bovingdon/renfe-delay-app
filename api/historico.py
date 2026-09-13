"""
historico.py — Puntualidad histórica por línea, servida desde memoria.

Carga el JSON que produce `scripts/generar_puntualidad.py` (unos pocos KB) y compone
las respuestas de la intención PUNTUALIDAD_HISTORICA. El servicio no toca Parquet en
ninguna ruta: el camino de la petición sigue siendo una consulta a un diccionario.

DOS SALVEDADES QUE VIAJAN SIEMPRE CON EL DATO
----------------------------------------------
No son adornos. Son las dos preguntas que un tribunal hará sobre cualquier cifra
histórica de este proyecto, y es mejor que las conteste el propio producto:

  1. **Es la estimación publicada por Renfe**, no el retraso reconstruido que modela
     el TFM. Se elige esa magnitud para que sea comparable con el estado en vivo que
     el asistente da en la misma conversación, que también sale del feed.

  2. **La ventana de captura es de verano.** Arranca el 13/06/2026 y contiene un
     único festivo y una sola semana lectiva. No describe el comportamiento de la red
     en régimen de curso escolar. Es la misma limitación que ya está documentada para
     el entrenamiento del modelo.

POR QUÉ SE RESPONDE CON EL PORCENTAJE DE TRENES PUNTUALES Y NO CON LA MEDIA
---------------------------------------------------------------------------
La distribución tiene cola larga: mediana en torno a 0 s y p99 cerca de 28 minutos.
Con esa forma, la media la mueve un puñado de trenes muy retrasados y ordena las
líneas de una manera que no se corresponde con lo que vive un viajero. El porcentaje
de trenes que llegan dentro del margen es más robusto y más fácil de entender. La
media y la mediana se guardan igualmente y se ofrecen al preguntar por una línea
concreta.

TFM Cercanías RENFE · UCM · 2026
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any

log = logging.getLogger(__name__)

# La salvedad de ventana, en una frase. Se añade a la primera respuesta de cada
# conversación, no a todas: repetirla en cada turno sería ruido y la gente deja de
# leerla, que es justo lo contrario de lo que se busca.
SALVEDAD = ("Son datos de {desde} a {hasta} ({dias} días) y miden el retraso que "
            "publica Renfe, no la predicción del modelo.")


def _minutos(segundos: float) -> str:
    m = segundos / 60.0
    if abs(m) < 1:
        return "menos de un minuto"
    return f"{m:.0f} min"


class HistoricoPuntualidad:
    """Resumen por línea cargado en memoria. Inmutable tras el arranque."""

    def __init__(self, ruta: str | Path):
        ruta = Path(ruta)
        with open(ruta, encoding="utf-8") as f:
            doc: dict[str, Any] = json.load(f)

        self.lineas: dict[str, dict] = doc["lineas"]
        if not self.lineas:
            raise ValueError(f"{ruta} no contiene ninguna línea con datos")

        self.ventana = doc["ventana"]
        self.umbral_s = int(doc.get("umbral_puntual_s", 180))
        self.generado = doc.get("generado_utc")
        self.observaciones = doc.get("observaciones_totales", 0)
        self._salvedad_dada = False

        log.info(
            "Histórico de puntualidad: %d líneas · %s a %s · %d observaciones",
            len(self.lineas), self.ventana["desde"], self.ventana["hasta"],
            self.observaciones,
        )

    # ------------------------------------------------------------------ apoyo ---
    def _salvedad(self) -> str:
        """Se da una vez por proceso y por conversación larga. Ver la cabecera."""
        return " " + SALVEDAD.format(**self.ventana)

    def _normalizar(self, codigo: str | None) -> str | None:
        """'c4b' -> 'C4'. Devuelve None si la línea no está en el resumen."""
        if not codigo:
            return None
        c = str(codigo).strip().upper()
        if c and c[-1] in ("A", "B"):
            c = c[:-1]
        return c if c in self.lineas else None

    # --------------------------------------------------------------- respuesta ---
    def responder(self, codigo_linea: str | None = None) -> str:
        """Texto para la intención PUNTUALIDAD_HISTORICA.

        Con línea, da su ficha. Sin línea, da la más y la menos puntual. En ambos
        casos el texto lo compone esta plantilla sobre los números del resumen: el
        modelo de lenguaje no interviene.
        """
        margen = self.umbral_s // 60

        linea = self._normalizar(codigo_linea)
        if codigo_linea and linea is None:
            disponibles = ", ".join(sorted(self.lineas))
            return (f"No tengo histórico de la {str(codigo_linea).upper()}. Tengo "
                    f"datos de: {disponibles}.")

        if linea:
            v = self.lineas[linea]
            return (f"La {linea} llega puntual en el {v['pct_puntual']:.0f}% de las "
                    f"observaciones, tomando puntual como {margen} minutos o menos. "
                    f"Su retraso mediano es de {_minutos(v['retraso_mediana_s'])} y "
                    f"el 10% de los trenes acumula más de "
                    f"{_minutos(v['retraso_p90_s'])}." + self._salvedad())

        orden = sorted(self.lineas.items(), key=lambda kv: -kv[1]["pct_puntual"])
        (mejor, vm), (peor, vp) = orden[0], orden[-1]
        return (f"La línea más puntual es la {mejor}: llega dentro de {margen} "
                f"minutos en el {vm['pct_puntual']:.0f}% de las observaciones. La "
                f"menos puntual es la {peor}, con un {vp['pct_puntual']:.0f}%. "
                f"Comparadas {len(self.lineas)} líneas." + self._salvedad())

    def diagnostico(self) -> dict[str, Any]:
        return {"lineas": len(self.lineas), "ventana": self.ventana,
                "generado_utc": self.generado}


def cargar(ruta: str | Path) -> HistoricoPuntualidad | None:
    """Carga el resumen, o devuelve None si no existe o está mal.

    NUNCA lanza: que falte el histórico no puede impedir que arranque el servicio.
    Sin él, la intención PUNTUALIDAD_HISTORICA responde que no puede contestar, que
    es el comportamiento de antes de esta fase y sigue siendo correcto.
    """
    try:
        return HistoricoPuntualidad(ruta)
    except Exception as exc:  # noqa: BLE001
        log.warning("Sin histórico de puntualidad (%s): %s", ruta, exc)
        return None


if __name__ == "__main__":
    import sys

    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    h = cargar(sys.argv[1] if len(sys.argv) > 1
               else "/home/tfm/renfe-delay-app/datos/puntualidad.json")
    if h is None:
        raise SystemExit("No se ha podido cargar el resumen.")
    print("\nSin línea:\n ", h.responder())
    for c in ("C4", "c10", "C4b", "C99"):
        print(f"\n{c}:\n ", h.responder(c))
