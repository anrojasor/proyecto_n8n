"""
Evalúa el resultado final del flujo contra las etiquetas reales (solo para pruebas con datos sintéticos).

Uso:
  python evaluar.py --clientes datos/clientes.xlsx --verdad datos/verdad.xlsx ^
                    --fusiones datos/log_fusiones.xlsx --decisiones salida/decisiones_agente.xlsx ^
                    --salida salida/evaluacion.xlsx

Imprime un resumen y guarda un Excel con:
  resumen          precisión, recall y F1 globales (por pares) y por actor
  por_tipo         cada tipo de caso: cuántos resolvió el modelo, el agente o quedaron pendientes
  agente           matriz de confusión del agente (decisión vs verdad) y calibración por confianza
  errores          pares mal resueltos, para revisarlos uno a uno
  registro_dorado  calidad del nombre que sobrevivió en el maestro
"""
import argparse
import json
import unicodedata
from itertools import combinations

import pandas as pd


def norm(s):
    if pd.isna(s):
        return ""
    s = unicodedata.normalize("NFD", str(s).upper())
    return " ".join("".join(c for c in s if unicodedata.category(c) != "Mn").split())


def pares_por(df, col):
    s = set()
    for _, g in df.groupby(col):
        s.update(combinations(sorted(g["id_cliente"]), 2))
    return s


def metricas(pred, reales):
    tp = len(pred & reales)
    p = tp / len(pred) if pred else 1.0
    r = tp / len(reales) if reales else 1.0
    return {"precision": round(p, 4), "recall": round(r, 4),
            "f1": round(2 * p * r / (p + r), 4) if p + r else 0,
            "verdaderos_positivos": tp, "falsos_positivos": len(pred - reales),
            "falsos_negativos": len(reales - pred)}


