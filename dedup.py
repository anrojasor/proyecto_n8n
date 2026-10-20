"""
Motor de deduplicación de clientes (entity resolution).

Pipeline: normalización -> blocking -> comparación por par -> scoring
          -> union-find -> registro maestro -> salidas.

Uso:
  python dedup.py --entrada datos/clientes.xlsx --salida salida
  python dedup.py --entrada datos/clientes.xlsx --salida salida --verdad datos/verdad.xlsx

Salidas (en --salida):
  clientes_actualizado.xlsx  tabla maestra con id_maestro y estado_registro actualizados
  fusiones.xlsx              fusiones automáticas (para log_fusiones)
  pares_zona_gris.json       pares dudosos para el agente
  metricas.json              resumen de la ejecución (para log_ejecuciones)
El resumen también se imprime como JSON en la última línea (n8n puede leerlo).
"""
import argparse
import json
import time
import unicodedata
from collections import defaultdict
from datetime import datetime
from itertools import combinations
from pathlib import Path

import pandas as pd

import supervivencia as SV
from rapidfuzz import fuzz
from rapidfuzz.distance import JaroWinkler, Levenshtein

VERSION_MODELO = "reglas-v2"
CAMPOS_NOMBRE = ["primer_nombre", "segundo_nombre", "primer_apellido", "segundo_apellido"]
MAX_BLOQUE = 200  # bloques más grandes se ignoran (llaves poco informativas)

PESOS = {"nombre": 0.35, "fecha": 0.25, "documento": 0.25,
         "telefono": 0.05, "correo": 0.05, "direccion": 0.05}


# ------------------------------------------------------------ 1. normalización
def norm_texto(s) -> str:
    if pd.isna(s):
        return ""
    s = unicodedata.normalize("NFD", str(s).upper())
    s = "".join(c for c in s if unicodedata.category(c) != "Mn")
    return " ".join(s.split())


def solo_digitos(s) -> str:
    return "" if pd.isna(s) else "".join(c for c in str(s) if c.isdigit())


def normalizar(df: pd.DataFrame) -> pd.DataFrame:
    n = pd.DataFrame(index=df.index)
    n["id_cliente"] = df["id_cliente"]
    n["tipo_doc"] = df["tipo_documento"].map(norm_texto)
    n["doc"] = df["numero_documento"].map(solo_digitos)
    for c in CAMPOS_NOMBRE:
        n[c] = df[c].map(norm_texto)
    n["nombre"] = n[CAMPOS_NOMBRE].agg(" ".join, axis=1).str.split().str.join(" ")
    n["fecha_nac"] = pd.to_datetime(df["fecha_nacimiento"], errors="coerce").dt.strftime("%Y-%m-%d").fillna("")
    n["fecha_reg"] = pd.to_datetime(df["fecha_registro"], errors="coerce")
    n["tel"] = df["telefono"].map(solo_digitos)
    n["correo"] = df["correo"].fillna("").astype(str).str.strip().str.lower()
    n["dir"] = df["direccion"].map(norm_texto)
    return n


# ------------------------------------------------------------ 2. blocking
def llaves_bloque(n: pd.DataFrame) -> dict:
    return {
        "documento": n["doc"],
        "fecha_nacimiento": n["fecha_nac"],
        "telefono": n["tel"],
        "nombre3_apellido3": n["primer_nombre"].str[:3] + "|" + n["primer_apellido"].str[:3],
    }


def pares_candidatos(n: pd.DataFrame) -> set:
    pares = set()
    for _, llave in llaves_bloque(n).items():
        grupos = defaultdict(list)
        for idx, k in llave.items():
            if k and k != "|":
                grupos[k].append(idx)
        for miembros in grupos.values():
            if 1 < len(miembros) <= MAX_BLOQUE:
                pares.update(combinations(sorted(miembros), 2))
    return pares


# ------------------------------------------------------------ 3. comparación
def comparar(a, b) -> dict:
    nombre = max(fuzz.token_sort_ratio(a.nombre, b.nombre),
                 0.95 * fuzz.token_set_ratio(a.nombre, b.nombre)) / 100
    if a.doc and a.doc == b.doc:
        doc = 1.0
    elif a.doc and b.doc and len(a.doc) == len(b.doc) and Levenshtein.distance(a.doc, b.doc) == 1:
        doc = 0.8
    else:
        doc = 0.0
    return {
        "sim_nombre": round(nombre, 3),
        "sim_apellido": round(JaroWinkler.similarity(a.primer_apellido, b.primer_apellido), 3),
        "doc": doc,
        "fecha_igual": float(bool(a.fecha_nac) and a.fecha_nac == b.fecha_nac),
        "fecha_faltante": float(not a.fecha_nac or not b.fecha_nac),
        "tel_igual": float(bool(a.tel) and a.tel == b.tel),
        "correo_igual": float(bool(a.correo) and a.correo == b.correo),
        "sim_dir": round(fuzz.token_sort_ratio(a.dir, b.dir) / 100, 3),
        "ti_cc": float({a.tipo_doc, b.tipo_doc} == {"TI", "CC"}),
    }


