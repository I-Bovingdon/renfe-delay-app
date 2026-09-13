"""
alertas.py — Clasificación e indexado en vivo de las alertas de RENFE.

Responsabilidad: mantener en memoria el estado de las incidencias del día de
servicio (hora de Madrid) leyendo el `raw` del colector, y servirlo ya
clasificado al endpoint de la API.

Decisiones de diseño (ver memoria, sección de productivización):

  - Solo biblioteca estándar. No se importa pandas: el volumen real es de ~115
    alertas únicas al día y ~71 entidades por captura, y el servicio está
    limitado a 600 MB compartidos con los colectores.
  - Se indexa el DÍA COMPLETO, no solo la última captura. Reconstruir el día
    entero cuesta ~2,7 s medidos (975 ficheros x 2,8 ms), así que la pantalla
    puede mostrar también las incidencias ya resueltas. Una pantalla que solo
    muestra "activas ahora" aparece vacía la mayor parte del tiempo.
  - Una alerta se considera ACTIVA si se ha visto en alguna de las ~3 últimas
    capturas (MARGEN_ACTIVA_S). Exigir presencia en *la* última captura hace
    que la pantalla parpadee a vacío ante un solo fallo de captura.
  - Si la última captura tiene más de MARGEN_FEED_CADUCO_S, el estado del feed
    pasa a CADUCO. La pantalla debe decir "datos no disponibles", nunca
    "sin incidencias": son cosas distintas y confundirlas es el peor fallo
    posible de esta pantalla (precedente real: 12/07 y 19/07).
  - La hora de aparición que se muestra es la NUESTRA (primera captura en que
    la vemos), no `activePeriod.start` del feed, que no está verificado. El
    valor del feed se conserva aparte para trazabilidad.
"""

from __future__ import annotations

import glob
import gzip
import json
import logging
import os
import re
import threading
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

log = logging.getLogger("alertas")

# ---------------------------------------------------------------------------
# CONFIGURACIÓN
# ---------------------------------------------------------------------------

# Carpeta del colector. Un subdirectorio por FECHA UTC, un fichero por captura.
RUTA_RAW_ALERTS = os.environ.get(
    "RUTA_RAW_ALERTS", "/home/tfm/data-renfe/raw/alerts"
)

MADRID = ZoneInfo("Europe/Madrid")

# Una alerta sigue "activa" si se ha visto en los últimos 5 minutos (~3 capturas).
MARGEN_ACTIVA_S = 5 * 60

# Por encima de esto, el feed se declara caduco y la pantalla lo dice.
MARGEN_FEED_CADUCO_S = 10 * 60

# Nº de capturas vacías consecutivas a partir del cual se sospecha del emisor.
# Un feed de alertas nacional completamente vacío es implausible.
UMBRAL_CAPTURAS_VACIAS = 5


# ---------------------------------------------------------------------------
# CLASIFICACIÓN
# ---------------------------------------------------------------------------

# Precedencia: CAUSA antes que SÍNTOMA. El orden importa y no es el del
# notebook original: allí RETRASO iba primero e incluía el patrón "minutos",
# la palabra más frecuente del corpus, de modo que toda avería redactada como
# "avería ..., demoras de 15 minutos" se clasificaba como RETRASO y la
# categoría AVERIA quedaba sistemáticamente a cero.
PATRONES_TIPO = (
    # RESOLUCION va PRIMERO y no es opcional. RENFE publica el aviso de
    # "ya está arreglado" como una alerta nueva, y su texto contiene la
    # palabra de la incidencia original ("Subsanada la avería en..."). Sin
    # esta regla, un aviso de normalización se clasifica como AVERIA con
    # impacto alto y aparece entre las incidencias activas diciendo justo
    # lo contrario de lo que dice su texto. Ocurrió el 31/08 en C10/C7.
    ("RESOLUCION", r"subsanad|restablecid|normalizad|recuperan sus frecuencias|"
                   r"queda resuelt|se ha resuelto|finalizad[ao] la incidencia"),
    ("SUPRESION", r"suprimid|no presta servicio|sin circulaci[óo]n|no circula"),
    ("AVERIA", r"aver[íi]a|incidencia t[ée]cnica"),
    ("SERVICIO_BUS", r"autob[úu]s|autobuses|plan alternativo"),
    ("OBRAS", r"obras|reajusta|mejora[^.]{0,30}infraestructura"),
    ("RETRASO", r"demora|retraso|retard"),
)
PATRONES_TIPO = tuple(
    (tipo, re.compile(patron, re.IGNORECASE)) for tipo, patron in PATRONES_TIPO
)

