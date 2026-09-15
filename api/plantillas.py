"""
plantillas.py — Todos los textos que el servidor enseña al usuario, en dos idiomas.

QUÉ VIVE AQUÍ. Las respuestas del asistente (chat.py), las del histórico de
puntualidad (historico.py) y la traducción de los avisos del resolutor y de
/api/consulta. La LÓGICA de cada respuesta (qué líneas entran en un ranking, qué
filtro se aplica, cuándo se avisa) sigue en su módulo y es única: aquí solo se
decide cómo se DICE. Así, añadir un idioma no puede cambiar lo que el asistente
calcula, solo cómo lo redacta.

POR QUÉ UNA CLASE POR IDIOMA Y NO UN DICCIONARIO DE CADENAS. Los plurales y los
artículos no se traducen palabra a palabra: "Hay 1 incidencia activa" y "Hay 5
incidencias activas" cambian en tres sitios, y en inglés en otros tres distintos.
Un diccionario obligaría a meter esa lógica en quien llama, que es justo lo que se
quiere evitar. Cada redactor es una clase con un método por frase.

EL ESPAÑOL ES LA REFERENCIA. RedactorES reproduce carácter a carácter los textos
que había en chat.py e historico.py antes del multiidioma. Lo comprueba
tests/plantillas_asistente.py contra una grabación previa.

QUÉ NO SE TRADUCE, a propósito:
  - Nombres de estación y códigos de línea: son identificadores del operador.
  - El texto de las incidencias: llega de Renfe en español, y traducirlo por
    máquina sería poner en boca del operador frases que no ha publicado. La
    respuesta inglesa lo declara.

El idioma lo decide la INTERFAZ (campo `idioma` de la petición), nunca el texto que
escribe el usuario. Un "responde en inglés" dentro de una pregunta no cambia nada:
es la misma regla de contención que el resto del asistente.

TFM Cercanías RENFE · UCM · 2026
"""

from __future__ import annotations

import logging
import re
from typing import Any, Iterable

log = logging.getLogger(__name__)

IDIOMAS: tuple[str, ...] = ("es", "en")


def normalizar_idioma(valor: str | None) -> str:
    """Cualquier valor desconocido cae en español, que es el idioma de referencia."""
    v = (valor or "").strip().lower()[:2]
    return v if v in IDIOMAS else "es"