# ------------------------------------------------------------ 4. scoring
def puntuar(f: dict) -> tuple[float, str]:
    """Devuelve (score, regla aplicada)."""
    # Regla de dominio NUIP: mismo número, paso de TI a CC, misma fecha y nombre similar
    if f["ti_cc"] and f["doc"] == 1.0 and f["fecha_igual"] and f["sim_nombre"] >= 0.85:
        return 1.0, "REGLA_NUIP_TI_CC"
    s = (PESOS["nombre"] * f["sim_nombre"] + PESOS["fecha"] * f["fecha_igual"]
         + PESOS["documento"] * f["doc"] + PESOS["telefono"] * f["tel_igual"]
         + PESOS["correo"] * f["correo_igual"]
         + PESOS["direccion"] * (f["sim_dir"] if f["sim_dir"] >= 0.9 else 0))
    return round(s, 3), "PONDERADO"


def justificar(f: dict, regla: str) -> str:
    partes = []
    if regla == "REGLA_NUIP_TI_CC":
        partes.append("TI→CC con el mismo número (NUIP)")
    partes.append({1.0: "documento igual", 0.8: "documento con un dígito distinto", 0.0: "documento distinto"}[f["doc"]])
    partes.append("misma fecha de nacimiento" if f["fecha_igual"] else
                  "falta la fecha de nacimiento" if f.get("fecha_faltante") else "fecha de nacimiento distinta")
    partes.append(f"similitud de nombre {f['sim_nombre']:.2f}")
    if f["tel_igual"]:
        partes.append("mismo teléfono")
    if f["correo_igual"]:
        partes.append("mismo correo")
    return "; ".join(partes)


# ------------------------------------------------------------ 5. union-find
class UnionFind:
    def __init__(self):
        self.padre = {}

    def find(self, x):
        self.padre.setdefault(x, x)
        while self.padre[x] != x:
            self.padre[x] = self.padre[self.padre[x]]
            x = self.padre[x]
        return x

    def union(self, a, b):
        self.padre[self.find(a)] = self.find(b)

    def grupos(self):
        g = defaultdict(list)
        for x in self.padre:
            g[self.find(x)].append(x)
        return [m for m in g.values() if len(m) > 1]


# ------------------------------------------------------------ evaluación
def evaluar(df, auto_pares, gris_pares, verdad_path):
    v = pd.read_excel(verdad_path).set_index("id_cliente")
    persona = df["id_cliente"].map(v["id_persona_real"])
    caso = df["id_cliente"].map(v["tipo_caso"])

    reales = set()
    for _, idxs in persona.groupby(persona).groups.items():
        reales.update(combinations(sorted(idxs), 2))

    def pr(pred):
        tp = len(pred & reales)
        return {"precision": round(tp / len(pred), 4) if pred else None,
                "recall": round(tp / len(reales), 4) if reales else None,
                "verdaderos_positivos": tp, "falsos_positivos": len(pred) - tp}

    por_tipo = {}
    for tipo in sorted(caso[~caso.isin(["ORIGINAL"]) & ~caso.str.startswith("DISTINTO")].unique()):
        idxs = caso[caso == tipo].index
        objetivo = [tuple(sorted(p)) for p in reales if p[0] in idxs or p[1] in idxs]
        por_tipo[tipo] = {"total": len(objetivo),
                          "auto": sum(p in auto_pares for p in objetivo),
                          "zona_gris": sum(p in gris_pares for p in objetivo)}
    trampas = {}
    for tipo in sorted(caso[caso.str.startswith("DISTINTO")].unique()):
        idxs = set(caso[caso == tipo].index)
        trampas[tipo] = {"fusionados_por_error": sum(1 for p in auto_pares - reales if idxs & set(p)),
                         "en_zona_gris": sum(1 for p in gris_pares - reales if idxs & set(p))}
    return {"solo_auto": pr(auto_pares), "auto_mas_gris": pr(auto_pares | gris_pares),
            "pares_reales": len(reales), "recall_por_tipo": por_tipo, "casos_trampa": trampas}