# Estas alertas son ruido PARA EL MODELO, pero son la información más útil de
# la pantalla para un viajero con movilidad reducida. No se descartan: se
# separan en su propia sección.
RE_ACCESIBILIDAD = re.compile(
    r"ascensor|escalera|aseos|accesible|inaccesible|movilidad reducida|pmr|vest[íi]bulo",
    re.IGNORECASE,
)

# Marca ortogonal al tipo: la incidencia procede de trabajos planificados,
# no de un fallo inesperado. NO significa que vaya a ocurrir más tarde.
#
# Se excluye a propósito `previst`: casaba con "tiene prevista su salida a las
# 15:12h", una frase rutinaria presente en casi cualquier aviso, y marcaba como
# planificadas supresiones que no lo eran. Se excluye también `reajusta` por lo
# mismo: un reajuste de servicio puede tener cualquier causa.
RE_PLANIFICADA = re.compile(r"obras|trabajos programad|programad[ao]s", re.IGNORECASE)

# Impacto estimado. NO es un dato de RENFE: el feed no publica severidad
# (`cause` y `effect` vienen vacíos al ~100 %). Es un criterio propio,
# derivado del tipo de incidencia, que se usa solo para ORDENAR la lista y
# como acento de icono. Debe documentarse como tal en la memoria.
IMPACTO_POR_TIPO = {
    "RESOLUCION": "BAJO",   # informa de que algo se ha arreglado: no es un problema
    "SUPRESION": "ALTO",
    "AVERIA": "ALTO",
    "RETRASO": "MEDIO",
    "SERVICIO_BUS": "MEDIO",
    "OBRAS": "BAJO",
    "OTRO": "BAJO",
}
ORDEN_IMPACTO = {"ALTO": 0, "MEDIO": 1, "BAJO": 2}

# Extracción de línea. Dos vías, en este orden:
#   1) hashtag en el texto  -> "#MadC4a" -> "C4a"
#   2) route_id de la entidad informada -> "10TC4a" -> "C4a"
# La vía 2 es el rescate de las alertas que no llevan hashtag y que el
# pipeline de modelado descartaba en silencio.
RE_LINEA_HASHTAG = re.compile(r"#MadC(\d+[ab]?)", re.IGNORECASE)
RE_LINEA_RUTA = re.compile(r"C(\d+[ab]?)\s*$", re.IGNORECASE)

# Timestamp UTC en el nombre del fichero: alerts_20260830T164230Z_ft...json.gz
RE_TS_FICHERO = re.compile(r"(\d{8}T\d{6})Z")


def clasificar_tipo(texto: str) -> str:
    """Devuelve el tipo de incidencia. Gana el primer patrón que casa."""
    for tipo, patron in PATRONES_TIPO:
        if patron.search(texto):
            return tipo
    return "OTRO"


# ---------------------------------------------------------------------------
# CLASIFICACIÓN PARA EL MODELO  ·  NO MODIFICAR SIN REENTRENAR
# ---------------------------------------------------------------------------
# Este segundo clasificador NO es el de la pantalla y no debe unificarse con
# él. Replica literalmente el `np.select` de `limpiar_alertas` en
# `cercanias_pipeline.py`, incluidos su orden de precedencia y sus patrones
# exactos, porque el modelo desplegado aprendió con esa clasificación y no con
# otra.
#
# Dos diferencias deliberadas respecto al clasificador de pantalla:
#
#   1. NO existe el tipo RESOLUCION. El entrenamiento no lo tenía, así que un
#      aviso de "Subsanada la avería..." caía en AVERIA por la palabra
#      "avería". Aquí cae exactamente igual. Mapearlo al tipo que le habría
#      tocado no es una interpretación: es lo que ocurría.
#   2. Los patrones van SIN corregir. La pantalla usa `suprimid`, que recoge
#      "suprimida" y "suprimidos"; el entrenamiento usaba `suprimido`, que se
#      deja "suprimida" fuera. Corregirlo aquí mejoraría la clasificación y
#      empeoraría la predicción, porque el modelo no vio esas filas marcadas.
#
# Consecuencia práctica: la pantalla puede mejorar su clasificación cuando
# haga falta sin alterar en silencio las entradas del modelo.
TIPOS_MODELO = ("SUPRESION", "AVERIA", "SERVICIO_BUS", "OBRAS", "RETRASO")

