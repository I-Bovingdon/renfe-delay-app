"""
adaptador_modelo.py — Traduce del contrato de la interfaz al modelo entrenado.

El modelo (`modelo_retrasos_v2.pkl`, LightGBM) fue entrenado sobre las columnas del
pipeline de modelado, cuyos nombres y tipos NO coinciden con los del contrato de la
interfaz. Este módulo es la única pieza que conoce esa correspondencia.

Por qué existe un adaptador en lugar de renombrar las columnas del contrato: el
contrato es el acuerdo con el equipo de modelado y cambiará cuando se reentrene tras
corregir la fuga de contexto. Concentrar la traducción en un fichero permite que el
siguiente modelo se integre tocando solo este módulo.

TRES TRAMPAS DETECTADAS AL INSPECCIONAR EL MODELO, y resueltas aquí:

  1. `trip_day_of_week` NO es un entero 0-6: son nombres de día en español, en
     minúsculas y con acentos ('miércoles', 'sábado'). Pasar un número haría que
     LightGBM lo tratase como categoría desconocida, sin dar ningún error.
  2. El modelo conoce 11 líneas: la C9 se eliminó del entrenamiento por falta de
     muestra. El catálogo GTFS tiene 12. Las consultas de la C9 se marcan de forma
     explícita en lugar de predecir sobre una categoría que el modelo no vio.
  3. LightGBM codifica las categóricas POR POSICIÓN. Si el orden de categorías no
     coincide con el del entrenamiento, la predicción sale silenciosamente mal. Las
     categorías se extraen del propio modelo (`booster_.pandas_categorical`), no se
     escriben a mano.

TFM Cercanías RENFE · UCM · 2026
"""

from __future__ import annotations

import logging
import pickle
from datetime import datetime
from pathlib import Path
from typing import Any

from tiempo import MADRID, desde_iso

log = logging.getLogger(__name__)

# Nombres de día tal como los espera el modelo. El orden de esta lista es
# irrelevante: lo que importa es que el texto coincida exactamente.
DIAS_ES = ["lunes", "martes", "miércoles", "jueves", "viernes", "sábado", "domingo"]

# Líneas ausentes del entrenamiento. Se completa al cargar el modelo.
LINEAS_DESCONOCIDAS: set[str] = set()


