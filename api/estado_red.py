"""
estado_red.py — Caché del contexto en tiempo real.

Guarda en memoria los tres bloques de contexto que el modelo necesita y que cambian
con el tiempo: estado de la red, meteorología y alertas activas. Un proceso en segundo
plano los refresca cada 60 s; la petición del usuario solo LEE de esta caché.

Por qué así y no leyendo los datos en cada consulta: recorrer los Parquet del día y
agregar con pandas cuesta segundos y varios cientos de megas de pico. El servicio va
limitado a 512 MB para no poner en riesgo la captura 24/7, y el objetivo de latencia es
menos de 1 s. Con caché, el camino de la petición es una consulta a un diccionario.

Los tres bloques comparten el mismo ciclo de vida (refresco periódico, posible fallo de
la fuente, degradación explícita), por eso viven en el mismo módulo.

ESTADO EN F2: los valores son SIMULADOS. La lectura real de los Parquet del VPS y de
AEMET llega en F7. La forma de los datos y el comportamiento ante fallos ya son los
definitivos, así que F7 solo sustituye la clase fuente.

TFM Cercanías RENFE · UCM · 2026
"""

from __future__ import annotations

import logging
import math
import random
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from tiempo import MADRID, ahora_utc

log = logging.getLogger(__name__)

# Ventana de agregación del estado de red. Es un valor de CONTRATO: el modelo tiene que
# haberse entrenado con esta misma ventana o las features no significan lo mismo.
VENTANA_ESTADO_MIN = 30

# Cada cuánto se refresca la caché.
INTERVALO_REFRESCO_S = 60

# A partir de qué antigüedad un dato deja de considerarse válido. Se prefiere declarar
# el bloque degradado y que el modelo reciba nulos antes que alimentarlo con datos
# rancios: el modelo sabe tratar un nulo, no sabe que un número es viejo.
MAX_ANTIGUEDAD_S = 600


@dataclass
class Instantanea:
    """Foto del contexto en un instante, con su marca de tiempo."""
    calculado_utc: datetime
    lineas: dict[str, dict[str, Any]] = field(default_factory=dict)
    red: dict[str, Any] = field(default_factory=dict)
    meteo: dict[str, dict[str, Any]] = field(default_factory=dict)
    alertas: dict[str, dict[str, Any]] = field(default_factory=dict)

    def antiguedad_s(self, ahora: datetime | None = None) -> float:
        return ((ahora or ahora_utc()) - self.calculado_utc).total_seconds()


# ======================================================================== fuentes ===
class FuenteContexto(ABC):
    """Interfaz de una fuente de contexto. F7 añadirá la implementación real."""

    @abstractmethod
    def calcular(self, lineas: list[str]) -> Instantanea:
        """Devuelve una instantánea completa o lanza excepción si la fuente falla."""


class FuenteSimulada(FuenteContexto):
    """Valores sintéticos plausibles para construir y probar la interfaz sin datos.

    No pretende ser un modelo de la red: solo tiene que producir números en el rango
    correcto y que varíen con la hora, para que la interfaz se vea y se pruebe como se
    verá con los datos reales.
    """

    def __init__(self, semilla: int = 42):
        self._rng = random.Random(semilla)

    def calcular(self, lineas: list[str]) -> Instantanea:
        ahora = ahora_utc()
        hora_local = ahora.astimezone(MADRID).hour + ahora.astimezone(MADRID).minute / 60

        # Dos jorobas de hora punta, mañana y tarde.
        punta = math.exp(-((hora_local - 8.0) ** 2) / 3.0) + math.exp(
            -((hora_local - 18.5) ** 2) / 4.0
        )
        nocturno = 1.0 if 5.5 <= hora_local <= 23.5 else 0.15

        estado_lineas: dict[str, dict[str, Any]] = {}
        for line_id in lineas:
            base = 60 + 240 * punta
            ruido = self._rng.uniform(-40, 60)
            media = max(0.0, (base + ruido) * nocturno)
            estado_lineas[line_id] = {
                "line_delay_mean_30m_s": round(media, 1),
                "line_delay_p90_30m_s": round(media * 2.4 + 90, 1),
                "line_active_trains_30m": max(1, int((6 + 18 * punta) * nocturno)),
            }

        medias = [v["line_delay_mean_30m_s"] for v in estado_lineas.values()]
        red = {
            "net_delay_mean_30m_s": round(sum(medias) / len(medias), 1) if medias else None,
            "net_active_trains_30m": sum(
                v["line_active_trains_30m"] for v in estado_lineas.values()
            ),
        }

        # Meteorología: una única observación para todo el núcleo mientras es simulada.
        # En F7 se asignará la estación AEMET más cercana a la parada de destino.
        meteo_global = {
            "temp_c": round(22 + 9 * math.sin((hora_local - 6) * math.pi / 14), 1),
            "precip_mm_1h": 0.0,
            "wind_gust_ms": round(self._rng.uniform(1.5, 7.0), 1),
        }
        meteo = {"__global__": meteo_global}

        # Alertas: mientras no esté enganchado el NLP, mayoría de líneas sin incidencias.
        alertas = {
            line_id: {
                "alerts_active_line": 1 if self._rng.random() < 0.15 else 0,
                "alert_severity_max": 0.0,
            }
            for line_id in lineas
        }

        return Instantanea(
            calculado_utc=ahora,
            lineas=estado_lineas,
            red=red,
            meteo=meteo,
            alertas=alertas,
        )