PATRONES_TIPO_MODELO = tuple(
    (tipo, re.compile(patron, re.IGNORECASE))
    for tipo, patron in (
        ("SUPRESION", r"suprimido|no presta servicio|sin circulación|no circula"),
        ("AVERIA", r"avería|averia|incidencia técnica"),
        ("SERVICIO_BUS", r"autobús|autobus|plan alternativo"),
        ("OBRAS", r"obras|reajusta|mejora infraestructura"),
        ("RETRASO", r"demora|retraso|retard"),
    )
)

# Ventana de agregación. Valor de CONTRATO: el entrenamiento usa
# MARGEN_ALERTAS_SEGUNDOS * 6 = 30 min. Cambiarlo aquí sin reentrenar hace que
# las columnas dejen de significar lo mismo.
VENTANA_MODELO_S = 30 * 60

# Nombre de las seis columnas tal como viajan en el contrato de predicción.
COLUMNA_NUM_ALERTAS = "alerts_line_30m"
COLUMNAS_TIPO = {t: f"alert_{t.lower()}_30m" for t in TIPOS_MODELO}

# Fila de ceros. El pipeline hace fillna(0) sobre las seis columnas, así que el
# modelo NUNCA vio un nulo en ellas: la ausencia de incidencias es un cero.
ALERTAS_CERO: dict[str, int] = {COLUMNA_NUM_ALERTAS: 0}
ALERTAS_CERO.update({c: 0 for c in COLUMNAS_TIPO.values()})

# RAMAS DE LÍNEA: SE CASA POR IGUALDAD EXACTA, sin normalizar la rama.
#
# Verificado sobre `cercanias_pipeline.py` y sobre las categorías del modelo
# desplegado. En el entrenamiento:
#
#   - `linea` de cada fila sale del trip_id del GTFS y SÍ trae rama. El modelo
#     conoce once categorías: C1, C2, C3, C4, C4a, C4b, C5, C7, C8a, C8b y C10.
#   - `linea_afectada` de cada alerta sale del hashtag `#MadC(\d+[ab]?)`, que en
#     la práctica publica RENFE sin rama: "#MadC4".
#   - El cruce es `merge_asof(..., by="linea")`, igualdad exacta, y después
#     `fillna(0)`.
#
# Consecuencia: una alerta de "#MadC4" NUNCA alcanzó a las filas de C4a ni C4b,
# que se quedaron con ceros. Normalizar la rama en servicio para "arreglarlo"
# activaría columnas que el modelo aprendió apagadas en esos trayectos, que es
# justo el desajuste que este módulo existe para evitar. Queda declarado como
# limitación heredada de la tabla de entrenamiento.


def clasificar_tipo_modelo(texto: str) -> str:
    """Tipo de incidencia SEGÚN EL ENTRENAMIENTO. Ver el bloque de arriba."""
    for tipo, patron in PATRONES_TIPO_MODELO:
        if patron.search(texto):
            return tipo
    return "OTRO"


def _texto_alerta(alerta: dict) -> str:
    """Extrae el texto en castellano de `descriptionText`, con `headerText` de
    reserva. En este feed `description_text` viene al 0 % de nulos y
    `header_text` al ~100 %, pero la reserva cuesta nada."""
    for clave in ("descriptionText", "headerText"):
        traducciones = (alerta.get(clave) or {}).get("translation") or []
        if not traducciones:
            continue
        en_castellano = next(
            (t for t in traducciones if t.get("language") == "es"), None
        )
        elegida = en_castellano or traducciones[0]
        texto = (elegida.get("text") or "").strip()
        if texto:
            return texto
    return ""


