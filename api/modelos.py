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

from pydantic import BaseModel, Field


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