# ========================================================================== caché ===
class CacheContexto:
    """Mantiene la última instantánea válida y la sirve a la API."""

    def __init__(self, fuente: FuenteContexto, lineas: list[str]):
        self.fuente = fuente
        self.lineas = lineas
        self._instantanea: Instantanea | None = None
        self.ultimo_error: str | None = None
        self.refrescos_ok = 0
        self.refrescos_fallidos = 0

    def refrescar(self) -> bool:
        """Recalcula la instantánea. Devuelve si tuvo éxito.

        Si la fuente falla, se CONSERVA la instantánea anterior: un fallo puntual no
        debe dejar la API sin contexto. Es la antigüedad, no el error, lo que decide
        si un dato sigue siendo utilizable.
        """
        try:
            self._instantanea = self.fuente.calcular(self.lineas)
            self.ultimo_error = None
            self.refrescos_ok += 1
            return True
        except Exception as exc:  # noqa: BLE001 — el refresco nunca puede tumbar la API
            self.ultimo_error = str(exc)
            self.refrescos_fallidos += 1
            log.warning("Fallo al refrescar el contexto: %s", exc)
            return False

    # ----------------------------------------------------------------- lecturas ---
    def _vigente(self) -> Instantanea | None:
        if self._instantanea is None:
            return None
        if self._instantanea.antiguedad_s() > MAX_ANTIGUEDAD_S:
            return None
        return self._instantanea

    def estado_linea(self, line_id: str) -> dict[str, Any] | None:
        """Estado de una línea, o None si no hay dato vigente (bloque degradado)."""
        inst = self._vigente()
        if inst is None:
            return None
        datos = dict(inst.lineas.get(line_id, {}))
        datos.update(inst.red)
        return datos or None

    def meteo(self, stop_id: str | None = None) -> dict[str, Any] | None:
        """Meteorología aplicable a una parada. En F2 es la misma para todo el núcleo."""
        inst = self._vigente()
        if inst is None:
            return None
        return inst.meteo.get(stop_id or "__global__") or inst.meteo.get("__global__")

    def alertas(self, line_id: str) -> dict[str, Any] | None:
        inst = self._vigente()
        if inst is None:
            return None
        return inst.alertas.get(line_id)

    def salud(self) -> dict[str, Any]:
        """Diagnóstico para /api/salud y para el modo degradado de la interfaz."""
        inst = self._instantanea
        return {
            "hay_datos": inst is not None,
            "antiguedad_s": round(inst.antiguedad_s(), 1) if inst else None,
            "vigente": self._vigente() is not None,
            "ventana_min": VENTANA_ESTADO_MIN,
            "refrescos_ok": self.refrescos_ok,
            "refrescos_fallidos": self.refrescos_fallidos,
            "ultimo_error": self.ultimo_error,
            "fuente": type(self.fuente).__name__,
        }


if __name__ == "__main__":
    LINEAS = ["C1", "C2", "C3", "C4", "C4a", "C4b", "C5", "C7", "C8a", "C8b", "C9", "C10"]

    cache = CacheContexto(FuenteSimulada(), LINEAS)
    print("Antes del primer refresco:", cache.estado_linea("C5"))

    cache.refrescar()
    print("\nEstado de C5 :", cache.estado_linea("C5"))
    print("Meteo        :", cache.meteo())
    print("Alertas C5   :", cache.alertas("C5"))
    print("\nSalud:")
    for k, v in cache.salud().items():
        print(f"  {k}: {v}")

    # Comprobación del comportamiento ante fallo: la instantánea anterior se conserva.
    class FuenteRota(FuenteContexto):
        def calcular(self, lineas):
            raise RuntimeError("AEMET no responde")

    cache.fuente = FuenteRota()
    ok = cache.refrescar()
    print(f"\nTras un fallo de la fuente (ok={ok}), C5 sigue disponible: "
          f"{cache.estado_linea('C5') is not None}")
    print(f"  ultimo_error: {cache.salud()['ultimo_error']}")