def _entidades_informadas(alerta: dict) -> tuple[list[str], list[str]]:
    """Devuelve (route_ids, stop_ids) de la alerta.

    Se conservan TODAS las entidades informadas. El notebook de referencia
    deduplicaba por (entity_id, fetched_at_utc) sobre la tabla ya aplanada, lo
    que colapsaba las ~18 entidades de cada alerta en una sola elegida al azar
    y hacía que el filtro por prefijo de ruta funcionase por accidente.
    Leyendo el JSON a nivel de alerta ese problema no existe.
    """
    rutas, paradas = [], []
    for entidad in alerta.get("informedEntity") or []:
        ruta = entidad.get("routeId")
        parada = entidad.get("stopId")
        if ruta:
            rutas.append(str(ruta).strip())
        if parada:
            paradas.append(str(parada).strip())
    return rutas, paradas


def extraer_lineas(texto: str, rutas: list[str]) -> list[str]:
    """Líneas afectadas, normalizadas a 'C3' / 'C4a'. Lista vacía = sin línea
    identificable (la pantalla lo trata como aviso de red)."""
    lineas = {"C" + n.lower() for n in RE_LINEA_HASHTAG.findall(texto)}
    if not lineas:
        for ruta in rutas:
            casa = RE_LINEA_RUTA.search(ruta)
            if casa:
                lineas.add("C" + casa.group(1).lower())
    return sorted(lineas)


def es_de_madrid(texto: str, rutas: list[str], paradas: list[str],
                 paradas_madrid: frozenset[str]) -> bool:
    """Tres criterios, en OR. El tercero es imprescindible: las alertas a nivel
    de parada (averías de ascensor, etc.) no traen NINGÚN marcador de núcleo
    salvo el propio `stopId`, así que sin él la sección de accesibilidad
    saldría siempre vacía."""
    if any(r.startswith("10T") for r in rutas):
        return True
    if "#mad" in texto.lower():
        return True
    if paradas_madrid and any(p in paradas_madrid for p in paradas):
        return True
    return False


def clasificar_alerta(entity_id: str, alerta: dict,
                      paradas_madrid: frozenset[str]) -> dict | None:
    """Convierte una entidad del feed en un registro clasificado.
    Devuelve None si la alerta no es del núcleo de Madrid o no tiene texto."""
    texto = _texto_alerta(alerta)
    if not texto:
        return None

    rutas, paradas = _entidades_informadas(alerta)
    if not es_de_madrid(texto, rutas, paradas, paradas_madrid):
        return None

    tipo = clasificar_tipo(texto)
    accesibilidad = bool(RE_ACCESIBILIDAD.search(texto))

    # `activePeriod.start` viene como epoch en segundos. Se guarda solo para
    # trazabilidad; la hora que se muestra es la de nuestra primera captura.
    inicio_declarado = None
    periodos = alerta.get("activePeriod") or []
    if periodos and periodos[0].get("start"):
        try:
            inicio_declarado = datetime.fromtimestamp(
                int(periodos[0]["start"]), tz=timezone.utc
            )
        except (ValueError, TypeError, OSError):
            inicio_declarado = None

    return {
        "id": entity_id,
        "texto": texto,
        "tipo": tipo,
        # Segunda clasificación, la que consume el modelo. Se calcula aquí, una
        # sola vez por alerta, y no en cada petición.
        "tipo_modelo": clasificar_tipo_modelo(texto),
        "impacto": IMPACTO_POR_TIPO.get(tipo, "BAJO"),
        "planificada": bool(RE_PLANIFICADA.search(texto)),
        "accesibilidad": accesibilidad,
        "lineas": extraer_lineas(texto, rutas),
        "paradas": sorted(set(paradas)),
        "inicio_declarado": inicio_declarado,
    }


# ---------------------------------------------------------------------------
# ALMACÉN EN MEMORIA
# ---------------------------------------------------------------------------

