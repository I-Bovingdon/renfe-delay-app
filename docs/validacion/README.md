# Validación en producción

Material del Anexo I de la memoria: contraste de las predicciones que sirvió la
aplicación con la última estimación de llegada publicada por Renfe.

| Fichero | Contenido |
|---|---|
| `verificacion.csv` | 613 predicciones del 13/09/2026 con su valor de referencia. Lo genera `tests/verificar_predicciones.py` |
| `medicion_rumbo.csv` | Medición del rumbo de los trenes frente a su desplazamiento real. La genera `scripts/medir_rumbo.py` |
| `validacion_anexo.ipynb` | Figuras `f1` a `f5` y comprobación de las cifras del anexo |
| `validacion_graficas.ipynb` | Figuras `figuras/01` a `figuras/09` |
| `resumen_validacion.py` | Resumen de un CSV de verificación frente a las reglas constantes |

Reproducción, desde esta carpeta:

```bash
jupyter nbconvert --to notebook --execute --inplace validacion_anexo.ipynb
jupyter nbconvert --to notebook --execute --inplace validacion_graficas.ipynb
python resumen_validacion.py verificacion.csv "13/09"
```

Ninguna cifra de las figuras está escrita a mano: todas se calculan desde los CSV.
