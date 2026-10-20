"""
Reglas de supervivencia: construyen el "registro dorado" de cada cliente fusionado.

El registro maestro conserva su id y su documento, pero cada campo toma el mejor valor del grupo:
  - Documento: el de mayor jerarquía (CC > CE > PA > TI); en empate, el más reciente.
  - Nombres y apellidos: la variante más frecuente en TODA la tabla (un error de digitación como
    "Geroán" es raro; "Germán" es común). En empate, la que conserva tildes y minúsculas.
  - Fecha de nacimiento: la del registro con documento de mayor jerarquía; si falta, la de otro registro.
  - Teléfono, correo y dirección: el valor no vacío más reciente.
Los registros fusionados no se modifican, para conservar la trazabilidad.
"""
import unicodedata
from collections import Counter

import pandas as pd

CAMPOS_NOMBRE = ["primer_nombre", "segundo_nombre", "primer_apellido", "segundo_apellido"]
CAMPOS_CONTACTO = ["telefono", "correo", "direccion", "ciudad"]
PRIORIDAD_DOC = {"CC": 0, "CE": 1, "PA": 2, "TI": 3}


def norm(s) -> str:
    if pd.isna(s):
        return ""
    s = unicodedata.normalize("NFD", str(s).upper())
    return " ".join("".join(c for c in s if unicodedata.category(c) != "Mn").split())


def vacio(v) -> bool:
    return pd.isna(v) or str(v).strip() == ""


def frecuencias(df: pd.DataFrame) -> dict:
    return {c: Counter(norm(v) for v in df[c] if not vacio(v)) for c in CAMPOS_NOMBRE}


def _calidad_formato(v: str) -> tuple:
    v = str(v)
    tiene_tilde = any(unicodedata.category(c) == "Mn" for c in unicodedata.normalize("NFD", v))
    return (v.strip() == v and "  " not in v, any(c.islower() for c in v), tiene_tilde)


def elegir_maestro(df: pd.DataFrame, indices) -> object:
    def clave(i):
        f = pd.to_datetime(df.at[i, "fecha_registro"], errors="coerce")
        return (PRIORIDAD_DOC.get(str(df.at[i, "tipo_documento"]).upper(), 9),
                -(f.value if pd.notna(f) else 0), df.at[i, "id_cliente"])
    return min(indices, key=clave)


def registro_dorado(df: pd.DataFrame, indices, maestro, freq: dict) -> dict:
    """Devuelve {campo: valor} con los valores que debe tomar el registro maestro."""
    valores = {}
    por_fecha = sorted(indices, key=lambda i: pd.to_datetime(df.at[i, "fecha_registro"], errors="coerce")
                       if pd.notna(df.at[i, "fecha_registro"]) else pd.Timestamp.min, reverse=True)
    for c in CAMPOS_NOMBRE:
        candidatos = [df.at[i, c] for i in indices if not vacio(df.at[i, c])]
        if candidatos:
            valores[c] = max(candidatos, key=lambda v: (freq[c][norm(v)], _calidad_formato(v)))
            valores[c] = str(valores[c]).strip()
    if vacio(df.at[maestro, "fecha_nacimiento"]):
        for i in por_fecha:
            if not vacio(df.at[i, "fecha_nacimiento"]):
                valores["fecha_nacimiento"] = df.at[i, "fecha_nacimiento"]
                break
    for c in CAMPOS_CONTACTO:
        for i in por_fecha:
            if not vacio(df.at[i, c]):
                valores[c] = df.at[i, c]
                break
    return valores


def aplicar_grupo(df: pd.DataFrame, indices, freq: dict):
    """Marca el grupo: maestro ACTIVO con su registro dorado, el resto FUSIONADO. Devuelve el índice maestro."""
    maestro = elegir_maestro(df, indices)
    id_m = df.at[maestro, "id_cliente"]
    for i in indices:
        df.at[i, "id_maestro"] = id_m
        df.at[i, "estado_registro"] = "ACTIVO" if i == maestro else "FUSIONADO"
    for c, v in registro_dorado(df, indices, maestro, freq).items():
        df.at[maestro, c] = v
    return maestro
