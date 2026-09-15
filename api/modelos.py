"""
modelos.py — Esquemas de petición y respuesta de la API.

Define el contrato entre el navegador y el servidor. FastAPI los usa para validar la
entrada, serializar la salida y generar la documentación automática en /docs.

DECISIÓN DE DISEÑO: un trayecto es siempre una LISTA DE TRAMOS, aunque en la versión 1
solo se resuelvan trayectos directos y la lista tenga siempre un elemento. Modelar el
caso general desde el principio cuesta lo mismo hoy y evita reescribir el resolutor y
la pantalla de resultado cuando se añadan los transbordos (medido: los directos cubren
el 24,5% de los pares origen-destino; con un transbordo en Atocha se llega al 88,2%).

TFM Cercanías RENFE · UCM · 2026
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field

# Idioma de la INTERFAZ. Lo fija el selector de la página, nunca el texto que escribe
# el usuario. Por defecto español: una página antigua en caché que no envíe el campo
# sigue funcionando exactamente igual que antes del multiidioma.
Idioma = Literal["es", "en"]


class Estacion(BaseModel):
    """Estación de la red, tal como la consume el selector de la interfaz."""
    stop_id: str
    nombre: str
    lat: float
    lon: float
    lineas: list[str]


class ConsultaTrayecto(BaseModel):
    """Lo que envía el navegador: origen, destino y cuándo se quiere salir."""
    origen: str = Field(..., description="stop_id de la estación de origen")
    destino: str = Field(..., description="stop_id de la estación de destino")
    salida_desde_utc: str | None = Field(
        None,
        description="Instante ISO-8601 UTC a partir del cual se quiere salir. "
                    "Si se omite, se usa el momento actual.",
    )
    idioma: Idioma = Field("es", description="Idioma de los avisos de la respuesta.")


class Retraso(BaseModel):
    """Retraso predicho en segundos, con su intervalo de incertidumbre."""
    p10: float
    p50: float
    p90: float
    con_intervalo: bool = True


class ParadaTramo(BaseModel):
    """Extremo de un tramo: dónde se sube o se baja el viajero."""
    stop_id: str
    nombre: str
    hora_teorica_utc: str


class Tramo(BaseModel):
    """Un tren concreto entre dos paradas. Un trayecto se compone de 1..N tramos."""
    trip_id: str
    line_id: str
    origen: ParadaTramo
    destino: ParadaTramo
    paradas_intermedias: int
    retraso_s: Retraso
    llegada_estimada_utc: str
    regime: str = Field(..., description="'A' tren en circulación · 'B' aún no salido")
    degraded_blocks: list[str] = []


class OpcionTrayecto(BaseModel):
    """Una forma de hacer el trayecto: uno o varios tramos encadenados."""
    tramos: list[Tramo]
    n_transbordos: int = 0
    salida_teorica_utc: str
    llegada_teorica_utc: str
    llegada_estimada_utc: str
    retraso_total_s: float
    enlace_en_riesgo: bool = Field(
        False,
        description="Cierto si el retraso predicho del tramo anterior se come el "
                    "margen de transbordo. Siempre falso mientras solo haya directos.",
    )


class RespuestaConsulta(BaseModel):
    """Respuesta completa de /api/consulta."""
    origen: Estacion
    destino: Estacion
    consultado_en_utc: str
    gtfs_version: str
    opciones: list[OpcionTrayecto]
    aviso: str | None = Field(
        None,
        description="Mensaje para el usuario cuando no hay directos o los datos "
                    "están degradados.",
    )


# ======================================================================== chat =====
# Esquemas del asistente conversacional (F9). El historial lo mantiene el navegador
# y lo devuelve en cada petición: el servidor no guarda conversaciones. Es lo que
# permite acotar el contexto por diseño y no por confianza en el cliente, porque el
# límite de turnos se aplica también aquí, en la validación de entrada.


class MensajeChat(BaseModel):
    """Un turno del historial que devuelve el navegador.

    El límite es MAYOR que el de `ConsultaChat.texto` a propósito: aquí no viaja
    solo lo que escribió el usuario, sino también lo que respondió el asistente,
    que es más largo. Con los dos límites iguales, la segunda petición de cada
    conversación se rechazaba con un 422 (detectado el 13/09). El cliente recorta
    a 200; este margen existe para que un cliente descuidado no rompa el servicio.
    """
    rol: str = Field(..., description="'usuario' o 'asistente'")
    texto: str = Field(..., max_length=400)


class ConsultaChat(BaseModel):
    """Lo que envía la ventana flotante del asistente.

    `max_length` en el texto y en el historial no es cosmético: son el primer
    control de consumo, y actúa antes de gastar un solo token del proveedor.
    """
    texto: str = Field(..., max_length=300)
    sesion: str | None = Field(
        None,
        description="Identificador de sesión generado por el navegador. Solo sirve "
                    "para poder explicar la última predicción; no identifica a nadie.",
    )
    historial: list[MensajeChat] = Field(default_factory=list, max_length=4)
    idioma: Idioma = Field("es", description="Idioma en que se redacta la respuesta.")