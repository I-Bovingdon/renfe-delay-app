"""
fuente_raw.py — Estado real de la red, leído del feed GTFS-RT ya capturado en disco.

Sustituye a FuenteSimulada. No toca la red de RENFE: lee los ficheros que el colector
24/7 deja en raw/trip_updates/, que es la única fuente que existe en el instante de la
consulta (los Parquet del día no se compactan hasta la madrugada siguiente).

TRES DECISIONES QUE GOBIERNAN ESTE MÓDULO

  1. VENTANA INCREMENTAL. Releer 30 ficheros cada 60 s serían 1,5 MB de I/O y ~1 s de
     CPU por ciclo en una máquina de 2 vCPU compartida con la captura. En su lugar se
     mantiene un buffer circular: 30 ficheros al arrancar, uno por ciclo después. En
     régimen son 50 KB y ~30 ms. La captura 24/7 no se entera.

  2. MEDIA POR TREN DISTINTO, no por observación. Cada tren de la línea aporta un
     único valor (su último retraso conocido dentro de la ventana). Promediar todas
     las observaciones daría 30 votos a un tren estacionado media hora en cabecera y
     uno solo a un tren que acaba de entrar en la red.

  3. SIN PANDAS. Solo biblioteca estándar. El servicio va limitado a 600 MB con el
     modelo ya cargado dentro.

QUÉ SE ENVÍA Y QUÉ NO (ver adaptador_modelo.py para el mapeo al modelo)

  Se envía: line_delay_mean_30m_s, net_delay_mean_30m_s, line_active_trains_30m,
            own_delay_s, own_delay_age_s.
  No se envía: n_capturas_line_30m. En entrenamiento cuenta PARADAS COMPLETADAS de la
            línea en la ventana; aquí solo se pueden contar trenes distintos, que es
            un orden de magnitud menor. Un conteo con la escala equivocada es peor que
            un nulo, que LightGBM trata de forma nativa.
  No se envía: meteorología. La fuente AEMET publica con ~13 h de latencia, así que el
            valor disponible en servicio no es la misma variable que la de
            entrenamiento. Se deja el bloque degradado a propósito.
  No se envía: alertas. FuenteSimulada las inventaba con un 15% de probabilidad, lo
            que metía ruido aleatorio en num_alertas_t0. Devolver vacío es más honesto
            hasta que se enganche el clasificador de la pantalla de alertas.

SOBRE LA MAGNITUD DEL RETRASO. El campo `tripUpdate.delay` del feed es la estimación
que publica RENFE. En entrenamiento, el retraso se reconstruyó como (hora real de
captura - hora teórica del GTFS). Son dos estimadores de la misma magnitud y no
coinciden exactamente; la discrepancia esperable es del orden de la cuantización del
muestreo (60 s), muy por debajo del MAE del modelo (147 s). Queda documentado como
limitación: replicar la reconstrucción del entrenamiento exigiría stop_times en la ruta
de servicio y pandas, que es justo lo que no cabe en el VPS.

TFM Cercanías RENFE · UCM · 2026
"""

from __future__ import annotations

import gzip
import json
import logging
import os
import re
from collections import deque
from pathlib import Path
from typing import Any, Iterable

from estado_red import VENTANA_ESTADO_MIN, FuenteContexto, Instantanea, nucleo_trip
from tiempo import ahora_utc

log = logging.getLogger(__name__)

# Carpeta que escribe el colector. Se sobreescribe por entorno para poder probar.
DIR_TRIP_UPDATES = Path(
    os.getenv("DIR_TRIP_UPDATES", "/home/tfm/data-renfe/raw/trip_updates")
)

# Cuántos ficheros se leen en el primer refresco (uno por minuto, ventana de 30 min).
FICHEROS_ARRANQUE = VENTANA_ESTADO_MIN

# Tope de ficheros por ciclo cuando ya hay buffer. Normalmente hay 1; el tope evita
# que tras una parada larga del servicio un solo ciclo intente leer cientos.
MAX_FICHEROS_POR_CICLO = 5

# Mismo umbral de sanidad que el pipeline de entrenamiento
# (DELAY_MAXIMO_PLAUSIBLE_S). Filtra además el glitch sistemático de +/-86.400 s del
# feed, documentado como recurrente en el 0,3-0,6% de las filas.
DELAY_MAX_PLAUSIBLE_S = 4 * 3600