class ModeloRetrasos:
    """Carga el modelo entrenado y predice a partir de filas del contrato."""

    def __init__(self, ruta: str | Path):
        with open(ruta, "rb") as f:
            paquete = pickle.load(f)

        self.modelo = paquete["modelo"]
        self.columnas: list[str] = list(paquete["columnas_x"])
        self.categoricas: list[str] = list(paquete["columnas_categoricas"])
        self.mae_test: float | None = paquete.get("mae_test_estimado")
        self.entrenado: str | None = paquete.get("fecha_entrenamiento")
        self.version = f"{paquete.get('tipo_modelo', 'modelo')}-{self.entrenado}"

        # --- Categorías exactas del entrenamiento, extraídas del propio modelo ---
        # `pandas_categorical` viene en el orden en que las columnas categóricas
        # aparecen en la matriz de entrenamiento, no en el de `columnas_categoricas`.
        cats = getattr(self.modelo.booster_, "pandas_categorical", None)
        orden = [c for c in self.columnas if c in self.categoricas]
        if cats is None or len(cats) != len(orden):
            raise ValueError(
                "No se han podido recuperar las categorías del modelo. Sin ellas, "
                "LightGBM codificaría las categóricas en un orden distinto al del "
                "entrenamiento y las predicciones serían erróneas sin avisar."
            )
        self.categorias: dict[str, list[str]] = dict(zip(orden, cats))

        global LINEAS_DESCONOCIDAS
        self.lineas_conocidas = set(self.categorias.get("linea", []))

        log.info(
            "Modelo cargado: %s · %d variables · MAE de prueba %.1f s · %d líneas",
            self.version, len(self.columnas), self.mae_test or -1,
            len(self.lineas_conocidas),
        )

    # ------------------------------------------------------------------ mapeo ---
    def _fila_modelo(self, f: dict[str, Any]) -> tuple[dict[str, Any], list[str]]:
        """Traduce una fila del contrato a las 25 columnas del modelo.

        Devuelve también la lista de columnas que van nulas, para poder informarlo.
        La regla es estricta: **si una magnitud del contrato no significa exactamente
        lo mismo que la del entrenamiento, se envía nula**. LightGBM trata los nulos
        de forma nativa; alimentarlo con un número de semántica distinta sería
        train/serve skew silencioso, que es peor que la ausencia del dato.
        """
        llegada = desde_iso(f["sched_arrival_utc"]).astimezone(MADRID)
        service_date = datetime.fromisoformat(f["service_date"]).date()

        moving = 1 if f["regime"] == "A" else 0

        # CORRECCIÓN F7. El pipeline de entrenamiento fuerza a CERO, no a nulo, el
        # retraso propio cuando moving=0 (cercanias_pipeline.py: filas.loc[filas
        # ["moving"] == 0, "own_train_delay_so_far_s"] = 0). El modelo nunca vio la
        # combinación (moving=0, retraso propio nulo), y el régimen B es la mayoría de
        # las consultas reales: el pasajero pregunta ANTES de que salga el tren.
        # Enviar nulo aquí era train/serve skew silencioso en el caso más frecuente.
        retraso_propio = f.get("own_delay_s") if moving else 0.0

        fila: dict[str, Any] = {
            # --- Correspondencias directas ---
            "linea": f["line_id"],
            "horizon_min": f["horizon_s"] / 60.0,
            "moving": moving,
            "own_train_delay_so_far_s": retraso_propio,
            "line_delay_mean_30m_s": f.get("line_delay_mean_30m_s"),
            "num_alertas_t0": f.get("alerts_active_line"),
            "temp_aire_c_t0": f.get("temp_c"),
            # Con la latencia de AEMET este valor detecta la lluvia que ya cayó, no
            # la que está cayendo. El entrenamiento usó exactamente el mismo dato
            # retrasado (merge_asof backward sobre captura_ts), así que enviarlo es
            # lo correcto y enviar un nulo sería el skew. Ver la cabecera de meteo.py.
            "precip_mm_t0": f.get("precip_mm_1h"),
            "stop_sequence": f["dest_stop_sequence"],
            "trip_total_stops": f["trip_total_stops"],

            # --- Derivadas, reproduciendo el cálculo del entrenamiento ---
            # hora_dia = hora de la llegada teórica; trip_day_of_week = nombre del día
            # de la FECHA DE SERVICIO, no del instante de consulta.
            "hora_dia": llegada.hour,
            "trip_day_of_week": DIAS_ES[service_date.weekday()],

            # --- Semántica distinta: se envían nulas a propósito ---
            # n_capturas_line_30m cuenta, en el pipeline de entrenamiento, las PARADAS
            # COMPLETADAS de la línea en la ventana (rolling .count() sobre eventos
            # resueltos, no sobre capturas crudas: el nombre de la columna es
            # heredado y engañoso). En servicio solo se pueden contar trenes distintos
            # presentes en el feed, que es un orden de magnitud menor. Un conteo con
            # la escala equivocada es peor que un nulo.
            "n_capturas_line_30m": None,
            # CORRECCIÓN F8. El modelo se entrenó con la velocidad MEDIA del viento
            # (raw 'vv' -> viento_vel_ms), no con la racha. El contrato v1.1.0 ya
            # envía la magnitud correcta en wind_speed_ms, y FuenteMeteo aplica el
            # mismo fillna(0) que el pipeline de entrenamiento cuando falta.
            "viento_vel_ms_t0": f.get("wind_speed_ms"),

            # --- Aún no disponibles en la interfaz ---
            # Los desgloses de alerta por tipo llegarán con la pantalla de alertas,
            # que aplica la misma clasificación por expresiones regulares.
            "alerta_SUPRESION_t0": None,
            "alerta_AVERIA_t0": None,
            "alerta_OBRAS_t0": None,
            "alerta_RETRASO_t0": None,
            "alerta_SERVICIO_BUS_t0": None,

            # El calendario de eventos no está integrado en el servicio.
            "num_eventos": None,
            "evento_pequeño": None,
            "evento_mediano": None,
            "evento_Arts_Theatre": None,
            "evento_Miscellaneous": None,
            "evento_Music": None,
            "evento_Sports": None,
            "evento_Undefined": None,
            "evento_grande": None,
        }

        # Comprobación de contrato inverso: que no falte ni sobre ninguna columna.
        faltan = [c for c in self.columnas if c not in fila]
        if faltan:
            raise ValueError(f"El adaptador no cubre estas columnas del modelo: {faltan}")

        nulas = [c for c in self.columnas if fila.get(c) is None]
        return fila, nulas

    # -------------------------------------------------------------- predicción ---
    def predict(self, filas: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """Predice el retraso en segundos para un lote de filas del contrato."""
        import pandas as pd

        mapeadas, nulas_por_fila, avisos = [], [], []
        for f in filas:
            fila, nulas = self._fila_modelo(f)
            if fila["linea"] not in self.lineas_conocidas:
                # La C9 no estaba en el entrenamiento. Se deja la categoría como nula
                # en lugar de inventar una: el modelo predice con el resto del
                # contexto y la interfaz puede avisar de que la línea es atípica.
                avisos.append(f"linea_no_entrenada:{fila['linea']}")
                fila["linea"] = None
            mapeadas.append(fila)
            nulas_por_fila.append(nulas)

        X = pd.DataFrame(mapeadas, columns=self.columnas)

        # Las categóricas se construyen con las categorías EXACTAS del entrenamiento.
        for col, categorias in self.categorias.items():
            X[col] = pd.Categorical(X[col], categories=categorias)

        # El resto, numérico. `errors="coerce"` convierte cualquier resto a NaN, que
        # es lo que LightGBM espera para un valor ausente.
        for col in self.columnas:
            if col not in self.categorias:
                X[col] = pd.to_numeric(X[col], errors="coerce")

        predicciones = self.modelo.booster_.predict(X)

        salida = []
        for valor, nulas in zip(predicciones, nulas_por_fila):
            v = float(valor)
            salida.append(
                {
                    "delay_s_p50": round(v, 1),
                    # El modelo es puntual: no produce intervalo. Se devuelven los
                    # tres percentiles iguales con la bandera a falso, y la interfaz
                    # oculta la banda de incertidumbre sin necesitar cambios.
                    "delay_s_p10": round(v, 1),
                    "delay_s_p90": round(v, 1),
                    "has_interval": False,
                    "model_version": self.version,
                    "columnas_nulas": nulas,
                    "avisos": avisos,
                }
            )
        return salida


if __name__ == "__main__":
    import sys

    from contrato import empty_row

    ruta = sys.argv[1] if len(sys.argv) > 1 else "/home/tfm/modelo/modelo_retrasos_v2.pkl"
    m = ModeloRetrasos(ruta)

    print(f"\nVariables del modelo ({len(m.columnas)}):")
    for c in m.columnas:
        marca = "  [categórica]" if c in m.categorias else ""
        print(f"  - {c}{marca}")
    print(f"\nCategorías:")
    for k, v in m.categorias.items():
        print(f"  {k}: {v}")

    fila = empty_row()
    fila.update(
        {
            "trip_id": "1037J79324C7", "service_date": "2026-09-18",
            "t0_utc": "2026-09-18T06:30:00Z", "sched_arrival_utc": "2026-09-18T07:12:00Z",
            "gtfs_version": "16e2776144a4", "line_id": "C7",
            "origin_stop_id": "18000", "dest_stop_id": "70103",
            "dest_stop_sequence": 12, "trip_total_stops": 12, "stops_to_dest": 11,
            "horizon_s": 2520, "dow": 4, "hour_local": 9, "minute_of_day_local": 552,
            "is_weekend": False, "is_holiday": False, "is_school_period": True,
            "regime": "B", "line_delay_mean_30m_s": 180.0, "temp_c": 22.0,
            "precip_mm_1h": 0.0, "alerts_active_line": 0,
        }
    )
    for p in m.predict([fila]):
        print(f"\nPredicción: {p['delay_s_p50']} s ({p['delay_s_p50']/60:.1f} min)")
        print(f"  modelo: {p['model_version']}")
        print(f"  columnas nulas ({len(p['columnas_nulas'])}): {p['columnas_nulas']}")
