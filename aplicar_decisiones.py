"""
Aplica las decisiones del agente sobre los pares de la zona gris.

- Valida cada decisión: el par debe existir en pares_zona_gris.json (el agente no puede inventar ids).
- DUPLICADO con confianza >= umbral -> se fusiona (decidido_por = AGENTE).
- DUPLICADO con confianza baja, INCIERTO o pares sin decisión -> revisión humana.
- DISTINTO -> no se toca.

Modifica en el lugar clientes_actualizado.xlsx y fusiones.xlsx (los archivos que el flujo
copia después a la tabla maestra y a log_fusiones). Guarda además:
  decisiones_agente.xlsx   todas las decisiones, para auditoría
  revision_humana.xlsx     casos que debe revisar una persona
Imprime un resumen JSON en la última línea.
"""
import argparse
import json
from datetime import datetime
from pathlib import Path

import pandas as pd

import supervivencia as SV

DECISIONES_VALIDAS = {"DUPLICADO", "DISTINTO", "INCIERTO"}


def cargar_decisiones(ruta: Path) -> list[dict]:
    """Acepta [{decisiones:[...]}, ...], {decisiones:[...]} o una lista plana de decisiones."""
    datos = json.loads(ruta.read_text(encoding="utf-8"))
    if isinstance(datos, dict):
        datos = [datos]
    planas = []
    for x in datos:
        if isinstance(x, dict) and "decisiones" in x:
            planas.extend(x["decisiones"] or [])
        elif isinstance(x, dict) and "output" in x:
            planas.extend((x["output"] or {}).get("decisiones", []))
        elif isinstance(x, dict) and "id_a" in x:
            planas.append(x)
    return planas


def vacio(v) -> bool:
    return pd.isna(v) or str(v).strip() == ""


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--decisiones", required=True)
    p.add_argument("--pares", required=True)
    p.add_argument("--clientes", required=True, help="clientes_actualizado.xlsx (se modifica)")
    p.add_argument("--fusiones", required=True, help="fusiones.xlsx de esta ejecución (se modifica)")
    p.add_argument("--salida", required=True)
    p.add_argument("--umbral-confianza", type=float, default=0.8)
    p.add_argument("--id-ejecucion", default=datetime.now().strftime("%Y%m%d%H%M%S"))
    a = p.parse_args()
    out = Path(a.salida)

    pares = {tuple(sorted((x["id_a"], x["id_b"]))): x["score"]
             for x in json.loads(Path(a.pares).read_text(encoding="utf-8"))}
    decisiones = cargar_decisiones(Path(a.decisiones))

    df = pd.read_excel(a.clientes, dtype={"numero_documento": str, "telefono": str})
    df["fecha_registro"] = pd.to_datetime(df["fecha_registro"], errors="coerce")
    df = df.set_index("id_cliente", drop=False)
    freq = SV.frecuencias(df)
    fusiones = pd.read_excel(a.fusiones)

    auditoria, revision, nuevas_fusiones = [], [], []
    vistos, invalidas = set(), 0

    for d in decisiones:
        try:
            par = tuple(sorted((int(d["id_a"]), int(d["id_b"]))))
            decision = str(d["decision"]).strip().upper()
            confianza = float(d.get("confianza", 0))
        except (KeyError, TypeError, ValueError):
            invalidas += 1
            continue
        if par not in pares or decision not in DECISIONES_VALIDAS or par in vistos:
            invalidas += 1
            continue
        vistos.add(par)
        just = str(d.get("justificacion", "")).strip()
        fila = {"id_ejecucion": a.id_ejecucion, "id_a": par[0], "id_b": par[1], "score_modelo": pares[par],
                "decision": decision, "confianza": confianza, "justificacion": just}

        if decision == "DUPLICADO" and confianza >= a.umbral_confianza:
            fila["accion"] = "FUSIONADO"
            ma, mb = df.at[par[0], "id_maestro"], df.at[par[1], "id_maestro"]
            if ma != mb:
                grupo = list(df[df["id_maestro"].isin([ma, mb])].index)
                antes = {i: df.at[i, "id_maestro"] for i in grupo}
                nuevo = SV.aplicar_grupo(df, grupo, freq)
                for i in grupo:
                    if i != nuevo and antes[i] != nuevo:
                        nuevas_fusiones.append({"id_ejecucion": a.id_ejecucion, "id_origen": int(i),
                                                "id_maestro": int(nuevo), "score": pares[par],
                                                "decidido_por": "AGENTE",
                                                "justificacion": f"[confianza {confianza:.2f}] {just}"})
        elif decision == "DISTINTO":
            fila["accion"] = "SIN_CAMBIOS"
        else:
            fila["accion"] = "REVISION_HUMANA"
            revision.append(fila)
        auditoria.append(fila)

    # Pares que el agente no decidió también van a revisión humana
    for par, score in pares.items():
        if par not in vistos:
            fila = {"id_ejecucion": a.id_ejecucion, "id_a": par[0], "id_b": par[1], "score_modelo": score,
                    "decision": "SIN_DECISION", "confianza": None, "justificacion": "", "accion": "REVISION_HUMANA"}
            revision.append(fila)
            auditoria.append(fila)

    # Las fusiones del modelo cuyo maestro cambió deben apuntar al maestro nuevo
    if len(fusiones):
        fusiones["id_maestro"] = fusiones["id_origen"].map(df["id_maestro"]).fillna(fusiones["id_maestro"]).astype(int)
    fusiones = pd.concat([fusiones, pd.DataFrame(nuevas_fusiones)], ignore_index=True)

    df.reset_index(drop=True).to_excel(a.clientes, index=False)
    fusiones.to_excel(a.fusiones, index=False)
    cols = ["id_ejecucion", "id_a", "id_b", "score_modelo", "decision", "confianza", "justificacion", "accion"]
    pd.DataFrame(auditoria, columns=cols).to_excel(out / "decisiones_agente.xlsx", index=False)
    pd.DataFrame(revision, columns=cols).to_excel(out / "revision_humana.xlsx", index=False)

    resumen = {
        "pares_zona_gris": len(pares), "decisiones_recibidas": len(decisiones), "decisiones_invalidas": invalidas,
        "duplicados_agente": len(nuevas_fusiones),
        "distintos": sum(f["decision"] == "DISTINTO" for f in auditoria),
        "revision_humana": len(revision), "umbral_confianza": a.umbral_confianza,
    }
    print(json.dumps(resumen, ensure_ascii=True))


if __name__ == "__main__":
    main()