# Sufijo de línea del trip_id: "...C1", "...C4a". Los trips sin sufijo de cercanías
# (media distancia dentro del mismo feed nacional) no casan y quedan fuera.
SUFIJO_LINEA = re.compile(r"C(\d+)([ab])?$")

# Epoch de captura incrustado en el nombre: trip_updates_<ts>_ft<epoch>.json.gz.
# Permite seleccionar la ventana SIN abrir ningún fichero.
PATRON_EPOCH = re.compile(r"_ft(\d+)\.json\.gz$")


def _mapa_lineas(lineas: Iterable[str]) -> dict[str, list[str]]:
    """Traduce el código de línea que trae el feed a los line_id del catálogo.

    El feed publica "C4" donde el catálogo puede tener "C4a" y "C4b" por separado. En
    ese caso el valor agregado se asigna a las dos: es la aproximación honesta, porque
    el feed no distingue la rama. Se documenta como tal en la memoria.
    """
    mapa: dict[str, list[str]] = {}
    for line_id in lineas:
        mapa.setdefault(line_id, []).append(line_id)
    for line_id in lineas:
        base = re.sub(r"[ab]$", "", line_id)
        if base != line_id:
            mapa.setdefault(base, []).append(line_id)
    return mapa


def _lineas_del_trip(trip_id: str, mapa: dict[str, list[str]]) -> list[str]:
    m = SUFIJO_LINEA.search(trip_id)
    if not m:
        return []
    codigo = f"C{m.group(1)}{m.group(2) or ''}"
    return mapa.get(codigo, [])


def _epoch_de_nombre(nombre: str) -> int | None:
    m = PATRON_EPOCH.search(nombre)
    return int(m.group(1)) if m else None


