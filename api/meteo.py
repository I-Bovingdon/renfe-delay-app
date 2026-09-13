"""
meteo.py — Observación AEMET más reciente, por parada, para el modelo.

Lee la última captura de `raw/observacion_nacional` (fichero nacional, ~9.700 registros)
y deja en memoria, para cada parada del catálogo, la observación de la estación AEMET
más cercana. La petición del usuario solo consulta un diccionario.

REPLICAR EL ENTRENAMIENTO, NO MEJORARLO. Las tres decisiones que siguen no son las que
tomaría un sistema meteorológico bien hecho: son las que tomó `cercanias_pipeline.py`.
Apartarse de ellas sería train/serve skew silencioso, que es peor que un dato pobre.

  1. CANDIDATAS: solo las 15 estaciones del corredor de Madrid, de las que 12 emiten de
     forma regular. El fichero nacional trae 784 estaciones con `idema` que empieza por
     3, pero la tabla de entrenamiento (`observacion_horaria_*.parquet`) contiene
     exactamente 12 estaciones distintas, verificado el 13/09. Elegir la más cercana
     entre 784 asignaría a muchas paradas una estación que el modelo nunca vio asociada
     a ellas: misma columna, valor distinto.

  2. ESTACIÓN POR PARADA: la más cercana, asignada UNA SOLA VEZ y no por consulta. El
     pipeline cruza paradas x estaciones, se queda con el `idxmin` de la distancia y pega
     cada parada a esa estación para todas sus filas del día; si esa estación no tiene
     observación anterior a t0, la fila queda nula y NO se busca la segunda más cercana.
     Recalcular la asignación en cada refresco entre las estaciones que estén emitiendo
     sería más listo y menos fiel, y además haría bailar la estación de una consulta a
     otra.

  3. VIENTO AUSENTE = 0, NO NULO. `limpiar_meteo` hace `fillna(0)` sobre
     `viento_vel_ms`, que tiene un 16 % de nulos. El modelo vio ceros donde faltaba el
     viento; enviarle un nulo sería un valor que nunca vio.

CORRESPONDENCIA RAW -> ENTRENAMIENTO, verificada el 13/09 comparando valor a valor las
414 filas del parquet del 11/09 contra el raw del mismo día (100 % de coincidencia):

    raw 'ta'    -> temp_aire_c      -> temp_aire_c_t0     (contrato: temp_c)
    raw 'prec'  -> precip_mm        -> precip_mm_t0       (contrato: precip_mm_1h)
    raw 'vv'    -> viento_vel_ms    -> viento_vel_ms_t0   (contrato: wind_speed_ms)
    raw 'vmax'  -> viento_racha_ms  -> NO lo usa el modelo

SIN DESPLAZAMIENTO TEMPORAL ENTRE ENTRENAMIENTO Y SERVICIO. El pipeline cruza la meteo
con `pd.merge_asof(..., left_on="t0", right_on="captura_ts", direction="backward")`, y
`captura_ts` es el instante en que el colector vio la observación por primera vez
(`drop_duplicates(..., keep="first")` sobre captura_ts ordenada). O sea: el modelo
entrenó con "la observación más reciente disponible en t0", que es exactamente lo único
que puede ofrecer este servicio. La semántica temporal coincide por construcción.

LA PRECIPITACIÓN SÍ VIAJA, y conviene entender por qué. Medido sobre siete días de serie
horaria, con un desfase de 2 h las 15 horas con precipitación del periodo se leen como
cero: el valor detecta la lluvia que ya cayó, no la que está cayendo. Es una limitación
real de la variable, pero afecta IGUAL al entrenamiento, que usó el mismo dato
retrasado. El modelo aprendió un indicador de lluvia reciente, y eso es lo que hay que
darle. Enviar un nulo por "honestidad" sería introducir la diferencia que este módulo
existe para evitar.

LATENCIA. Medida el 13/09 a las 09:26 UTC: 1,4 h en la mayoría de estaciones, hasta
4,4 h en la más rezagada. No es un fallo, es cómo publica AEMET, y el entrenamiento la
absorbió igual. Por eso la caducidad se evalúa sobre el FICHERO (¿está escribiendo el
colector?) y no sobre la antigüedad de cada observación.

EL NULO ES LEGÍTIMO AQUÍ. El pipeline deja NaN en las tres columnas para los t0
anteriores a la primera captura del día, así que el modelo sí vio nulos en meteo. El
camino degradado de este módulo está dentro de la distribución de entrenamiento. No es
el caso de las columnas de alerta, donde el nulo no existe y hay que enviar cero.

TFM Cercanías RENFE · UCM · 2026
"""