# ------------------------------------------------------------ principal
def main():
    p = argparse.ArgumentParser()
    p.add_argument("--entrada", default="datos/clientes.xlsx")
    p.add_argument("--salida", default="salida")
    p.add_argument("--verdad", help="verdad.xlsx para medir precisión y recall (solo pruebas)")
    p.add_argument("--umbral-alto", type=float, default=0.85)
    p.add_argument("--umbral-bajo", type=float, default=0.55)
    p.add_argument("--id-ejecucion", default=datetime.now().strftime("%Y%m%d%H%M%S"))
    a = p.parse_args()

    t0 = time.time()
    out = Path(a.salida)
    out.mkdir(parents=True, exist_ok=True)

    df = pd.read_excel(a.entrada, dtype={"numero_documento": str, "telefono": str})
    if "estado_registro" not in df:
        df["estado_registro"] = "ACTIVO"
    df["estado_registro"] = df["estado_registro"].fillna("ACTIVO")
    if "id_maestro" not in df:
        df["id_maestro"] = pd.NA
    activos = df[df["estado_registro"] == "ACTIVO"]

    n = normalizar(activos)
    candidatos = pares_candidatos(n)

    uf, auto_pares, gris_pares = UnionFind(), set(), set()
    zona_gris, puntajes = [], {}
    cols_mostrar = ["id_cliente", "tipo_documento", "numero_documento", *CAMPOS_NOMBRE,
                    "fecha_nacimiento", "telefono", "correo", "direccion", "ciudad", "fecha_registro"]
    for i, j in candidatos:
        f = comparar(n.loc[i], n.loc[j])
        score, regla = puntuar(f)
        if score >= a.umbral_alto:
            uf.union(i, j)
            auto_pares.add((i, j))
            puntajes[(i, j)] = (score, justificar(f, regla))
        elif score >= a.umbral_bajo:
            gris_pares.add((i, j))
            zona_gris.append({
                "id_a": int(n.at[i, "id_cliente"]), "id_b": int(n.at[j, "id_cliente"]),
                "score": score, "caracteristicas": f,
                "registro_a": json.loads(activos.loc[i, cols_mostrar].to_json(date_format="iso")),
                "registro_b": json.loads(activos.loc[j, cols_mostrar].to_json(date_format="iso")),
            })

    # Clusters y registro maestro (registro dorado: mejor valor de cada campo)
    fusiones = []
    freq = SV.frecuencias(activos)
    for miembros in uf.grupos():
        m = SV.aplicar_grupo(df, miembros, freq)
        id_m = int(df.at[m, "id_cliente"])
        for i in miembros:
            if i == m:
                continue
            par = tuple(sorted((i, m)))
            score, just = puntajes.get(par, (None, "unión transitiva dentro del cluster"))
            fusiones.append({"id_ejecucion": a.id_ejecucion, "id_origen": int(df.at[i, "id_cliente"]),
                             "id_maestro": id_m, "score": score, "decidido_por": "MODELO",
                             "justificacion": just})
    df["id_maestro"] = df["id_maestro"].fillna(df["id_cliente"]).astype(int)

    # Salidas
    df.to_excel(out / "clientes_actualizado.xlsx", index=False)
    pd.DataFrame(fusiones, columns=["id_ejecucion", "id_origen", "id_maestro", "score",
                                    "decidido_por", "justificacion"]).to_excel(out / "fusiones.xlsx", index=False)
    (out / "pares_zona_gris.json").write_text(json.dumps(zona_gris, ensure_ascii=False, indent=2), encoding="utf-8")

    metricas = {
        "id_ejecucion": a.id_ejecucion, "fecha": datetime.now().isoformat(timespec="seconds"),
        "registros_procesados": len(activos), "pares_evaluados": len(candidatos),
        "duplicados_auto": len(fusiones), "pares_zona_gris": len(zona_gris),
        "grupos_modelo": len(uf.grupos()), "umbral_alto": a.umbral_alto, "umbral_bajo": a.umbral_bajo,
        "duracion_seg": round(time.time() - t0, 2), "version_modelo": VERSION_MODELO,
    }
    if a.verdad:
        metricas["evaluacion"] = evaluar(activos, auto_pares, gris_pares, a.verdad)
    (out / "metricas.json").write_text(json.dumps(metricas, ensure_ascii=False, indent=2), encoding="utf-8")
    # ensure_ascii evita errores de codificación al leer stdout desde n8n en Windows
    print(json.dumps({k: v for k, v in metricas.items() if k != "evaluacion"}, ensure_ascii=True))


if __name__ == "__main__":
    main()