# ============================================================================ ES ===
class RedactorES:
    """Textos en español. Idénticos a los anteriores al multiidioma salvo las cinco
    correcciones del commit siguiente, documentadas en la referencia de la prueba."""

    idioma = "es"

    # ------------------------------------------------------------ textos fijos ---
    AYUDA = (
        "Puedo ayudarte con los trenes de Cercanías de Madrid:\n"
        "· La hora de llegada estimada entre dos estaciones\n"
        "· Las incidencias de la red o de una línea\n"
        "· El retraso que acumula una línea ahora mismo\n"
        "· Qué línea va peor en este momento\n"
        "· Por qué he estimado un retraso concreto\n"
        "· El estado de las fuentes de datos\n"
        "También puedo llevarte a las pantallas de llegada, alertas o mapa."
    )
    FUERA = (
        "Solo puedo responder sobre los trenes de Cercanías de Madrid y sobre el uso "
        "de esta aplicación. Prueba a preguntarme por un trayecto, por las incidencias "
        "de una línea o por el estado de la red."
    )
    DEGRADADO = (
        "Ahora mismo no puedo interpretar tu pregunta porque el servicio de lenguaje no "
        "responde. Las tres pantallas de la aplicación siguen funcionando con normalidad."
    )
    SIN_CUOTA = (
        "El asistente ha alcanzado su límite de consultas de hoy. Las pantallas de "
        "llegada, alertas y mapa siguen funcionando con normalidad."
    )
    DEMASIADO_RAPIDO = "Vas muy rápido. Espera unos segundos antes de volver a preguntar."
    FALLO_COMPONER = ("No he podido consultar ese dato ahora mismo. "
                      "Las pantallas de la aplicación siguen disponibles.")
    SUGERENCIAS = [
        "¿A qué hora llego a Alcalá saliendo de Atocha?",
        "¿Qué incidencias hay ahora en la red?",
        "¿Qué línea va peor en este momento?",
        "¿Por qué has estimado ese retraso?",
    ]

    # Motivo de exclusión de las líneas con afectación estructural. Es texto de
    # producto, no configuración: por eso vive aquí y no en el .env.
    MOTIVO_ESTRUCTURAL = {"C9": "está en obras de reforma integral desde marzo"}
    MOTIVO_GENERICO = "tiene una afectación programada"

    # Nombres legibles. Antes el asistente enseñaba los códigos internos ("Averia",
    # "sin alertas ni meteo", "Incidencias: CADUCO"); son los mismos nombres que usa
    # la pantalla (web/i18n.js), para que chat y pantalla digan lo mismo.
    TIPOS = {
        "RESOLUCION": "Vuelta a la normalidad",
        "SUPRESION": "Supresión",
        "AVERIA": "Avería",
        "RETRASO": "Retraso",
        "SERVICIO_BUS": "Servicio alternativo",
        "OBRAS": "Obras",
        "OTRO": "Otra incidencia",
    }
    BLOQUES = {
        "meteo": "meteorología",
        "estado_red": "estado de la red",
        "alertas": "incidencias",
        "estado_propio": "posición del tren",
    }
    ESTADOS_FEED = {
        "OK": "al día",
        "CADUCO": "desactualizadas",
        "SIN_DATOS": "sin datos",
        "EMISOR_VACIO": "la fuente responde sin contenido",
    }

    # ------------------------------------------------------------- utilidades ---
    def motivo_estructural(self, codigo: str) -> str:
        return self.MOTIVO_ESTRUCTURAL.get(codigo.upper(), self.MOTIVO_GENERICO)

    def minutos(self, segundos: float | None) -> str:
        """Segundos a minutos legibles. Criterio del asistente."""
        if segundos is None:
            return "sin dato"
        m = segundos / 60.0
        if m < 1:
            return "menos de un minuto"
        return f"{m:.0f} min" if m >= 1.5 else "1 min"

    def minutos_historico(self, segundos: float) -> str:
        """Criterio del histórico: sin el redondeo a 1 min del asistente."""
        m = segundos / 60.0
        if abs(m) < 1:
            return "menos de un minuto"
        return f"{m:.0f} min"

    def aviso(self, texto: str | None) -> str | None:
        """Avisos del resolutor y de /api/consulta. En español pasan tal cual."""
        return texto

    # --------------------------------------------------------------- trayecto ---
    def trayecto_pide_estaciones(self) -> str:
        return ("Dime la estación de origen y la de destino y te doy la hora "
                "estimada de llegada.")

    def estacion_inexistente(self, pedido: str) -> str:
        return (f"No encuentro ninguna estación de Cercanías de Madrid "
                f"que se llame «{pedido}».")

    def estacion_ambigua(self, pedido: str, nombres: list[str], rol: str) -> str:
        """`rol` es 'origen' o 'destino'."""
        return (f"«{pedido}» puede ser varias estaciones: {', '.join(nombres)}. "
                f"¿Cuál es tu {rol}?")

    def nota_dominio_parcial(self) -> str:
        return ("\nSolo te doy los trenes que ya circulan o salen en "
                "la próxima media hora: son sobre los que puedo "
                "predecir.")

    def dominio_vacio(self) -> str:
        return ("Para ese trayecto no hay ningún tren que ya circule o salga "
                "en la próxima media hora, que es hasta donde puedo predecir. "
                "Los siguientes salen más tarde.")

    def sin_trenes(self, aviso: str | None) -> str:
        return self.aviso(aviso) or "No he encontrado trenes para ese trayecto."

    def tren_sin_prediccion(self, linea: str, salida: str, destino: str,
                            llegada: str) -> str:
        return (f"· {linea} · sale {salida} y llega a {destino} a las {llegada} "
                f"según horario. No predigo el retraso de esta línea: está "
                f"excluida del modelo por obras prolongadas.")

    def tren(self, linea: str, salida: str, destino: str, llegada: str,
             horario: str, retraso_s: float) -> str:
        return (f"· {linea} · sale {salida} y llega a {destino} a las {llegada} "
                f"(horario {horario}, retraso previsto {self.minutos(retraso_s)})")

    def nota_degradados(self, bloques: Iterable[str]) -> str:
        nombres = [self.BLOQUES.get(b, b) for b in sorted(bloques)]
        return "\nAviso: la predicción se ha hecho sin datos de " + " ni ".join(nombres) + "."

    def cabecera_trayecto(self, origen: str, destino: str) -> str:
        return f"De {origen} a {destino}:\n"

    # ---------------------------------------------------------------- alertas ---
    def alertas_sin_datos(self) -> str:
        return ("No tengo datos actualizados de incidencias en este momento. "
                "La pantalla de alertas muestra el detalle.")

    def alertas_red_vacia(self) -> str:
        return "Ahora mismo no hay incidencias activas en la red."

    def tipo_incidencia(self, tipo: str) -> str:
        return self.TIPOS.get(tipo, tipo.replace("_", " ").capitalize())

    def alertas_red(self, filas: list[tuple[str, list[str], str]], total: int) -> str:
        """`filas`: (tipo, líneas, texto ya recortado) de las que se enseñan."""
        cuerpo = "\n".join(
            f"· {self.tipo_incidencia(tipo)}"
            f"{' en ' + ', '.join(lineas) if lineas else ''}: {texto}"
            for tipo, lineas, texto in filas
        )
        extra = f"\nY {total - len(filas)} más." if total > len(filas) else ""
        s = "s" if total > 1 else ""
        return f"Hay {total} incidencia{s} activa{s}:\n{cuerpo}{extra}"

    def etiqueta_ver_incidencias(self) -> str:
        return "Ver todas las incidencias"

    def alertas_linea_pide(self) -> str:
        return "Dime qué línea te interesa, de la C1 a la C10."

    def alertas_linea_vacia(self, base: str) -> str:
        return f"No hay incidencias activas publicadas en la {base}."

    def alertas_linea(self, base: str, textos: list[str], total: int) -> str:
        cuerpo = "\n".join(f"· {t}" for t in textos)
        s = "s" if total > 1 else ""
        return f"En la {base} hay {total} incidencia{s} activa{s}:\n{cuerpo}"

    def etiqueta_ver_incidencias_linea(self, base: str) -> str:
        return f"Ver incidencias de la {base}"

    # ---------------------------------------------------------- estado de línea ---
    def estado_linea_pide(self) -> str:
        return "Dime qué línea quieres consultar, de la C1 a la C10."

    def estado_linea_sin_datos(self, base: str) -> str:
        return (f"No tengo datos recientes de la {base}. Puede que no haya "
                f"trenes suyos circulando ahora mismo.")

    def estado_linea(self, base: str, media_s: float, trenes: int,
                     pocos: bool, estructural: bool) -> str:
        cautela = " Son pocos trenes, así que la media es poco representativa." if pocos else ""
        if estructural:
            cautela += (f" Ten en cuenta que la {base} {self.motivo_estructural(base)}, "
                        f"así que ese retraso es habitual y no responde a una "
                        f"incidencia puntual.")
        return (f"La {base} acumula un retraso medio de {self.minutos(media_s)} en los "
                f"últimos 30 minutos, con {trenes} trenes en circulación.{cautela}")

    # ---------------------------------------------------------------- ranking ---
    def ranking_insuficiente(self) -> str:
        return ("No tengo suficientes líneas con trenes en circulación para "
                "compararlas ahora mismo. El mapa muestra el detalle.")

    def ranking(self, peores: list[tuple[str, float, int]],
                mejor: tuple[str, float, int], comparadas: int,
                escasas: list[str], estructurales: list[str],
                min_trenes: int) -> str:
        detalle = ", ".join(
            f"la {base} con {self.minutos(d)} sobre {t} trenes" for base, d, t in peores
        )
        if len(peores) == 1:
            encabezado = f"Ahora mismo la línea con más retraso medio es {detalle}."
        else:
            encabezado = (f"Ahora mismo las {len(peores)} líneas con más retraso medio "
                          f"en los últimos 30 minutos son: {detalle}.")
        nota = ""
        if escasas:
            verbo = "se ha excluido la" if len(escasas) == 1 else "se han excluido las"
            nota += (f" Además, {verbo} {', '.join(escasas)} por tener menos de "
                     f"{min_trenes} trenes en circulación: con tan "
                     f"pocos, la media no sería representativa.")
        if estructurales:
            motivos = "; ".join(f"la {c}, que {self.motivo_estructural(c)}"
                                for c in estructurales)
            fuera = "Queda fuera" if len(estructurales) == 1 else "Quedan fuera"
            nota += (f" {fuera} de la comparación {motivos}: su retraso es una "
                     f"condición permanente del servicio, no una incidencia de hoy.")
        base_m, d_m, t_m = mejor
        return (f"{encabezado} La que mejor va es la {base_m}, con "
                f"{self.minutos(d_m)} sobre {t_m} trenes. Comparadas "
                f"{comparadas} líneas.{nota}")

    def etiqueta_ver_mapa(self) -> str:
        return "Ver el mapa de la red"

    # --------------------------------------------------------------- histórico ---
    def historico_ausente(self) -> str:
        return ("Todavía no puedo responder sobre puntualidad histórica: este "
                "servicio solo tiene el estado en tiempo real. Puedo decirte qué "
                "línea va peor ahora mismo.")

    def historico_salvedad(self, desde: str, hasta: str, dias: int, **_: Any) -> str:
        return (f" Son datos de {desde} a {hasta} ({dias} días) y miden el retraso que "
                f"publica Renfe, no la predicción del modelo.")

    def historico_desconocida(self, codigo: str, disponibles: str) -> str:
        return f"No tengo histórico de la {codigo}. Tengo datos de: {disponibles}."

    def historico_linea(self, linea: str, pct: float, margen: int,
                        mediana_s: float, p90_s: float) -> str:
        return (f"La {linea} llega puntual en el {pct:.0f}% de las "
                f"observaciones, tomando puntual como {margen} minutos o menos. "
                f"Su retraso mediano es de {self.minutos_historico(mediana_s)} y "
                f"el 10% de los trenes acumula más de "
                f"{self.minutos_historico(p90_s)}.")

    def historico_general(self, mejor: str, pct_m: float, peor: str, pct_p: float,
                          margen: int, comparadas: int) -> str:
        return (f"La línea más puntual es la {mejor}: llega dentro de {margen} "
                f"minutos en el {pct_m:.0f}% de las observaciones. La "
                f"menos puntual es la {peor}, con un {pct_p:.0f}%. "
                f"Comparadas {comparadas} líneas.")

    # -------------------------------------------------------------- explicación ---
    def explicar_vacio(self) -> str:
        return ("Pregúntame primero por un trayecto y te explico en qué me baso "
                "para esa estimación.")

    def explicar(self, retraso_s: float, fila: dict, linea: str,
                 tipos: list[str]) -> str:
        minutos_h = int((fila.get("horizon_s") or 0) / 60)
        partes = [
            f"horizonte de {minutos_h} {'minuto' if minutos_h == 1 else 'minutos'} "
            f"hasta la llegada",
            ("el tren ya está en circulación" if fila.get("regime") == "A"
             else "el tren aún no ha salido de cabecera"),
        ]
        if fila.get("line_delay_mean_30m_s") is not None:
            partes.append(f"retraso medio de la {linea} de "
                          f"{self.minutos(float(fila['line_delay_mean_30m_s']))} en los "
                          f"últimos 30 minutos")
        if tipos:
            partes.append("incidencias de tipo " +
                          ", ".join(self.tipo_incidencia(t).lower() for t in tipos))
        else:
            partes.append("sin incidencias publicadas en la línea")
        if fila.get("temp_c") is not None:
            partes.append(f"{float(fila['temp_c']):.0f} °C")
            if (fila.get("precip_mm_1h") or 0) > 0:
                partes.append(f"{float(fila['precip_mm_1h']):.1f} mm de lluvia acumulada")
        return ("La estimación de " + self.minutos(retraso_s) + " sale de un modelo "
                "entrenado con el histórico de la red. Los valores con los que se "
                "calculó fueron: " + "; ".join(partes) + ". No es una explicación de "
                "la causa del retraso, sino de los datos de entrada.")

    # ---------------------------------------------------------------- sistema ---
    def sistema(self, vigente: bool, trenes: Any, posiciones: Any,
                incidencias: Any, backend: Any) -> str:
        return (f"Estado de las fuentes:\n"
                f"· Estado de la red: {'al día' if vigente else 'sin dato vigente'}"
                f" ({trenes} trenes en el feed)\n"
                f"· Posiciones: {self.ESTADOS_FEED.get(posiciones, posiciones)}\n"
                f"· Incidencias: {self.ESTADOS_FEED.get(incidencias, incidencias)}\n"
                f"· Modelo: backend {backend}")

    # -------------------------------------------------------------- navegación ---
    def navegar_pide(self) -> str:
        return "Puedo llevarte a llegada, alertas o mapa. ¿Cuál quieres?"

    def navegar(self, pantalla: str) -> str:
        # Frases completas y no un diccionario de sustantivos: con "a " + nombre
        # salía "Te llevo a el mapa".
        return {
            "llegada": "Te llevo a la pantalla de llegada estimada.",
            "alertas": "Te llevo a las incidencias.",
            "mapa": "Te llevo al mapa de la red.",
        }[pantalla]


