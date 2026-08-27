"""
tiempo.py — Conversiones horarias del proyecto. Única fuente de verdad.

Todo el manejo de husos, fechas de servicio y horas del GTFS pasa por aquí. Concentrarlo
en un módulo no es purismo: el error de restar horas del día sin tener en cuenta la fecha
de servicio ya produjo retrasos de ±86.400 s (±24 h) en la tabla de modelado del equipo.
Si la conversión vive en un solo sitio, ese error solo puede cometerse una vez.

Convenciones (las mismas que el contrato de predicción):
  - Los instantes viajan siempre en UTC, ISO-8601 con sufijo 'Z'.
  - Las features de calendario se derivan en Europe/Madrid.
  - La fecha de servicio (service_date) NO es la fecha natural: un tren de las 00:30
    pertenece al día de servicio anterior.
  - Las horas del GTFS pueden pasar de 24:00 ('25:10:00') y no se truncan nunca.

TFM Cercanías RENFE · UCM · 2026
"""

from __future__ import annotations

from datetime import date, datetime, time, timedelta, timezone
from zoneinfo import ZoneInfo

MADRID = ZoneInfo("Europe/Madrid")
UTC = timezone.utc

# Cercanías no circula aproximadamente entre las 01:00 y las 05:00, así que cortar el
# día de servicio a las 04:00 hora local es seguro: nunca parte un servicio por la mitad.
HORA_CORTE_SERVICIO = 4


def ahora_utc() -> datetime:
    """Instante actual en UTC, con zona horaria explícita (nunca ingenuo)."""
    return datetime.now(UTC)


def iso_utc(dt: datetime) -> str:
    """datetime -> '2026-09-18T07:12:00Z'. Formato del contrato de predicción."""
    return dt.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def desde_iso(texto: str) -> datetime:
    """'2026-09-18T07:12:00Z' -> datetime en UTC. Acepta también offsets explícitos."""
    dt = datetime.fromisoformat(texto.replace("Z", "+00:00"))
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    return dt.astimezone(UTC)


def fecha_de_servicio(instante_utc: datetime) -> date:
    """Fecha de servicio GTFS a la que pertenece un instante.

    Todo lo anterior a las 04:00 hora de Madrid cuenta como el día anterior.
    """
    local = instante_utc.astimezone(MADRID)
    if local.hour < HORA_CORTE_SERVICIO:
        local -= timedelta(days=1)
    return local.date()


def _base_del_dia_de_servicio(fecha_servicio: date) -> datetime:
    """Instante UTC que corresponde al 'segundo 0' de una fecha de servicio.

    La especificación GTFS define las horas respecto a *mediodía menos doce horas* de la
    fecha de servicio, no respecto a la medianoche. La diferencia solo se nota los dos
    días del año con cambio de hora, pero calcularlo bien cuesta lo mismo: se toma el
    mediodía local (que nunca cae en la hora saltada), se pasa a UTC y se restan 12 h.
    """
    mediodia_local = datetime.combine(fecha_servicio, time(12, 0), tzinfo=MADRID)
    return mediodia_local.astimezone(UTC) - timedelta(hours=12)


def hora_gtfs_a_utc(fecha_servicio: date, segundos: int) -> datetime:
    """(fecha de servicio, segundos del GTFS) -> instante UTC.

    Ejemplo: fecha 2026-09-18 y 90.600 s ('25:10:00') devuelve la 01:10 del 19 de
    septiembre en hora de Madrid, expresada en UTC.
    """
    return _base_del_dia_de_servicio(fecha_servicio) + timedelta(seconds=segundos)


def utc_a_segundos_de_servicio(instante_utc: datetime, fecha_servicio: date) -> int:
    """Operación inversa: cuántos segundos de la fecha de servicio han transcurrido.

    Puede devolver valores mayores que 86.400 (madrugada del día siguiente) o negativos
    (instante anterior al inicio del día de servicio). Ambos son legítimos.
    """
    delta = instante_utc - _base_del_dia_de_servicio(fecha_servicio)
    return int(delta.total_seconds())


def formatear_local(instante_utc: datetime) -> str:
    """'07:12' en hora de Madrid. Solo para mostrar al usuario, nunca para calcular."""
    return instante_utc.astimezone(MADRID).strftime("%H:%M")


if __name__ == "__main__":
    # Prueba de humo: los tres casos que rompen las implementaciones ingenuas.
    ahora = ahora_utc()
    print(f"Ahora UTC        : {iso_utc(ahora)}")
    print(f"Hora de Madrid   : {formatear_local(ahora)}")
    print(f"Fecha de servicio: {fecha_de_servicio(ahora)}")

    # Caso 1: tren de madrugada. Pertenece al día de servicio anterior.
    madrugada = desde_iso("2026-09-19T00:30:00Z")  # 02:30 en Madrid
    print(f"\n02:30 de Madrid del 19/09 -> fecha de servicio {fecha_de_servicio(madrugada)}")

    # Caso 2: hora del GTFS mayor que 24:00.
    instante = hora_gtfs_a_utc(date(2026, 9, 18), 25 * 3600 + 10 * 60)
    print(f"'25:10:00' del 18/09      -> {iso_utc(instante)} "
          f"({formatear_local(instante)} de Madrid)")

    # Caso 3: ida y vuelta, debe conservar el valor exacto.
    segundos = utc_a_segundos_de_servicio(instante, date(2026, 9, 18))
    print(f"Vuelta a segundos         -> {segundos} (esperado 90600)")
