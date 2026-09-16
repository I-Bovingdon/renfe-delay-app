import csv, statistics as st, sys
from collections import Counter

def resumen(ruta, etiqueta):
    todas = list(csv.DictReader(open(ruta, encoding="utf-8")))
    filas = [r for r in todas if r["estado"] == "ok" and r["via"] == "parada"
             and r["retraso_predicho_s"] not in ("", None)]
    real = [float(r["retraso_real_s"]) for r in filas]
    pred = [float(r["retraso_predicho_s"]) for r in filas]
    err = [abs(p - y) / 60 for p, y in zip(pred, real)]
    mae = lambda xs: st.mean(abs(x) for x in xs) / 60
    hora = [r["consultado_en_utc"] for r in filas]
    print(f"\n== {etiqueta}")
    print(f"filas del CSV {len(todas)} · estados {dict(Counter(r['estado'] for r in todas))}"
          f" · vías {dict(Counter(r['via'] for r in todas))}")
    print(f"{len(filas)} predicciones de {len({r['nucleo'] for r in filas})} trenes"
          f" · líneas {dict(sorted(Counter(r['line_id'] for r in filas).items()))}")
    print(f"régimen {dict(Counter(r['regime'] for r in filas))}"
          f" · horizonte mediano {st.median(float(r['horizonte_min']) for r in filas):.0f} min"
          f" · consultas de {min(hora)} a {max(hora)}")
    print(f"  modelo              MAE {mae([p - y for p, y in zip(pred, real)]):5.2f} min"
          f" · mediana {st.median(err):.2f} min · dentro de ±3 min {100 * sum(e <= 3 for e in err) / len(err):.0f} %")
    print(f"  siempre 4 min       MAE {mae([240 - y for y in real]):5.2f} min")
    print(f"  horario (siempre 0) MAE {mae([-y for y in real]):5.2f} min")
    print(f"  r = {st.correlation(pred, real):.3f} · dispersión real/predicha {st.stdev(real) / st.stdev(pred):.2f}x"
          f" · predicciones negativas {sum(p < 0 for p in pred)}")
    print(f"  retraso real: mediana {st.median(real) / 60:.1f} min, media {st.mean(real) / 60:.1f} min")

for ruta, etiqueta in zip(sys.argv[1::2], sys.argv[2::2]):
    resumen(ruta, etiqueta)