# ============================================================================ EN ===
# Avisos que el servidor compone en español (resolver.py y main.py). Se traducen en
# la frontera, al devolverlos, para no tocar la lógica que los genera. Si alguien
# cambia una frase en español y no la actualiza aquí, el usuario inglés la verá en
# español, que es un fallo visible pero inocuo; tests/plantillas_asistente.py lo
# detecta antes.
_AVISOS_FIJOS_EN = {
    "El origen y el destino son la misma estación.":
        "Origin and destination are the same station.",
    "Alguna de las estaciones no pertenece al núcleo de Madrid.":
        "One of the stations is not part of the Madrid network.",
    "No hay tren directo entre estas dos estaciones. Los trayectos con "
    "transbordo aún no están disponibles.":
        "There is no direct train between these two stations. Journeys with a "
        "change of train are not available yet.",
    "Solo se muestran los trenes que ya circulan o que salen en la próxima "
    "media hora: son aquellos sobre los que el modelo puede predecir.":
        "Only trains that are already running or depart within the next 30 minutes "
        "are shown: those are the ones the model can predict.",
    "No hay trenes que salgan en la próxima media hora para ese trayecto. "
    "El modelo solo predice sobre trenes que ya circulan o están a punto "
    "de salir.":
        "No trains depart on this route in the next 30 minutes. The model only "
        "predicts trains that are already running or about to depart.",
    "Estación no encontrada.": "Station not found.",
    "Formato de fecha no válido. Se espera ISO-8601 UTC, "
    "por ejemplo 2026-09-18T07:30:00Z.":
        "Invalid date format. ISO-8601 UTC expected, for example "
        "2026-09-18T07:30:00Z.",
    "El servicio de predicción no está disponible en este momento.":
        "The prediction service is not available right now.",
}