class AlmacenAlertas:
    """Índice en memoria de las alertas del día de servicio en curso.

    Uso desde main.py:

        almacen = AlmacenAlertas(paradas_madrid=..., nombre_parada=...)
        almacen.cargar_ultima_captura()          # instantáneo, ~3 ms
        almacen.arrancar_tarea_de_fondo()        # backfill + refresco cada 60 s
        ...
        almacen.estado()                         # para el endpoint
    """

    def __init__(self, ruta_raw: str = RUTA_RAW_ALERTS,
                 paradas_madrid: frozenset[str] | None = None,
                 nombre_parada=None):
        """
        Args:
            ruta_raw: carpeta raíz del raw de alertas del colector.
            paradas_madrid: conjunto de stop_id del núcleo de Madrid (del
                catálogo GTFS). Sin él no se detectan las alertas de parada.
            nombre_parada: función stop_id -> nombre legible, o None.
        """
        self.ruta_raw = ruta_raw
        self.paradas_madrid = paradas_madrid or frozenset()
        self._nombre_parada = nombre_parada

        self._lock = threading.Lock()
        self._indice: dict[str, dict] = {}     # entity_id -> registro
        self._ultima_captura: datetime | None = None   # UTC
        self._dia_indexado: str | None = None          # fecha Madrid, 'AAAA-MM-DD'
        self._capturas_leidas = 0
        self._capturas_vacias_seguidas = 0
        self._backfill_completo = False
        self._parar = threading.Event()

    # -- utilidades de fichero ---------------------------------------------

    def _inicio_dia_utc(self, ahora_utc: datetime) -> tuple[datetime, str]:
        """Devuelve (medianoche de Madrid en UTC, fecha Madrid como texto).

        El colector nombra las carpetas por FECHA UTC. En horario de verano,
        entre las 00:00 y las 02:00 de Madrid la carpeta UTC del día actual
        todavía no existe: hay que mirar también la del día anterior o la
        pantalla se queda en blanco dos horas cada noche.
        """
        ahora_madrid = ahora_utc.astimezone(MADRID)
        inicio_madrid = ahora_madrid.replace(hour=0, minute=0, second=0, microsecond=0)
        return inicio_madrid.astimezone(timezone.utc), inicio_madrid.strftime("%Y-%m-%d")

    def _ts_de_nombre(self, ruta: str) -> datetime | None:
        casa = RE_TS_FICHERO.search(os.path.basename(ruta))
        if not casa:
            return None
        return datetime.strptime(casa.group(1), "%Y%m%dT%H%M%S").replace(
            tzinfo=timezone.utc
        )

    def _ficheros_del_dia(self, desde_utc: datetime, hasta_utc: datetime,
                          posteriores_a: datetime | None = None) -> list[str]:
        """Ficheros de captura del rango, ordenados. Se filtra por el timestamp
        del NOMBRE, sin abrir nada: descartar 900 ficheros cuesta microsegundos."""
        rutas = []
        dia = desde_utc.date()
        while dia <= hasta_utc.date():
            patron = os.path.join(self.ruta_raw, dia.isoformat(), "*.json.gz")
            rutas.extend(glob.glob(patron))
            dia += timedelta(days=1)

        seleccion = []
        for ruta in rutas:
            ts = self._ts_de_nombre(ruta)
            if ts is None or ts < desde_utc:
                continue
            # `posteriores_a` es la última captura ya procesada: se excluye
            # ella misma para no reprocesarla en cada ciclo.
            if posteriores_a is not None and ts <= posteriores_a:
                continue
            seleccion.append((ts, ruta))
        seleccion.sort()
        return [ruta for _, ruta in seleccion]

    # -- procesado ---------------------------------------------------------

    def _procesar_fichero(self, ruta: str) -> None:
        """Lee una captura y actualiza el índice. Nunca lanza: un fichero
        corrupto o a medio escribir no puede tumbar el servicio."""
        try:
            with gzip.open(ruta, "rt", encoding="utf-8") as f:
                bruto = json.load(f)
        except (OSError, EOFError, json.JSONDecodeError, UnicodeDecodeError) as exc:
            log.warning("captura ilegible %s: %s", os.path.basename(ruta), exc)
            return

        capturado = self._ts_de_nombre(ruta) or datetime.now(timezone.utc)
        entidades = (bruto.get("payload") or {}).get("entity") or []

        self._capturas_leidas += 1
        if not entidades:
            # El feed respondió pero sin contenido. Es el patrón de fallo de
            # EMISOR ya documentado (12/07, 19/07): HTTP 200 con payload vacío.
            self._capturas_vacias_seguidas += 1
        else:
            self._capturas_vacias_seguidas = 0

        for entidad in entidades:
            entity_id = entidad.get("id")
            alerta = entidad.get("alert")
            if not entity_id or not alerta:
                continue

            registro = clasificar_alerta(entity_id, alerta, self.paradas_madrid)
            if registro is None:
                continue

            previo = self._indice.get(entity_id)
            if previo is None:
                registro["primera_vez"] = capturado
                registro["ultima_vez"] = capturado
                self._indice[entity_id] = registro
            else:
                # `entity_id` es estable entre capturas (verificado: 66 de 70
                # sobreviven 2 h), así que se conserva la primera aparición y
                # se refresca el contenido, que RENFE puede editar en vivo.
                # min/max y no asignación directa: el backfill puede procesar
                # una captura ANTERIOR a otra ya vista (arranque rápido primero,
                # día completo después).
                registro["primera_vez"] = min(previo["primera_vez"], capturado)
                registro["ultima_vez"] = max(previo["ultima_vez"], capturado)
                self._indice[entity_id] = registro

        if self._ultima_captura is None or capturado > self._ultima_captura:
            self._ultima_captura = capturado

    def _reiniciar_si_cambio_de_dia(self, dia_madrid: str) -> None:
        if self._dia_indexado != dia_madrid:
            log.info("nuevo día de servicio %s: se vacía el índice", dia_madrid)
            self._indice.clear()
            self._dia_indexado = dia_madrid
            self._capturas_leidas = 0
            self._ultima_captura = None
            self._backfill_completo = False

    # -- API pública -------------------------------------------------------

    def cargar_ultima_captura(self) -> None:
        """Lee SOLO el último fichero. Cuesta ~3 ms, así que se llama al
        arrancar para que el endpoint responda con datos desde el primer
        instante, antes de que el backfill del día haya terminado."""
        ahora = datetime.now(timezone.utc)
        desde, dia = self._inicio_dia_utc(ahora)
        with self._lock:
            self._reiniciar_si_cambio_de_dia(dia)
            ficheros = self._ficheros_del_dia(desde, ahora)
            if ficheros:
                self._procesar_fichero(ficheros[-1])

    def refrescar(self, dia_completo: bool = False) -> int:
        """Lee capturas y actualiza el índice. Devuelve cuántas ha leído.

        Args:
            dia_completo: si True, relee todas las capturas del día de servicio
                en curso (backfill de arranque, ~2,7 s medidos). Si False, solo
                las posteriores a la última procesada — un fichero por ciclo en
                régimen normal.
        """
        ahora = datetime.now(timezone.utc)
        desde, dia = self._inicio_dia_utc(ahora)
        with self._lock:
            self._reiniciar_si_cambio_de_dia(dia)
            pendientes = self._ficheros_del_dia(
                desde, ahora,
                posteriores_a=None if dia_completo else self._ultima_captura,
            )
            for ruta in pendientes:
                self._procesar_fichero(ruta)
            if dia_completo:
                self._backfill_completo = True
            return len(pendientes)

    def arrancar_tarea_de_fondo(self, periodo_s: int = 60) -> threading.Thread:
        """Hilo propio con su propia caché. Deliberadamente NO se reutiliza el
        refresco de `estado_red.py`: ese alimenta la predicción, y meter aquí
        las alertas acoplaría dos dominios de fallo (una captura corrupta de
        alertas degradaría el endpoint de consulta, que es el producto
        principal). Mismo patrón, objeto distinto.

        El periodo es de 60 s porque el colector escribe cada 60 s: refrescar
        más a menudo no aporta un dato nuevo, solo consume CPU compartida.
        """
        def bucle():
            try:
                # Backfill del día completo. Se relee también la captura que
                # cargó `cargar_ultima_captura`, que es idempotente.
                leidos = self.refrescar(dia_completo=True)
                log.info("backfill de alertas: %d capturas, %d alertas en índice",
                         leidos, len(self._indice))
            except Exception:
                log.exception("fallo en el backfill de alertas")
            while not self._parar.wait(periodo_s):
                try:
                    self.refrescar()
                except Exception:
                    log.exception("fallo al refrescar alertas")

        hilo = threading.Thread(target=bucle, name="alertas", daemon=True)
        hilo.start()
        return hilo

    def detener(self) -> None:
        self._parar.set()

    def ventana_modelo(
        self,
        t0_utc: datetime | None = None,
        ahora: datetime | None = None,
    ) -> dict[str, dict[str, int]] | None:
        """Las seis columnas de incidencias por línea, listas para el modelo.

        Devuelve {código base de línea: {alerts_line_30m, alert_<tipo>_30m...}}
        con SOLO las líneas que tienen alguna incidencia en la ventana. Las
        demás valen cero y las rellena quien construye la fila.

        Devuelve None si el feed no es utilizable (sin datos o caduco). En ese
        caso quien llama envía ceros igualmente, porque el modelo nunca vio un
        nulo en estas columnas, y marca el bloque como degradado para que la
        interfaz avise al usuario. La bandera de degradación no es una feature,
        así que se puede ser honesto con la persona sin mentirle al modelo.

        SEMÁNTICA REPLICADA del entrenamiento (§4.1 del pipeline):

          - `alert_<tipo>_30m` es binaria: ¿hubo alguna alerta de ese tipo en
            esa línea en los 30 minutos anteriores a t0? Es el
            `rolling("30min").max()` sobre la bandera del tipo.
          - `alerts_line_30m` cuenta `entity_id` DISTINTOS, es decir avisos
            distintos, no filas ni rutas informadas.
          - Todo por línea. Ni por parada ni por red completa.

        Única aproximación: el índice guarda la primera y la última vez que se
        vio cada alerta, no todas las capturas intermedias, de modo que una
        alerta que desapareciese y reapareciese dentro de la misma ventana se
        contaría como presente durante toda ella. Con t0 igual al instante de
        la consulta ese caso no puede darse, porque la última vista nunca es
        posterior a ahora.

        La accesibilidad se excluye: el entrenamiento la filtraba como ruido
        con PALABRAS_ALERTAS_RUIDO antes de clasificar.

        Las claves son la línea tal cual la publica el feed, sin normalizar la
        rama. Ver el bloque RAMAS DE LÍNEA al principio del módulo.
        """
        ahora = ahora or datetime.now(timezone.utc)
        # t0 puede venir en el futuro (el usuario pide "salgo dentro de 30
        # min"). Del futuro no hay incidencias publicadas, así que la ventana
        # se ancla en el último instante del que hay información. Es el mismo
        # criterio que usa la meteorología con el merge_asof hacia atrás.
        t0 = min(t0_utc, ahora) if t0_utc is not None else ahora
        desde = t0 - timedelta(seconds=VENTANA_MODELO_S)

        with self._lock:
            ultima = self._ultima_captura
            registros = list(self._indice.values())

        if ultima is None or (ahora - ultima).total_seconds() > MARGEN_FEED_CADUCO_S:
            return None

        acumulado: dict[str, tuple[set, set]] = {}
        for reg in registros:
            if reg["accesibilidad"]:
                continue
            if reg["primera_vez"] > t0 or reg["ultima_vez"] < desde:
                continue
            for linea in reg["lineas"]:
                # Igualdad exacta con la línea del trayecto, ramas incluidas.
                # Ver el bloque RAMAS DE LÍNEA más arriba.
                ids, tipos = acumulado.setdefault(linea, (set(), set()))
                ids.add(reg["id"])
                if reg["tipo_modelo"] in TIPOS_MODELO:
                    tipos.add(reg["tipo_modelo"])

        salida: dict[str, dict[str, int]] = {}
        for base, (ids, tipos) in acumulado.items():
            fila = {COLUMNA_NUM_ALERTAS: len(ids)}
            for tipo, columna in COLUMNAS_TIPO.items():
                fila[columna] = 1 if tipo in tipos else 0
            salida[base] = fila
        return salida

    def estado(self, ahora: datetime | None = None) -> dict:
        """Estado listo para serializar en el endpoint.

        El estado ACTIVA/RESUELTA se calcula en el momento de la petición, no
        al refrescar: así una alerta caduca sin necesidad de que llegue una
        captura nueva.
        """
        ahora = ahora or datetime.now(timezone.utc)

        with self._lock:
            ultima = self._ultima_captura
            registros = list(self._indice.values())
            vacias = self._capturas_vacias_seguidas
            capturas = self._capturas_leidas
            listo = self._backfill_completo
            dia = self._dia_indexado

        # Frescura del feed. Tres estados, no dos: "sin incidencias" y "no
        # tengo datos" son cosas distintas y la pantalla debe distinguirlas.
        if ultima is None:
            estado_feed, antiguedad = "SIN_DATOS", None
        else:
            antiguedad = (ahora - ultima).total_seconds()
            if antiguedad > MARGEN_FEED_CADUCO_S:
                estado_feed = "CADUCO"
            elif vacias >= UMBRAL_CAPTURAS_VACIAS:
                estado_feed = "EMISOR_VACIO"
            else:
                estado_feed = "OK"

        incidencias, accesibilidad = [], []
        for reg in registros:
            edad = (ahora - reg["ultima_vez"]).total_seconds()
            activa = edad <= MARGEN_ACTIVA_S
            salida = {
                "id": reg["id"],
                "texto": reg["texto"],
                "tipo": reg["tipo"],
                "impacto": reg["impacto"],
                "planificada": reg["planificada"],
                "lineas": reg["lineas"],
                "estado": "ACTIVA" if activa else "RESUELTA",
                "desde": reg["primera_vez"].astimezone(MADRID).isoformat(timespec="seconds"),
                "hasta": None if activa else reg["ultima_vez"].astimezone(MADRID).isoformat(timespec="seconds"),
                "inicio_declarado": (
                    reg["inicio_declarado"].astimezone(MADRID).isoformat(timespec="seconds")
                    if reg["inicio_declarado"] else None
                ),
            }
            if reg["accesibilidad"]:
                salida["estaciones"] = [
                    self._nombre_parada(p) if self._nombre_parada else p
                    for p in reg["paradas"]
                ]
                accesibilidad.append(salida)
            else:
                incidencias.append(salida)

        # Orden: activas primero, luego por impacto, luego lo más reciente.
        def clave(item):
            return (
                0 if item["estado"] == "ACTIVA" else 1,
                ORDEN_IMPACTO.get(item["impacto"], 3),
                item["desde"],
            )

        incidencias.sort(key=clave)
        accesibilidad.sort(key=clave)

        lineas_con_alerta = sorted({
            linea
            for item in incidencias
            if item["estado"] == "ACTIVA"
            for linea in item["lineas"]
        })

        return {
            "generado": ahora.astimezone(MADRID).isoformat(timespec="seconds"),
            "dia_servicio": dia,
            "feed": {
                "estado": estado_feed,
                "ultima_captura": (
                    ultima.astimezone(MADRID).isoformat(timespec="seconds")
                    if ultima else None
                ),
                "antiguedad_s": None if antiguedad is None else round(antiguedad),
                "capturas_indexadas": capturas,
                "indice_completo": listo,
            },
            "resumen": {
                "activas": sum(1 for i in incidencias if i["estado"] == "ACTIVA"),
                "resueltas_hoy": sum(1 for i in incidencias if i["estado"] == "RESUELTA"),
                "accesibilidad_activas": sum(
                    1 for a in accesibilidad if a["estado"] == "ACTIVA"
                ),
                "lineas_afectadas": lineas_con_alerta,
            },
            "incidencias": incidencias,
            "accesibilidad": accesibilidad,
        }