class FuenteRaw(FuenteContexto):
    """Calcula el estado de la red leyendo las últimas capturas de trip_updates."""

    def __init__(
        self,
        paradas_madrid: Iterable[str],
        directorio: Path | str = DIR_TRIP_UPDATES,
        ventana_min: int = VENTANA_ESTADO_MIN,
    ):
        # Filtro de núcleo TOPOLÓGICO: el feed es nacional y el sufijo de línea se
        # repite entre núcleos (el C1 de Sevilla existe). Se exige que el tren tenga
        # al menos una parada del catálogo de Madrid. Descartada la delimitación por
        # bounding box de coordenadas: ya se probó y excluía Guadalajara mal.
        self.paradas_madrid = frozenset(str(p) for p in paradas_madrid)
        self.directorio = Path(directorio)
        self.ventana_s = ventana_min * 60

        # Buffer circular: (epoch de captura, {nucleo_trip: datos del tren}).
        # Solo agregados, nunca el JSON completo. ~250 trenes por captura y 30
        # capturas son unos pocos MB.
        self._snapshots: deque[tuple[int, dict[str, dict[str, Any]]]] = deque()
        self._epochs: set[int] = set()

    # ------------------------------------------------------------- ficheros ---
    def _carpetas_recientes(self) -> list[Path]:
        """Las dos carpetas de fecha más recientes.

        Dos y no una porque la ventana de 30 min puede caer a caballo de la
        medianoche, y entonces la mitad de los ficheros están en la carpeta del día
        anterior.
        """
        if not self.directorio.is_dir():
            raise FileNotFoundError(f"No existe {self.directorio}")
        carpetas = sorted(
            (d for d in self.directorio.iterdir() if d.is_dir()),
            key=lambda d: d.name,
        )
        return carpetas[-2:]

    def _ficheros_pendientes(self) -> list[tuple[Path, int]]:
        """Ficheros aún no procesados, ordenados por epoch de captura.

        No abre ninguno: el epoch va en el nombre. En el primer refresco devuelve los
        últimos FICHEROS_ARRANQUE; después, solo los nuevos.
        """
        candidatos: list[tuple[Path, int]] = []
        for carpeta in self._carpetas_recientes():
            with os.scandir(carpeta) as entradas:
                for e in entradas:
                    epoch = _epoch_de_nombre(e.name)
                    if epoch is not None and epoch not in self._epochs:
                        candidatos.append((Path(e.path), epoch))

        candidatos.sort(key=lambda par: par[1])
        tope = FICHEROS_ARRANQUE if not self._snapshots else MAX_FICHEROS_POR_CICLO
        return candidatos[-tope:]

    def _leer_snapshot(
        self, ruta: Path, mapa_lineas: dict[str, list[str]]
    ) -> dict[str, dict[str, Any]]:
        """Extrae de una captura los trenes de Madrid con su retraso publicado."""
        with gzip.open(ruta, "rt", encoding="utf-8") as fh:
            doc = json.load(fh)

        entidades = (doc.get("payload") or {}).get("entity") or []
        trenes: dict[str, dict[str, Any]] = {}

        for entidad in entidades:
            actualizacion = entidad.get("tripUpdate") or {}
            trip_id = str((actualizacion.get("trip") or {}).get("tripId") or "")
            if not trip_id:
                continue

            retraso = actualizacion.get("delay")
            if retraso is None:
                continue
            retraso = float(retraso)
            if abs(retraso) > DELAY_MAX_PLAUSIBLE_S:
                continue

            paradas = [
                str(s.get("stopId") or "")
                for s in (actualizacion.get("stopTimeUpdate") or [])
            ]
            parada_madrid = next((p for p in paradas if p in self.paradas_madrid), None)
            if parada_madrid is None:
                continue  # tren de otro núcleo

            lineas = _lineas_del_trip(trip_id, mapa_lineas)
            if not lineas:
                continue  # sin sufijo de cercanías reconocible

            trenes[nucleo_trip(trip_id)] = {
                "delay": retraso,
                "stop_id": parada_madrid,
                "lineas": lineas,
            }

        return trenes

    # -------------------------------------------------------------- cálculo ---
    def calcular(self, lineas: list[str]) -> Instantanea:
        ahora = ahora_utc()
        mapa = _mapa_lineas(lineas)

        # 1) Incorporar las capturas nuevas. Un fichero a medio escribir o corrupto se
        # salta y se vuelve a intentar en el siguiente ciclo: no se marca como visto.
        leidos = 0
        for ruta, epoch in self._ficheros_pendientes():
            try:
                trenes = self._leer_snapshot(ruta, mapa)
            except (OSError, EOFError, ValueError) as exc:
                log.warning("Captura ilegible %s: %s", ruta.name, exc)
                continue
            self._snapshots.append((epoch, trenes))
            self._epochs.add(epoch)
            leidos += 1

        if not self._snapshots:
            raise RuntimeError(
                f"Ninguna captura legible en {self.directorio}. "
                "El colector puede estar parado o los permisos ser incorrectos."
            )

        # 2) Podar lo que se sale de la ventana.
        self._snapshots = deque(sorted(self._snapshots, key=lambda par: par[0]))
        epoch_mas_reciente = self._snapshots[-1][0]
        limite = epoch_mas_reciente - self.ventana_s
        while self._snapshots and self._snapshots[0][0] < limite:
            epoch, _ = self._snapshots.popleft()
            self._epochs.discard(epoch)

        # 3) Último retraso conocido de cada tren dentro de la ventana. Se recorre de
        # la captura más antigua a la más reciente, así que la última escritura gana.
        ultimo_por_tren: dict[str, dict[str, Any]] = {}
        for epoch, trenes in self._snapshots:
            for nucleo, datos in trenes.items():
                ultimo_por_tren[nucleo] = {**datos, "epoch": epoch}

        # 4) Agregados por línea. Un voto por tren distinto, no por observación.
        suma_linea: dict[str, float] = {}
        cuenta_linea: dict[str, int] = {}
        suma_red = 0.0
        for datos in ultimo_por_tren.values():
            suma_red += datos["delay"]
            for line_id in datos["lineas"]:
                suma_linea[line_id] = suma_linea.get(line_id, 0.0) + datos["delay"]
                cuenta_linea[line_id] = cuenta_linea.get(line_id, 0) + 1

        n_trenes_red = len(ultimo_por_tren)
        estado_lineas: dict[str, dict[str, Any]] = {}
        for line_id in lineas:
            n = cuenta_linea.get(line_id, 0)
            estado_lineas[line_id] = {
                # Nulo explícito cuando no hay ni un tren de la línea en la ventana
                # (madrugada, o línea sin servicio). Es ausencia real de dato, no cero.
                "line_delay_mean_30m_s": round(suma_linea[line_id] / n, 1) if n else None,
                "line_active_trains_30m": n or None,
                # El p90 requeriría guardar la distribución completa; no es feature del
                # modelo y la interfaz no lo usa. Se declara nulo en lugar de inventarlo.
                "line_delay_p90_30m_s": None,
            }

        red = {
            "net_delay_mean_30m_s": (
                round(suma_red / n_trenes_red, 1) if n_trenes_red else None
            ),
            "net_active_trains_30m": n_trenes_red or None,
        }

        # 5) Estado propio de cada tren, indexado por núcleo del trip_id. El trip_id
        # del catálogo GTFS y el del feed llevan prefijos de publicación distintos
        # ("1037J79324C7" vs "3053S23573C1"); casar por núcleo dio un 99,9% de match
        # frente al 0% de la igualdad exacta.
        epoch_ahora = int(ahora.timestamp())
        propios = {
            nucleo: {
                "own_delay_s": datos["delay"],
                "own_delay_age_s": float(max(0, epoch_ahora - datos["epoch"])),
                # stop_sequence NO viene en el feed (100% nulo, verificado sobre los
                # Parquet reales). Derivarlo exigiría el orden de paradas del trip en
                # la ruta de servicio. No es feature del modelo: se deja nulo.
                "own_last_stop_sequence": None,
            }
            for nucleo, datos in ultimo_por_tren.items()
        }

        log.info(
            "Estado de red: %d capturas en ventana (+%d nuevas) · %d trenes de Madrid "
            "· retraso medio del núcleo %s s",
            len(self._snapshots), leidos, n_trenes_red, red["net_delay_mean_30m_s"],
        )

        return Instantanea(
            calculado_utc=ahora,
            lineas=estado_lineas,
            red=red,
            # Meteo y alertas vacías a propósito: ver cabecera del módulo. features.py
            # las declarará como bloques degradados, que es la verdad.
            meteo={},
            alertas={},
            propios=propios,
        )