from __future__ import annotations

import gzip
import json
import logging
import math
import os
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from tiempo import ahora_utc, iso_utc

log = logging.getLogger(__name__)

DIR_OBSERVACION = Path(
    os.getenv("RUTA_RAW_AEMET", "/home/tfm/data-aemet/raw/observacion_nacional")
)

# Las 15 estaciones del corredor validadas en 03_DATOS_Y_ESQUEMAS.md. Tres de ellas
# (3196 Cuatro Vientos, 3175 Torrejón, 3200 Aranjuez) no emitían el 13/09 y por eso la
# tabla de entrenamiento tiene 12. Se dejan en la lista: si vuelven a emitir, el
# entrenamiento también las recogería.
ESTACIONES_CORREDOR: frozenset[str] = frozenset(
    {
        "3195",   # Madrid, Retiro
        "3129",   # Madrid, Barajas
        "3196",   # Madrid, Cuatro Vientos
        "3194U",  # Madrid, Ciudad Universitaria
        "3126Y",  # El Goloso
        "3125Y",  # San Sebastián de los Reyes
        "3191E",  # Colmenar Viejo
        "3338",   # Robledo de Chavela
        "3330Y",  # Las Rozas de Puerto Real
        "3170Y",  # Alcalá de Henares
        "3175",   # Torrejón de Ardoz
        "3168D",  # Guadalajara
        "3110C",  # Getafe
        "3200",   # Aranjuez
        "3182Y",  # Arganda del Rey
    }
)

# Cada cuánto se mira si hay captura nueva. El colector escribe una vez por hora (a y 26
# el 13/09), así que 10 minutos acotan la antigüedad percibida sin releer nada de más:
# si el nombre del último fichero no ha cambiado, `refrescar` sale sin abrirlo.
INTERVALO_METEO_S = 600

# Por encima de esto el bloque se declara degradado. Tres ciclos horarios perdidos ya no
# son latencia de AEMET, son el colector parado.
MARGEN_FEED_CADUCO_S = 3 * 3600

# Ver la cabecera. A False, `precip_mm_1h` deja de viajar: se conserva el interruptor
# porque la variable tiene una limitación documentada y conviene poder aislarla.
SERVIR_PRECIPITACION = True

# observacion_nacional_20260913T092614Z.json.gz
_RE_TS = re.compile(r"_(\d{8}T\d{6})Z\.json\.gz$")


def _ts_de_nombre(nombre: str) -> datetime | None:
    """Instante de captura a partir del nombre del fichero, o None si no encaja."""
    m = _RE_TS.search(nombre)
    if not m:
        return None
    return datetime.strptime(m.group(1), "%Y%m%dT%H%M%S").replace(tzinfo=timezone.utc)


