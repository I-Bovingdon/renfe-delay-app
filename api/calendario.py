"""
calendario.py — Festivos y periodo lectivo de la Comunidad de Madrid.

MÓDULO COMPARTIDO. Lo usan dos sitios y tienen que usar exactamente este:
  - El servicio de predicción, al construir las features de una consulta.
  - El código de entrenamiento del equipo, al construir la tabla de modelado.

Si cada lado calcula los festivos a su manera, el modelo recibe en producción unos
valores que no significan lo mismo que los que aprendió. No da error: da predicciones
peores sin que nadie se entere. Eso es train/serve skew.

Dependencia: pip install holidays

FUENTES DE LAS FECHAS ESCOLARES (Comunidad de Madrid):
  - Fin del curso 2025/2026: 19 de junio de 2026 (Orden 1476/2025).
  - Inicio del curso 2026/2027: 7 de septiembre de 2026 para Infantil, Primaria y
    Educación Especial; 8 de septiembre para ESO, Bachillerato y FP (Orden 2034/2026,
    BOCM de 4 de junio de 2026).

Se toma el 7 de septiembre como frontera única: es la primera oleada de vuelta al cole
y la que mueve la demanda de Cercanías (desplazamientos escolares y de acompañantes).
Distinguir el 7 del 8 añadiría una categoría más para un solo día de diferencia.

TFM Cercanías RENFE · UCM · 2026
"""

from __future__ import annotations

from datetime import date

import holidays

# Festivos nacionales + autonómicos de la Comunidad de Madrid.
# La librería expande los años bajo demanda, así que no hay que declararlos.
_FESTIVOS_MADRID = holidays.country_holidays("ES", subdiv="MD")

# Festivos LOCALES del municipio de Madrid. No los cubre la librería porque cada
# ayuntamiento fija los suyos. Los dos de la capital son fijos año tras año.
_LOCALES_MADRID_CAPITAL = {
    (5, 15),   # San Isidro
    (11, 9),   # Nuestra Señora de la Almudena
}

# Periodos lectivos que cubren la ventana de captura del proyecto.
# (inicio inclusive, fin inclusive)
_PERIODOS_LECTIVOS: tuple[tuple[date, date], ...] = (
    (date(2025, 9, 8), date(2026, 6, 19)),    # curso 2025/2026
    (date(2026, 9, 7), date(2027, 6, 18)),    # curso 2026/2027
)

# Vacaciones dentro del curso: no son lectivas aunque caigan dentro del periodo.
_VACACIONES: tuple[tuple[date, date], ...] = (
    (date(2025, 12, 20), date(2026, 1, 7)),   # Navidad 2025/2026
    (date(2026, 3, 27), date(2026, 4, 6)),    # Semana Santa 2026
    (date(2026, 12, 19), date(2027, 1, 10)),  # Navidad 2026/2027 (aproximada)
)


def es_festivo(fecha: date) -> bool:
    """Festivo nacional, autonómico de Madrid o local del municipio de Madrid."""
    if fecha in _FESTIVOS_MADRID:
        return True
    return (fecha.month, fecha.day) in _LOCALES_MADRID_CAPITAL


def nombre_festivo(fecha: date) -> str | None:
    """Nombre del festivo, para depurar y para las notas de la memoria."""
    if fecha in _FESTIVOS_MADRID:
        return _FESTIVOS_MADRID.get(fecha)
    if (fecha.month, fecha.day) in _LOCALES_MADRID_CAPITAL:
        return "Festivo local de Madrid"
    return None


def es_periodo_lectivo(fecha: date) -> bool:
    """Cierto si ese día hay actividad escolar ordinaria.

    Un fin de semana dentro del curso SÍ cuenta como periodo lectivo: la feature mide
    el régimen de demanda de la temporada, no si ese día concreto hay clase. Para lo
    segundo ya están 'is_weekend' e 'is_holiday', que el modelo combina por su cuenta.
    """
    en_curso = any(ini <= fecha <= fin for ini, fin in _PERIODOS_LECTIVOS)
    if not en_curso:
        return False
    en_vacaciones = any(ini <= fecha <= fin for ini, fin in _VACACIONES)
    return not en_vacaciones


def features_calendario(fecha: date) -> dict[str, object]:
    """Las tres features de calendario que dependen de la fecha (no de la hora)."""
    return {
        "dow": fecha.weekday(),              # 0 = lunes
        "is_weekend": fecha.weekday() >= 5,
        "is_holiday": es_festivo(fecha),
        "is_school_period": es_periodo_lectivo(fecha),
    }


def añadir_features_calendario(df, columna_fecha: str = "service_date"):
    """Versión vectorizada para el equipo de modelado.

    Añade a un DataFrame de pandas las columnas dow, is_weekend, is_holiday e
    is_school_period a partir de una columna de fechas (date o texto 'YYYY-MM-DD').

    Uso:
        import calendario
        df = calendario.añadir_features_calendario(df, "service_date")

    pandas se importa aquí dentro a propósito: el servicio de predicción no lo necesita
    y no debe cargarlo en memoria por una función que solo usa el entrenamiento.
    """
    import pandas as pd

    fechas = pd.to_datetime(df[columna_fecha]).dt.date
    df = df.copy()
    df["dow"] = [f.weekday() for f in fechas]
    df["is_weekend"] = [f.weekday() >= 5 for f in fechas]
    df["is_holiday"] = [es_festivo(f) for f in fechas]
    df["is_school_period"] = [es_periodo_lectivo(f) for f in fechas]
    return df


if __name__ == "__main__":
    from datetime import timedelta

    print("Festivos de Madrid en la ventana de captura (13/06 - 30/09/2026):")
    d = date(2026, 6, 13)
    while d <= date(2026, 9, 30):
        if es_festivo(d):
            print(f"  {d} ({d.strftime('%A')}): {nombre_festivo(d)}")
        d += timedelta(days=1)

    print("\nFrontera del periodo lectivo:")
    for d in (date(2026, 6, 18), date(2026, 6, 19), date(2026, 6, 20),
              date(2026, 9, 6), date(2026, 9, 7), date(2026, 9, 8)):
        print(f"  {d} ({d.strftime('%a')}): lectivo = {es_periodo_lectivo(d)}")

    print("\nFeatures del día de la defensa (18/09/2026):")
    for k, v in features_calendario(date(2026, 9, 18)).items():
        print(f"  {k}: {v}")