if __name__ == "__main__":
    # Prueba en seco. Se ejecuta en el VPS desde la carpeta api/, SIN reiniciar el
    # servicio y sin tocar nada: solo lee.
    import time

    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")

    from catalogo import Catalogo
    from estado_red import CacheContexto

    cat = Catalogo(os.getenv("RUTA_CATALOGO", "../datos/catalogo.json"))
    lineas = [l["line_id"] for l in cat.lineas]
    print(f"Catálogo: {len(cat.estaciones)} estaciones · {len(lineas)} líneas · "
          f"{len(cat.trips)} trips")

    fuente = FuenteRaw(paradas_madrid=cat.estaciones)
    cache = CacheContexto(fuente, lineas)

    t = time.perf_counter()
    ok = cache.refrescar()
    print(f"\nPrimer refresco (30 capturas): {'OK' if ok else 'FALLO'} en "
          f"{(time.perf_counter() - t) * 1000:.0f} ms")
    if not ok:
        print("  error:", cache.salud()["ultimo_error"])
        raise SystemExit(1)

    t = time.perf_counter()
    cache.refrescar()
    print(f"Segundo refresco (incremental): {(time.perf_counter() - t) * 1000:.0f} ms")

    print("\nEstado por línea:")
    for line_id in lineas:
        e = cache.estado_linea(line_id) or {}
        print(f"  {line_id:>4}: media {str(e.get('line_delay_mean_30m_s')):>8} s · "
              f"{str(e.get('line_active_trains_30m')):>4} trenes")

    print(f"\nNúcleo completo: {(cache.estado_linea(lineas[0]) or {}).get('net_delay_mean_30m_s')} s")

    # Comprobación del cruce por núcleo contra el catálogo: es lo que decide si
    # own_delay_s llega a poblarse en una consulta real.
    inst = cache._instantanea
    nucleos_feed = set(inst.propios)
    nucleos_catalogo = {nucleo_trip(t) for t in cat.trips}
    casan = nucleos_feed & nucleos_catalogo
    print(f"\nTrenes en el feed: {len(nucleos_feed)}")
    print(f"Casan con el catálogo: {len(casan)} "
          f"({len(casan) / max(len(nucleos_feed), 1):.1%})")
    if len(casan) / max(len(nucleos_feed), 1) < 0.90:
        print("  AVISO: por debajo del 90%. El catálogo GTFS probablemente está "
              "desfasado; regenéralo antes de desplegar.")

    ejemplo = next(iter(casan), None)
    if ejemplo:
        print(f"\nEjemplo de estado propio ({ejemplo}): {inst.propios[ejemplo]}")

    print("\nSalud:")
    for k, v in cache.salud().items():
        print(f"  {k}: {v}")