def _fint_a_utc(texto: str) -> datetime | None:
    """'2026-09-13T08:00:00+0000' -> datetime UTC.

    AEMET publica `fint` con el desplazamiento explícito (verificado el 13/09), así que
    no hay que suponer huso. Si algún día dejara de traerlo, se asume UTC y se sigue:
    perder la meteorología por un formato es peor que un error de una hora.
    """
    if not texto:
        return None
    try:
        dt = datetime.fromisoformat(texto.replace("Z", "+00:00"))
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def _distancia_km(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """Distancia ortodrómica entre dos puntos. La misma fórmula que `_haversine_km`
    del pipeline de entrenamiento."""
    r = 6371.0
    f1, f2 = math.radians(lat1), math.radians(lat2)
    df = math.radians(lat2 - lat1)
    dl = math.radians(lon2 - lon1)
    a = math.sin(df / 2) ** 2 + math.cos(f1) * math.cos(f2) * math.sin(dl / 2) ** 2
    return 2 * r * math.asin(math.sqrt(a))


def _num(valor: Any) -> float | None:
    """Convierte a float tolerando nulos y cadenas. Cualquier resto queda en None."""
    if valor is None or valor == "":
        return None
    try:
        return float(valor)
    except (TypeError, ValueError):
        return None


class FuenteMeteo:
    """Última observación AEMET aplicable a cada parada del catálogo.

    Caché propia y tarea de fondo propia, igual que `AlmacenAlertas` y
    `FuentePosiciones`. Un fallo leyendo `data-aemet` no puede dejar sin refrescar el
    estado de red, que sí está en la ruta crítica de la predicción.
    """

    def __init__(self, catalogo: Any, directorio: Path | str = DIR_OBSERVACION):
        self.directorio = Path(directorio)

        # Coordenadas de las 95 paradas, copiadas una vez. Evita tocar el catálogo en
        # cada refresco y deja el módulo probable sin él.
        self._paradas: dict[str, tuple[float, float]] = {
            sid: (e["lat"], e["lon"]) for sid, e in catalogo.estaciones.items()
        }

        self._por_parada: dict[str, dict[str, Any]] = {}
        self._observaciones: dict[str, dict[str, Any]] = {}
        # Asignación parada -> estación. Se calcula UNA vez (ver decisión 2) y solo se
        # rehace si aparece una estación que no estaba en el censo.
        self._estacion_de_parada: dict[str, tuple[str, float]] = {}
        # Censo ACUMULADO de estaciones vistas: idema -> (lat, lon). Solo crece. Una
        # estación que deja de emitir un rato no puede reasignar paradas.
        self._censo: dict[str, tuple[float, float]] = {}
        self._fichero_leido: str | None = None
        self._captura_utc: datetime | None = None
        self._registros_ultima: int = 0
        self.ultimo_error: str | None = None

    # ------------------------------------------------------------- ficheros ---
    def _ultimo_fichero(self) -> tuple[Path, datetime] | None:
        """Captura más reciente, por el sello de tiempo del nombre.

        Se miran las dos carpetas de fecha más recientes: la carpeta cambia a medianoche
        UTC y a esa hora el último fichero puede seguir en la anterior.
        """
        if not self.directorio.is_dir():
            raise FileNotFoundError(f"No existe {self.directorio}")
        carpetas = sorted(
            (d for d in self.directorio.iterdir() if d.is_dir()), key=lambda d: d.name
        )[-2:]

        mejor: tuple[Path, datetime] | None = None
        for carpeta in carpetas:
            with os.scandir(carpeta) as entradas:
                for e in entradas:
                    ts = _ts_de_nombre(e.name)
                    if ts is not None and (mejor is None or ts > mejor[1]):
                        mejor = (Path(e.path), ts)
        return mejor

    # -------------------------------------------------------------- lectura ---
    def _parsear(self, ruta: Path) -> dict[str, dict[str, Any]]:
        """Observación más reciente de cada estación del corredor.

        Coste medido el 13/09: 488 KB comprimidos, 9.724 registros, 49 ms de parseo y
        13 MB de pico transitorio. Se paga una vez por hora en un hilo de fondo.
        """
        with gzip.open(ruta, "rt", encoding="utf-8") as fh:
            doc = json.load(fh)

        # El colector guarda la lista tal cual o envuelta; se admiten ambas formas.
        registros = doc if isinstance(doc, list) else (doc.get("payload") or doc.get("datos") or [])
        self._registros_ultima = len(registros)

        mejores: dict[str, dict[str, Any]] = {}
        for r in registros:
            idema = str(r.get("idema") or "")
            if idema not in ESTACIONES_CORREDOR:
                continue
            fint = _fint_a_utc(str(r.get("fint") or ""))
            if fint is None:
                continue
            lat, lon = _num(r.get("lat")), _num(r.get("lon"))
            if lat is None or lon is None:
                continue

            # El fichero trae las últimas ~24 h de cada estación: nos quedamos con la
            # observación de `fint` más alto, que es la más reciente disponible.
            anterior = mejores.get(idema)
            if anterior is not None and anterior["fint"] >= fint:
                continue

            mejores[idema] = {
                "idema": idema,
                "fint": fint,
                "lat": lat,
                "lon": lon,
                "ta": _num(r.get("ta")),
                "prec": _num(r.get("prec")),
                # `vv` ausente se convierte en 0.0 aquí, no más abajo: es donde el
                # pipeline de entrenamiento hace el fillna, antes de cualquier cruce.
                "vv": _num(r.get("vv")) if _num(r.get("vv")) is not None else 0.0,
            }
        return mejores

    def _calcular_asignacion(self, observaciones: dict[str, dict[str, Any]]) -> None:
        """Pega cada parada a su estación AEMET más cercana. Se calcula UNA vez.

        95 paradas x 12 estaciones = 1.140 distancias, microsegundos. El censo de
        estaciones SOLO CRECE: si una estación deja de emitir durante unas horas, la
        asignación no se toca y las paradas que dependen de ella se quedan sin dato,
        igual que en el entrenamiento. Reasignarlas a la siguiente más cercana cambiaría
        en silencio el significado de la columna, y esa es la clase de cosa que luego no
        se sabe explicar delante de un tribunal.
        """
        nuevas = {
            idema: (o["lat"], o["lon"])
            for idema, o in observaciones.items()
            if idema not in self._censo
        }
        if not nuevas and self._estacion_de_parada:
            return  # nada que recalcular: el censo no ha crecido

        if self._censo and nuevas:
            log.info(
                "Estaciones AEMET nuevas en el censo (%s): se rehace la asignación",
                sorted(nuevas),
            )
        self._censo.update(nuevas)

        asignacion: dict[str, tuple[str, float]] = {}
        for stop_id, (lat, lon) in self._paradas.items():
            mejor, mejor_km = None, None
            for idema, (elat, elon) in self._censo.items():
                km = _distancia_km(lat, lon, elat, elon)
                if mejor_km is None or km < mejor_km:
                    mejor, mejor_km = idema, km
            if mejor is not None:
                asignacion[stop_id] = (mejor, round(mejor_km, 1))

        self._estacion_de_parada = asignacion

    def _bloques_por_parada(self, observaciones: dict[str, dict[str, Any]]) -> dict[str, dict[str, Any]]:
        """Valores servibles de cada parada, según la asignación ya fijada.

        Si la estación asignada a una parada falta en esta captura, la parada se queda
        sin dato. No se busca la segunda más cercana: el entrenamiento tampoco lo hacía,
        dejaba la fila nula.
        """
        salida: dict[str, dict[str, Any]] = {}
        for stop_id, (idema, km) in self._estacion_de_parada.items():
            obs = observaciones.get(idema)
            if obs is None:
                continue
            salida[stop_id] = {
                "temp_c": obs["ta"],
                "precip_mm_1h": obs["prec"] if SERVIR_PRECIPITACION else None,
                "wind_speed_ms": obs["vv"],
                # Trazabilidad: no son features, pero permiten explicar en la defensa
                # de dónde sale el número que ha visto el modelo.
                "estacion": idema,
                "distancia_km": km,
                "observado_utc": iso_utc(obs["fint"]),
                "edad_obs_s": None,  # se calcula al leer, no al refrescar
            }
        return salida

    def refrescar(self) -> bool:
        """Relee la última captura si hay una nueva. Nunca lanza excepción.

        Ante un fallo se CONSERVA la última asignación válida: lo que decide si el dato
        sirve es su antigüedad, no que haya habido un error. Mismo criterio que
        `CacheContexto` y `FuentePosiciones`.
        """
        try:
            ultimo = self._ultimo_fichero()
            if ultimo is None:
                raise RuntimeError(f"Sin capturas en {self.directorio}")
            ruta, ts = ultimo
            if ruta.name == self._fichero_leido:
                return True  # el colector aún no ha escrito una captura nueva

            observaciones = self._parsear(ruta)
            if not observaciones:
                raise RuntimeError(
                    "Ninguna estación del corredor en la captura "
                    f"({self._registros_ultima} registros nacionales)"
                )

            self._observaciones = observaciones
            self._calcular_asignacion(observaciones)
            self._por_parada = self._bloques_por_parada(observaciones)
            self._captura_utc = ts
            self._fichero_leido = ruta.name
            self.ultimo_error = None
            log.info(
                "Meteo refrescada: %s · %d estaciones del corredor · %d paradas cubiertas",
                ruta.name, len(observaciones), len(self._por_parada),
            )
            return True
        except Exception as exc:  # noqa: BLE001 — el refresco nunca tumba la API
            self.ultimo_error = str(exc)
            log.warning("Fallo al refrescar la meteorología: %s", exc)
            return False

    # --------------------------------------------------------------- lectura ---
    def _caduco(self, ahora: datetime | None = None) -> bool:
        """Cierto si el colector lleva demasiado tiempo sin escribir una captura."""
        if self._captura_utc is None:
            return True
        edad = ((ahora or ahora_utc()) - self._captura_utc).total_seconds()
        return edad > MARGEN_FEED_CADUCO_S

    def observacion(self, stop_id: str) -> dict[str, Any] | None:
        """Bloque meteorológico aplicable a una parada, o None si no hay dato vigente.

        Devolver None hace que `features.py` marque el bloque como degradado y deje las
        tres columnas nulas. No se imputa nada aquí: una imputación silenciosa en el
        servicio es una diferencia invisible con el entrenamiento.
        """
        if self._caduco():
            return None
        datos = self._por_parada.get(stop_id)
        if datos is None:
            return None
        salida = dict(datos)
        obs = self._observaciones.get(datos["estacion"])
        if obs is not None:
            salida["edad_obs_s"] = round((ahora_utc() - obs["fint"]).total_seconds())
        return salida

    def salud(self) -> dict[str, Any]:
        """Diagnóstico para /api/salud y para explicar la latencia en la defensa."""
        ahora = ahora_utc()
        antiguedad = (
            round((ahora - self._captura_utc).total_seconds())
            if self._captura_utc else None
        )
        edades = [
            round((ahora - o["fint"]).total_seconds() / 3600, 1)
            for o in self._observaciones.values()
        ]
        return {
            "hay_datos": bool(self._por_parada),
            "vigente": not self._caduco(ahora),
            "ultima_captura": iso_utc(self._captura_utc) if self._captura_utc else None,
            "antiguedad_captura_s": antiguedad,
            "registros_nacionales": self._registros_ultima,
            "estaciones_corredor": len(self._observaciones),
            "paradas_cubiertas": len(self._por_parada),
            # La latencia real de AEMET es por estación, no del feed: interesa el peor
            # caso tanto como la mediana.
            "edad_observacion_h_min": min(edades) if edades else None,
            "edad_observacion_h_max": max(edades) if edades else None,
            "sirve_precipitacion": SERVIR_PRECIPITACION,
            "ultimo_error": self.ultimo_error,
        }


if __name__ == "__main__":
    # Prueba en seco. Se ejecuta en el VPS desde la carpeta api/, SIN reiniciar el
    # servicio: solo lee ficheros.
    import sys

    from catalogo import Catalogo

    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    cat = Catalogo(sys.argv[1] if len(sys.argv) > 1 else "../datos/catalogo.json")

    fuente = FuenteMeteo(cat)
    fuente.refrescar()

    print("\nSalud:")
    for k, v in fuente.salud().items():
        print(f"  {k}: {v}")

    print("\nEstaciones del corredor con observación:")
    for idema, o in sorted(fuente._observaciones.items()):
        edad = (ahora_utc() - o["fint"]).total_seconds() / 3600
        print(f"  {idema:<7} {iso_utc(o['fint'])}  {edad:4.1f} h  "
              f"ta={o['ta']}  prec={o['prec']}  vv={o['vv']}")

    print("\nAlgunas paradas:")
    for nombre in ("atocha", "chamartin", "alcala de henares", "cercedilla", "aranjuez"):
        r = cat.buscar_estaciones(nombre, limite=1)
        if not r:
            continue
        sid = r[0]["stop_id"]
        obs = fuente.observacion(sid)
        if obs is None:
            print(f"  {r[0]['nombre']:<24} sin dato vigente")
            continue
        print(f"  {r[0]['nombre']:<24} estacion {obs['estacion']:<7} "
              f"{obs['distancia_km']:5.1f} km  {obs['temp_c']} C  "
              f"viento {obs['wind_speed_ms']} m/s  obs. hace {obs['edad_obs_s']/3600:.1f} h")