def main():
    a = argparse.ArgumentParser()
    a.add_argument("--clientes", required=True, help="tabla maestra final (después del flujo)")
    a.add_argument("--verdad", required=True)
    a.add_argument("--fusiones", help="log_fusiones.xlsx")
    a.add_argument("--decisiones", help="decisiones_agente.xlsx")
    a.add_argument("--salida", default="evaluacion.xlsx")
    args = a.parse_args()

    c = pd.read_excel(args.clientes, dtype={"numero_documento": str})
    v = pd.read_excel(args.verdad)
    if "dificultad" not in v:
        v["dificultad"] = ""
    m = c.merge(v, on="id_cliente")
    caso = v.set_index("id_cliente")["tipo_caso"]
    persona = v.set_index("id_cliente")["id_persona_real"]

    reales = pares_por(m, "id_persona_real")
    pred = pares_por(m, "id_maestro")
    hojas = {}
    resumen = {"global": metricas(pred, reales)}

    # ---- por actor (modelo / agente), según log_fusiones
    if args.fusiones:
        f = pd.read_excel(args.fusiones).dropna(subset=["id_origen"])
        f = f.drop_duplicates(subset=["id_origen"], keep="last")
        for actor, g in f.groupby("decidido_por"):
            ok = sum(persona[o] == persona[mm] for o, mm in zip(g.id_origen.astype(int), g.id_maestro.astype(int)))
            resumen[f"fusiones_{actor.lower()}"] = {"total": len(g), "correctas": int(ok), "incorrectas": len(g) - int(ok)}

    # ---- por tipo de caso
    filas = []
    tipos_dup = [t for t in caso.unique() if t != "ORIGINAL" and not t.startswith("DISTINTO")]
    actor_de = {}
    if args.fusiones:
        for o, mm, act in zip(f.id_origen.astype(int), f.id_maestro.astype(int), f.decidido_por):
            actor_de[o] = act
    for t in sorted(tipos_dup):
        ids = m[m.tipo_caso == t]
        filas_t = {"tipo_caso": t, "dificultad": ids.dificultad.iloc[0] if len(ids) else "", "casos": len(ids),
                   "resueltos": 0, "por_modelo": 0, "por_agente": 0, "no_resueltos": 0}
        for _, r in ids.iterrows():
            gemelo = m[(m.id_persona_real == r.id_persona_real) & (m.id_cliente != r.id_cliente)]
            if gemelo.empty:
                continue
            g = gemelo.iloc[0]
            if r.id_maestro == g.id_maestro:
                filas_t["resueltos"] += 1
                act = actor_de.get(r.id_cliente) or actor_de.get(g.id_cliente)
                filas_t["por_agente" if act == "AGENTE" else "por_modelo"] += 1
            else:
                filas_t["no_resueltos"] += 1
        filas.append(filas_t)
    for t in sorted(x for x in caso.unique() if x.startswith("DISTINTO")):
        ids = m[m.tipo_caso == t]
        mal = 0
        for _, r in ids.iterrows():
            grupo = m[m.id_maestro == r.id_maestro]
            mal += int(grupo.id_persona_real.nunique() > 1)
        filas.append({"tipo_caso": t, "dificultad": ids.dificultad.iloc[0] if len(ids) else "", "casos": len(ids),
                      "resueltos": len(ids) - mal, "por_modelo": None, "por_agente": None,
                      "no_resueltos": None, "fusionados_por_error": mal})
    hojas["por_tipo"] = pd.DataFrame(filas)

    # ---- agente: matriz de confusión y calibración
    if args.decisiones:
        d = pd.read_excel(args.decisiones)
        d["verdad"] = ["DUPLICADO" if persona.get(x) == persona.get(y) else "DISTINTO"
                       for x, y in zip(d.id_a, d.id_b)]
        d["tipo_caso"] = [caso[x] if caso[x] != "ORIGINAL" else caso[y] for x, y in zip(d.id_a, d.id_b)]
        d["correcta"] = d.decision == d.verdad
        mat = pd.crosstab(d.verdad, d.decision, margins=True, margins_name="Total")
        decididas = d[d.decision.isin(["DUPLICADO", "DISTINTO"])]
        resumen["agente"] = {
            "pares_evaluados": len(d),
            "decididos": len(decididos := decididas),
            "aciertos": int(decididos.correcta.sum()),
            "exactitud_en_decididos": round(decididos.correcta.mean(), 4) if len(decididos) else None,
            "incierto_o_sin_decision": int((~d.decision.isin(["DUPLICADO", "DISTINTO"])).sum()),
        }
        d["rango_confianza"] = pd.cut(d.confianza, [0, 0.6, 0.8, 0.9, 1.0], include_lowest=True).astype(str)
        calib = decididas.assign(rango_confianza=d.loc[decididas.index, "rango_confianza"]) \
            .groupby("rango_confianza").correcta.agg(["count", "mean"]).rename(
                columns={"count": "decisiones", "mean": "exactitud"}).reset_index()
        por_tipo_ag = pd.crosstab(d.tipo_caso, d.decision)
        hojas["agente_matriz"] = mat.reset_index()
        hojas["agente_por_tipo"] = por_tipo_ag.reset_index()
        hojas["agente_calibracion"] = calib
        hojas["errores_agente"] = d[(~d.correcta) & d.decision.isin(["DUPLICADO", "DISTINTO"])][
            ["id_a", "id_b", "tipo_caso", "decision", "verdad", "confianza", "justificacion"]]
        print("\nAgente - decisión vs verdad:\n" + mat.to_string())
        print("\nAgente - decisiones por tipo de caso:\n" + por_tipo_ag.to_string())

    # ---- errores globales
    err = [{"id_a": x, "id_b": y, "error": "FALSO_POSITIVO", "tipo_a": caso[x], "tipo_b": caso[y]} for x, y in pred - reales]
    err += [{"id_a": x, "id_b": y, "error": "FALSO_NEGATIVO", "tipo_a": caso[x], "tipo_b": caso[y]} for x, y in reales - pred]
    hojas["errores"] = pd.DataFrame(err, columns=["id_a", "id_b", "error", "tipo_a", "tipo_b"])

    # ---- calidad del registro dorado: ¿el maestro conserva el nombre correcto?
    maestros = m[(m.estado_registro == "ACTIVO") & m.id_maestro.isin(m[m.estado_registro == "FUSIONADO"].id_maestro)]
    originales = m[m.tipo_caso == "ORIGINAL"].set_index("id_persona_real")
    ok = total = 0
    for _, r in maestros.iterrows():
        if r.id_persona_real in originales.index:
            o = originales.loc[r.id_persona_real]
            total += 1
            ok += norm(r.primer_nombre) == norm(o.primer_nombre) and norm(r.primer_apellido) == norm(o.primer_apellido)
    resumen["registro_dorado"] = {"maestros_evaluados": total, "nombre_correcto": int(ok),
                                  "porcentaje": round(ok / total, 4) if total else None}

    hojas = {"resumen": pd.DataFrame([{"metrica": k, **(v if isinstance(v, dict) else {"valor": v})}
                                      for k, v in resumen.items()]), **hojas}
    with pd.ExcelWriter(args.salida) as w:
        for nombre, h in hojas.items():
            h.to_excel(w, sheet_name=nombre[:31], index=False)

    print("\nPor tipo de caso:\n" + hojas["por_tipo"].to_string(index=False))
    print("\n" + json.dumps(resumen, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