_AVISOS_PATRON_EN = [
    (re.compile(r"No hay trenes directos entre estas estaciones en los próximos "
                r"(\d+) minutos\."),
     "No direct trains between these stations in the next {0} minutes."),
    (re.compile(r"La (\S+) está excluida del modelo por obras prolongadas, así que no "
                r"se predice su retraso\. Se muestra el horario oficial\."),
     "Line {0} is excluded from the model due to long-running engineering works, "
     "so its delay is not predicted. The official timetable is shown."),
]


class RedactorEN(RedactorES):
    """Textos en inglés. Hereda del español solo para que un método olvidado
    falle hacia el idioma de referencia en vez de lanzar una excepción."""

    idioma = "en"

    AYUDA = (
        "I can help you with Madrid Cercanías commuter trains:\n"
        "· Estimated arrival time between two stations\n"
        "· Incidents on the network or on a line\n"
        "· How much delay a line has right now\n"
        "· Which line is doing worst at the moment\n"
        "· Why I estimated a particular delay\n"
        "· The status of the data sources\n"
        "I can also take you to the arrival, alerts or map screens."
    )
    FUERA = (
        "I can only answer questions about Madrid Cercanías trains and about how to "
        "use this app. Try asking me about a journey, the incidents on a line or the "
        "state of the network."
    )
    DEGRADADO = (
        "I can't interpret your question right now because the language service is "
        "not responding. The three screens of the app are still working normally."
    )
    SIN_CUOTA = (
        "The assistant has reached its query limit for today. The arrival, alerts "
        "and map screens are still working normally."
    )
    DEMASIADO_RAPIDO = "You're going too fast. Wait a few seconds before asking again."
    FALLO_COMPONER = ("I couldn't look up that information right now. "
                      "The app screens are still available.")
    SUGERENCIAS = [
        "When do I get to Alcalá if I leave from Atocha?",
        "Are there any incidents on the network now?",
        "Which line is doing worst right now?",
        "Why did you estimate that delay?",
    ]

    MOTIVO_ESTRUCTURAL = {"C9": "has been closed for a full refurbishment since March"}
    MOTIVO_GENERICO = "is affected by planned works"

    TIPOS = {
        "RESOLUCION": "Service restored",
        "SUPRESION": "Cancellation",
        "AVERIA": "Breakdown",
        "RETRASO": "Delay",
        "SERVICIO_BUS": "Replacement bus",
        "OBRAS": "Engineering works",
        "OTRO": "Other incident",
    }

    BLOQUES = {
        "meteo": "weather",
        "estado_red": "network status",
        "alertas": "incidents",
        "estado_propio": "train position",
    }

    ESTADOS_FEED = {
        "OK": "OK",
        "CADUCO": "out of date",
        "SIN_DATOS": "no data",
        "EMISOR_VACIO": "source returning no content",
    }

    NOTA_TEXTO_RENFE = "\n(Incident descriptions are shown as published by Renfe, in Spanish.)"

    # ------------------------------------------------------------- utilidades ---
    @staticmethod
    def _enumerar(elementos: list[str]) -> str:
        """['C5', 'C7', 'C8'] -> 'C5, C7 and C8'."""
        if len(elementos) <= 1:
            return "".join(elementos)
        return ", ".join(elementos[:-1]) + " and " + elementos[-1]

    def minutos(self, segundos: float | None) -> str:
        if segundos is None:
            return "no data"
        m = segundos / 60.0
        if m < 1:
            return "less than a minute"
        return f"{m:.0f} min" if m >= 1.5 else "1 min"

    def minutos_historico(self, segundos: float) -> str:
        m = segundos / 60.0
        if abs(m) < 1:
            return "less than a minute"
        return f"{m:.0f} min"

    def aviso(self, texto: str | None) -> str | None:
        if not texto:
            return texto
        if texto in _AVISOS_FIJOS_EN:
            return _AVISOS_FIJOS_EN[texto]
        for patron, plantilla in _AVISOS_PATRON_EN:
            m = patron.fullmatch(texto)
            if m:
                return plantilla.format(*m.groups())
        log.warning("Aviso sin traducción al inglés: %s", texto[:80])
        return texto

    # --------------------------------------------------------------- trayecto ---
    def trayecto_pide_estaciones(self) -> str:
        return ("Tell me the origin and destination stations and I'll give you the "
                "estimated arrival time.")

    def estacion_inexistente(self, pedido: str) -> str:
        return f"I can't find any Madrid Cercanías station called “{pedido}”."

    def estacion_ambigua(self, pedido: str, nombres: list[str], rol: str) -> str:
        rol_en = "origin" if rol == "origen" else "destination"
        return (f"“{pedido}” could be several stations: {', '.join(nombres)}. "
                f"Which one is your {rol_en}?")

    def nota_dominio_parcial(self) -> str:
        return ("\nI only list trains that are already running or depart within the "
                "next 30 minutes: those are the ones I can predict.")

    def dominio_vacio(self) -> str:
        return ("No train on this route is running or departing within the next "
                "30 minutes, which is as far ahead as I can predict. The next ones "
                "leave later.")

    def sin_trenes(self, aviso: str | None) -> str:
        return self.aviso(aviso) or "I couldn't find any trains for that journey."

    def tren_sin_prediccion(self, linea: str, salida: str, destino: str,
                            llegada: str) -> str:
        return (f"· {linea} · departs {salida} and arrives at {destino} at {llegada} "
                f"according to the timetable. I don't predict delays on this line: "
                f"it is excluded from the model due to long-running works.")

    def tren(self, linea: str, salida: str, destino: str, llegada: str,
             horario: str, retraso_s: float) -> str:
        return (f"· {linea} · departs {salida} and arrives at {destino} at {llegada} "
                f"(scheduled {horario}, expected delay {self.minutos(retraso_s)})")

    def nota_degradados(self, bloques: Iterable[str]) -> str:
        nombres = [self.BLOQUES.get(b, b) for b in sorted(bloques)]
        return "\nNote: the prediction was made without " + " or ".join(nombres) + " data."

    def cabecera_trayecto(self, origen: str, destino: str) -> str:
        return f"From {origen} to {destino}:\n"

    # ---------------------------------------------------------------- alertas ---
    def alertas_sin_datos(self) -> str:
        return ("I don't have up-to-date incident data right now. "
                "The alerts screen shows the details.")

    def alertas_red_vacia(self) -> str:
        return "There are no active incidents on the network right now."

    def tipo_incidencia(self, tipo: str) -> str:
        return self.TIPOS.get(tipo, tipo.replace("_", " ").capitalize())

    def alertas_red(self, filas: list[tuple[str, list[str], str]], total: int) -> str:
        cuerpo = "\n".join(
            f"· {self.tipo_incidencia(tipo)}"
            f"{' on ' + ', '.join(lineas) if lineas else ''}: {texto}"
            for tipo, lineas, texto in filas
        )
        extra = f"\nAnd {total - len(filas)} more." if total > len(filas) else ""
        cabeza = ("There is 1 active incident" if total == 1
                  else f"There are {total} active incidents")
        return f"{cabeza}:\n{cuerpo}{extra}{self.NOTA_TEXTO_RENFE}"

    def etiqueta_ver_incidencias(self) -> str:
        return "See all incidents"

    def alertas_linea_pide(self) -> str:
        return "Tell me which line you're interested in, from C1 to C10."

    def alertas_linea_vacia(self, base: str) -> str:
        return f"There are no active incidents published for line {base}."

    def alertas_linea(self, base: str, textos: list[str], total: int) -> str:
        cuerpo = "\n".join(f"· {t}" for t in textos)
        cabeza = ("there is 1 active incident" if total == 1
                  else f"there are {total} active incidents")
        return f"On line {base} {cabeza}:\n{cuerpo}{self.NOTA_TEXTO_RENFE}"

    def etiqueta_ver_incidencias_linea(self, base: str) -> str:
        return f"See incidents on {base}"

    # ---------------------------------------------------------- estado de línea ---
    def estado_linea_pide(self) -> str:
        return "Tell me which line you want to check, from C1 to C10."

    def estado_linea_sin_datos(self, base: str) -> str:
        return (f"I have no recent data for line {base}. There may be none of its "
                f"trains running right now.")

    def estado_linea(self, base: str, media_s: float, trenes: int,
                     pocos: bool, estructural: bool) -> str:
        cautela = " That is only a few trains, so the average is not very representative." if pocos else ""
        if estructural:
            cautela += (f" Bear in mind that line {base} {self.motivo_estructural(base)}, "
                        f"so this delay is usual and not caused by a one-off incident.")
        unidad = "train" if trenes == 1 else "trains"
        return (f"Line {base} has an average delay of {self.minutos(media_s)} over the "
                f"last 30 minutes, with {trenes} {unidad} running.{cautela}")

    # ---------------------------------------------------------------- ranking ---
    def ranking_insuficiente(self) -> str:
        return ("There aren't enough lines with trains running to compare them right "
                "now. The map shows the details.")

    def ranking(self, peores: list[tuple[str, float, int]],
                mejor: tuple[str, float, int], comparadas: int,
                escasas: list[str], estructurales: list[str],
                min_trenes: int) -> str:
        detalle = ", ".join(
            f"{base} with {self.minutos(d)} over {t} trains" for base, d, t in peores
        )
        if len(peores) == 1:
            encabezado = f"Right now the line with the highest average delay is {detalle}."
        else:
            encabezado = (f"Right now the {len(peores)} lines with the highest average "
                          f"delay over the last 30 minutes are: {detalle}.")
        nota = ""
        if escasas:
            sujeto = "line" if len(escasas) == 1 else "lines"
            verbo = "was" if len(escasas) == 1 else "were"
            nota += (f" {sujeto.capitalize()} {self._enumerar(escasas)} {verbo} left out "
                     f"because fewer than {min_trenes} trains are running: with so "
                     f"few, the average would not be representative.")
        if estructurales:
            motivos = "; ".join(f"{c}, which {self.motivo_estructural(c)}"
                                for c in estructurales)
            nota += (f" Not included in the comparison: {motivos}. Its delay is a "
                     f"permanent condition of the service, not today's incident."
                     if len(estructurales) == 1 else
                     f" Not included in the comparison: {motivos}. Their delays are a "
                     f"permanent condition of the service, not today's incidents.")
        base_m, d_m, t_m = mejor
        return (f"{encabezado} The best-performing line is {base_m}, with "
                f"{self.minutos(d_m)} over {t_m} trains. {comparadas} lines "
                f"compared.{nota}")

    def etiqueta_ver_mapa(self) -> str:
        return "See the network map"

    # --------------------------------------------------------------- histórico ---
    def historico_ausente(self) -> str:
        return ("I can't answer about historical punctuality yet: this service only "
                "has the real-time status. I can tell you which line is doing worst "
                "right now.")

    def historico_salvedad(self, desde: str, hasta: str, dias: int, **_: Any) -> str:
        return (f" Data from {desde} to {hasta} ({dias} days), measuring the delay "
                f"published by Renfe, not the model's prediction.")

    def historico_desconocida(self, codigo: str, disponibles: str) -> str:
        return f"I have no history for line {codigo}. I have data for: {disponibles}."

    def historico_linea(self, linea: str, pct: float, margen: int,
                        mediana_s: float, p90_s: float) -> str:
        return (f"Line {linea} arrives on time in {pct:.0f}% of observations, "
                f"counting on time as {margen} minutes or less. Its median delay is "
                f"{self.minutos_historico(mediana_s)} and 10% of trains are more than "
                f"{self.minutos_historico(p90_s)} late.")

    def historico_general(self, mejor: str, pct_m: float, peor: str, pct_p: float,
                          margen: int, comparadas: int) -> str:
        return (f"The most punctual line is {mejor}: it arrives within {margen} "
                f"minutes in {pct_m:.0f}% of observations. The least punctual is "
                f"{peor}, at {pct_p:.0f}%. {comparadas} lines compared.")

    # -------------------------------------------------------------- explicación ---
    def explicar_vacio(self) -> str:
        return ("Ask me about a journey first and I'll explain what that estimate "
                "is based on.")

    def explicar(self, retraso_s: float, fila: dict, linea: str,
                 tipos: list[str]) -> str:
        minutos_h = int((fila.get("horizon_s") or 0) / 60)
        partes = [
            f"{minutos_h} {'minute' if minutos_h == 1 else 'minutes'} until arrival",
            ("the train is already running" if fila.get("regime") == "A"
             else "the train has not left its first station yet"),
        ]
        if fila.get("line_delay_mean_30m_s") is not None:
            partes.append(f"average delay on {linea} of "
                          f"{self.minutos(float(fila['line_delay_mean_30m_s']))} over "
                          f"the last 30 minutes")
        if tipos:
            partes.append("incidents of type " +
                          ", ".join(self.tipo_incidencia(t).lower() for t in tipos))
        else:
            partes.append("no incidents published on the line")
        if fila.get("temp_c") is not None:
            partes.append(f"{float(fila['temp_c']):.0f} °C")
            if (fila.get("precip_mm_1h") or 0) > 0:
                partes.append(f"{float(fila['precip_mm_1h']):.1f} mm of rain")
        return ("The estimate of " + self.minutos(retraso_s) + " comes from a model "
                "trained on the network's history. The inputs used were: " +
                "; ".join(partes) + ". This does not explain the cause of the delay, "
                "only the data it was calculated from.")

    # ---------------------------------------------------------------- sistema ---
    def sistema(self, vigente: bool, trenes: Any, posiciones: Any,
                incidencias: Any, backend: Any) -> str:
        return (f"Data source status:\n"
                f"· Network status: {'up to date' if vigente else 'no current data'}"
                f" ({trenes} trains in the feed)\n"
                f"· Positions: {self.ESTADOS_FEED.get(posiciones, posiciones)}\n"
                f"· Incidents: {self.ESTADOS_FEED.get(incidencias, incidencias)}\n"
                f"· Model: backend {backend}")

    # -------------------------------------------------------------- navegación ---
    def navegar_pide(self) -> str:
        return "I can take you to the arrival, alerts or map screen. Which one?"

    def navegar(self, pantalla: str) -> str:
        return {
            "llegada": "Taking you to the estimated arrival screen.",
            "alertas": "Taking you to the incidents.",
            "mapa": "Taking you to the network map.",
        }[pantalla]


_REDACTORES = {"es": RedactorES(), "en": RedactorEN()}


def redactor(idioma: str | None) -> RedactorES:
    """Redactor del idioma pedido. Sin estado: se comparte entre peticiones."""
    return _REDACTORES[normalizar_idioma(idioma)]
