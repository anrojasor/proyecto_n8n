"""
Genera un dataset sintético de clientes colombianos con duplicados etiquetados.

Incluye casos fáciles (los resuelve el modelo), casos difíciles que caen en la zona gris
(los decide el agente) y casos trampa: personas distintas que se parecen mucho.

Salidas (en la carpeta --salida):
  clientes.xlsx         tabla maestra (lo que verá el modelo)
  verdad.xlsx           etiquetas reales: id_persona_real, tipo_caso y dificultad (solo para evaluar)
  control.xlsx          tabla disparadora con una solicitud PENDIENTE
  log_ejecuciones.xlsx  vacío, solo encabezados
  log_fusiones.xlsx     vacío, solo encabezados

Uso:
  python generar_datos.py --n 3000 --tasa-dup 0.08 --tasa-trampas 0.03 --salida datos
"""
import argparse
import random
import unicodedata
from datetime import date, datetime, timedelta
from pathlib import Path

import pandas as pd
from faker import Faker

CIUDADES = ["Bogotá", "Medellín", "Cali", "Barranquilla", "Cartagena", "Bucaramanga",
            "Pereira", "Manizales", "Ibagué", "Cúcuta", "Villavicencio", "Pasto", "Neiva"]
DOMINIOS = ["gmail.com", "hotmail.com", "outlook.com", "yahoo.com"]
HOY = date.today()

# Duplicados reales: tipo -> (peso relativo, dificultad esperada)
TIPOS_DUPLICADO = {
    "TI_CC_MISMO_NUMERO":      (3, "FACIL"),    # NUIP (nacidos desde 2000): el número se conserva
    "ERROR_DIGITACION_NOMBRE": (3, "FACIL"),
    "TILDES_Y_FORMATO":        (2, "FACIL"),
    "APELLIDO_OMITIDO":        (2, "FACIL"),
    "SEGUNDO_NOMBRE_OMITIDO":  (2, "FACIL"),
    "DOCUMENTO_DIGITO_ERRADO": (2, "FACIL"),
    "MISMO_DOC_CONTACTO_NUEVO": (1, "FACIL"),
    "TI_CC_NUMERO_DISTINTO":   (2, "DIFICIL"),  # nacidos antes de 2000: el número cambia
    "FECHA_DIGITADA":          (2, "DIFICIL"),  # mismo documento, fecha de nacimiento mal digitada
    "FECHA_FALTANTE":          (2, "DIFICIL"),  # un registro no tiene fecha y el nombre varía
    "DOC_Y_NOMBRE_ERRADOS":    (2, "DIFICIL"),  # dígito errado en el documento + apellido omitido
}

# Personas distintas que se parecen: tipo -> (peso relativo, dificultad esperada)
TIPOS_TRAMPA = {
    "DISTINTO_COMPARTE_DATOS":      (1, "FACIL"),    # familiar: apellido, teléfono y dirección
    "DISTINTO_HOMONIMO":            (1, "FACIL"),    # mismo nombre, otra fecha y otro documento
    "DISTINTO_GEMELOS":             (2, "DIFICIL"),  # misma fecha, apellidos y hogar; otro nombre
    "DISTINTO_HOMONIMO_MISMA_FECHA": (1, "DIFICIL"),  # mismo nombre y fecha, otra cédula y ciudad
}


# ---------------------------------------------------------------- utilidades
def quitar_tildes(s: str) -> str:
    return "".join(c for c in unicodedata.normalize("NFD", s) if unicodedata.category(c) != "Mn")


def error_digitacion(s: str, rng: random.Random) -> str:
    if len(s) < 3:
        return s
    i = rng.randrange(1, len(s) - 1)
    op = rng.choice(["intercambio", "borrado", "duplicado", "cambio"])
    if op == "intercambio":
        return s[:i] + s[i + 1] + s[i] + s[i + 2:]
    if op == "borrado":
        return s[:i] + s[i + 1:]
    if op == "duplicado":
        return s[:i] + s[i] + s[i:]
    return s[:i] + rng.choice("aeiournslt") + s[i + 1:]


def digito_errado(num: str, rng: random.Random) -> str:
    n = list(num)
    i = rng.randrange(len(n))
    n[i] = str((int(n[i]) + rng.randint(1, 9)) % 10)
    return "".join(n)


def edad(nac: date, ref: date = None) -> int:
    ref = ref or HOY
    return ref.year - nac.year - ((ref.month, ref.day) < (nac.month, nac.day))


def fecha_a_edad(nac: date, anios: int, rng: random.Random) -> date:
    """Fecha aleatoria en la que la persona tenía `anios` años (sin pasar de hoy)."""
    inicio = date(nac.year + anios, nac.month, min(nac.day, 28))
    return min(inicio + timedelta(days=rng.randrange(0, 365)), HOY)


def fecha_digitada(nac: date, rng: random.Random) -> date:
    """Un solo error típico al digitar una fecha: día y mes invertidos, día vecino o año vecino."""
    if nac.day <= 12 and nac.day != nac.month and rng.random() < 0.6:
        return date(nac.year, nac.day, nac.month)
    if rng.random() < 0.5:
        nuevo = nac + timedelta(days=rng.choice([-1, 1]))
        return nuevo if nuevo.month == nac.month else nac - timedelta(days=1) if nuevo > nac else nac + timedelta(days=1)
    try:
        return nac.replace(year=nac.year + rng.choice([-1, 1]))
    except ValueError:  # 29 de febrero
        return nac.replace(year=nac.year + rng.choice([-1, 1]), day=28)


def nuip(rng):
    return "1" + "".join(str(rng.randrange(10)) for _ in range(9))


def cc_antigua(rng):
    return str(rng.randrange(10_000_000, 99_999_999))


def ti_antigua(nac: date, rng) -> str:
    """TI anterior al NUIP: 11 dígitos que empiezan por la fecha de nacimiento AAMMDD."""
    return nac.strftime("%y%m%d") + "".join(str(rng.randrange(10)) for _ in range(5))


def telefono(rng):
    return "3" + "".join(str(rng.randrange(10)) for _ in range(9))


def correo(nombre, apellido, rng):
    base = quitar_tildes(f"{nombre}.{apellido}").lower().replace(" ", "")
    return f"{base}{rng.randrange(1, 999)}@{rng.choice(DOMINIOS)}"


def documento_para(nac: date, rng) -> tuple[str, str]:
    if edad(nac) < 18:
        return "TI", nuip(rng)
    return "CC", (nuip(rng) if nac.year >= 2000 else cc_antigua(rng))


# ---------------------------------------------------------------- personas
def nueva_persona(fake, rng, nac=None, mujer=None):
    if nac is None:
        nac = fake.date_of_birth(minimum_age=14, maximum_age=80)
    if mujer is None:
        mujer = rng.random() < 0.5
    nombre = fake.first_name_female if mujer else fake.first_name_male
    pn = nombre()
    sn = nombre() if rng.random() < 0.6 else ""
    pa, sa = fake.last_name(), fake.last_name()
    tipo, num = documento_para(nac, rng)
    reg_min = max(nac.year + 14, HOY.year - 15)
    return {
        "tipo_documento": tipo, "numero_documento": num,
        "primer_nombre": pn, "segundo_nombre": sn,
        "primer_apellido": pa, "segundo_apellido": sa,
        "fecha_nacimiento": nac, "telefono": telefono(rng),
        "correo": correo(pn, pa, rng),
        "direccion": fake.street_address(), "ciudad": rng.choice(CIUDADES),
        # Quien tiene CC se registró siendo mayor de edad; quien se registró de menor tiene TI.
        "fecha_registro": fecha_a_edad(nac, max(18 if tipo == "CC" else 14, reg_min - nac.year), rng),
        "_mujer": mujer,
    }


def preparar_base(base, tipo, rng):
    """Ajusta la persona original según el tipo de duplicado que se le va a crear."""
    if tipo in ("TI_CC_MISMO_NUMERO", "TI_CC_NUMERO_DISTINTO"):
        # Regla de negocio: desde 2000 (NUIP) el número se conserva; antes cambiaba.
        if tipo == "TI_CC_MISMO_NUMERO":
            inicio, fin = date(2000, 1, 1), date(HOY.year - 19, HOY.month, min(HOY.day, 28))
        else:
            inicio, fin = date(1985, 1, 1), date(1999, 12, 31)
        nac = inicio + timedelta(days=rng.randrange((fin - inicio).days))
        base["fecha_nacimiento"] = nac
        base["tipo_documento"] = "TI"
        base["numero_documento"] = nuip(rng) if tipo == "TI_CC_MISMO_NUMERO" else ti_antigua(nac, rng)
        base["fecha_registro"] = fecha_a_edad(nac, rng.randint(14, 17), rng)
    elif tipo == "DOC_Y_NOMBRE_ERRADOS" and not base["segundo_apellido"]:
        base["segundo_apellido"] = "Gómez"
    return base


def derivar_duplicado(base, tipo, fake, rng):
    d = dict(base)
    d["fecha_registro"] = min(base["fecha_registro"] + timedelta(days=rng.randint(200, 2500)), HOY)

    if tipo == "TI_CC_MISMO_NUMERO":
        d["tipo_documento"] = "CC"
        d["fecha_registro"] = max(d["fecha_registro"], fecha_a_edad(base["fecha_nacimiento"], 18, rng))
    elif tipo == "TI_CC_NUMERO_DISTINTO":
        d["tipo_documento"] = "CC"
        d["numero_documento"] = cc_antigua(rng)
        d["fecha_registro"] = max(d["fecha_registro"], fecha_a_edad(base["fecha_nacimiento"], 18, rng))
    elif tipo == "ERROR_DIGITACION_NOMBRE":
        campo = rng.choice(["primer_nombre", "primer_apellido", "segundo_apellido"])
        d[campo] = error_digitacion(d[campo], rng)
    elif tipo == "TILDES_Y_FORMATO":
        for c in ["primer_nombre", "segundo_nombre", "primer_apellido", "segundo_apellido"]:
            d[c] = quitar_tildes(d[c]).upper()
        d["primer_apellido"] = d["primer_apellido"] + "  "
    elif tipo == "APELLIDO_OMITIDO":
        d["segundo_apellido"] = ""
    elif tipo == "SEGUNDO_NOMBRE_OMITIDO":
        d["segundo_nombre"] = "" if d["segundo_nombre"] else fake.first_name()
    elif tipo == "DOCUMENTO_DIGITO_ERRADO":
        d["numero_documento"] = digito_errado(d["numero_documento"], rng)
    elif tipo == "MISMO_DOC_CONTACTO_NUEVO":
        d["telefono"] = telefono(rng)
        d["direccion"] = fake.street_address()
        d["correo"] = correo(d["primer_nombre"], d["primer_apellido"], rng)
    elif tipo == "FECHA_DIGITADA":
        d["fecha_nacimiento"] = fecha_digitada(base["fecha_nacimiento"], rng)
        if rng.random() < 0.5:
            d["primer_apellido"] = error_digitacion(d["primer_apellido"], rng)
        d["telefono"], d["correo"] = telefono(rng), correo(d["primer_nombre"], d["primer_apellido"], rng)
    elif tipo == "FECHA_FALTANTE":
        d["fecha_nacimiento"] = None
        if rng.random() < 0.5:
            d["segundo_apellido"] = ""
        else:
            d["segundo_nombre"] = "" if d["segundo_nombre"] else fake.first_name()
        d["telefono"], d["correo"] = telefono(rng), correo(d["primer_nombre"], d["primer_apellido"], rng)
    elif tipo == "DOC_Y_NOMBRE_ERRADOS":
        d["numero_documento"] = digito_errado(d["numero_documento"], rng)
        d["segundo_apellido"] = ""
        d["primer_nombre"] = error_digitacion(d["primer_nombre"], rng)
        d["telefono"], d["correo"] = telefono(rng), correo(d["primer_nombre"], d["primer_apellido"], rng)

    if tipo not in ("MISMO_DOC_CONTACTO_NUEVO", "FECHA_DIGITADA", "FECHA_FALTANTE", "DOC_Y_NOMBRE_ERRADOS") \
            and rng.random() < 0.4:
        d["telefono"] = telefono(rng)
    if rng.random() < 0.3:
        d["direccion"] = fake.street_address()
    return d


def crear_trampa(ref, tipo, fake, rng):
    """Persona DISTINTA que se parece a `ref`."""
    if tipo == "DISTINTO_COMPARTE_DATOS":
        otro = nueva_persona(fake, rng)
        otro.update(primer_apellido=ref["primer_apellido"], telefono=ref["telefono"],
                    direccion=ref["direccion"], ciudad=ref["ciudad"])
    elif tipo == "DISTINTO_HOMONIMO":
        otro = nueva_persona(fake, rng)
        for c in ["primer_nombre", "segundo_nombre", "primer_apellido", "segundo_apellido"]:
            otro[c] = ref[c]
    elif tipo == "DISTINTO_GEMELOS":
        # Hermano gemelo: misma fecha, apellidos, hogar y ciudad; distinto nombre y documento.
        otro = nueva_persona(fake, rng, nac=ref["fecha_nacimiento"], mujer=rng.random() < 0.5)
        while otro["primer_nombre"] == ref["primer_nombre"]:
            otro = nueva_persona(fake, rng, nac=ref["fecha_nacimiento"])
        otro.update(primer_apellido=ref["primer_apellido"], segundo_apellido=ref["segundo_apellido"],
                    telefono=ref["telefono"] if rng.random() < 0.7 else otro["telefono"],
                    direccion=ref["direccion"], ciudad=ref["ciudad"],
                    tipo_documento=ref["tipo_documento"])
        otro["correo"] = correo(otro["primer_nombre"], otro["primer_apellido"], rng)
    else:  # DISTINTO_HOMONIMO_MISMA_FECHA
        # Nombre común repetido con la misma fecha: dos cédulas distintas, otra ciudad y otro contacto.
        otro = nueva_persona(fake, rng, nac=ref["fecha_nacimiento"])
        for c in ["primer_nombre", "segundo_nombre", "primer_apellido", "segundo_apellido"]:
            otro[c] = ref[c]
        otro["tipo_documento"] = ref["tipo_documento"]
        otro["ciudad"] = rng.choice([c for c in CIUDADES if c != ref["ciudad"]])
        otro["correo"] = correo(otro["primer_nombre"], otro["primer_apellido"], rng)
    return otro


def anadir_faltantes(reg, rng, tasa):
    for c in ["telefono", "correo", "direccion"]:
        if rng.random() < tasa:
            reg[c] = None
    return reg


# ---------------------------------------------------------------- principal
def generar(n, tasa_dup, tasa_trampas, tasa_faltantes, semilla):
    rng = random.Random(semilla)
    fake = Faker("es_CO")
    Faker.seed(semilla)

    registros = []  # (registro, id_persona_real, tipo_caso, dificultad)
    tipos_d = list(TIPOS_DUPLICADO)
    pesos_d = [TIPOS_DUPLICADO[t][0] for t in tipos_d]

    for pid in range(1, n + 1):
        base = nueva_persona(fake, rng)
        if rng.random() < tasa_dup:
            tipo = rng.choices(tipos_d, pesos_d)[0]
            base = preparar_base(base, tipo, rng)
            registros.append((base, pid, "ORIGINAL", ""))
            registros.append((derivar_duplicado(base, tipo, fake, rng), pid, tipo, TIPOS_DUPLICADO[tipo][1]))
        else:
            registros.append((base, pid, "ORIGINAL", ""))

    originales = [r[0] for r in registros if r[2] == "ORIGINAL" and r[0]["fecha_nacimiento"]]
    tipos_t = list(TIPOS_TRAMPA)
    pesos_t = [TIPOS_TRAMPA[t][0] for t in tipos_t]
    pid = n
    for _ in range(int(n * tasa_trampas)):
        pid += 1
        tipo = rng.choices(tipos_t, pesos_t)[0]
        registros.append((crear_trampa(rng.choice(originales), tipo, fake, rng), pid, tipo, TIPOS_TRAMPA[tipo][1]))

    rng.shuffle(registros)
    filas, verdad = [], []
    for i, (reg, pid_real, caso, dif) in enumerate(registros, start=1):
        reg = anadir_faltantes(dict(reg), rng, tasa_faltantes)
        reg.pop("_mujer", None)
        filas.append({"id_cliente": i, **reg, "id_maestro": None, "estado_registro": "ACTIVO"})
        verdad.append({"id_cliente": i, "id_persona_real": pid_real, "tipo_caso": caso, "dificultad": dif})
    return pd.DataFrame(filas), pd.DataFrame(verdad)


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--n", type=int, default=3000, help="personas únicas")
    p.add_argument("--tasa-dup", type=float, default=0.08, help="fracción de personas con duplicado")
    p.add_argument("--tasa-trampas", type=float, default=0.03, help="personas distintas parecidas, sobre n")
    p.add_argument("--tasa-faltantes", type=float, default=0.03, help="prob. de vaciar teléfono/correo/dirección")
    p.add_argument("--semilla", type=int, default=42)
    p.add_argument("--salida", default="datos")
    a = p.parse_args()

    out = Path(a.salida)
    out.mkdir(parents=True, exist_ok=True)
    clientes, verdad = generar(a.n, a.tasa_dup, a.tasa_trampas, a.tasa_faltantes, a.semilla)

    clientes.to_excel(out / "clientes.xlsx", index=False)
    verdad.to_excel(out / "verdad.xlsx", index=False)
    pd.DataFrame([{"id": 1, "estado": "PENDIENTE",
                   "fecha_solicitud": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                   "fecha_fin": "", "mensaje_error": ""}]).to_excel(out / "control.xlsx", index=False)
    pd.DataFrame(columns=["id_ejecucion", "fecha", "registros_procesados", "pares_evaluados",
                          "duplicados_auto", "duplicados_agente", "pendientes_revision",
                          "duracion_seg", "version_modelo"]).to_excel(out / "log_ejecuciones.xlsx", index=False)
    pd.DataFrame(columns=["id_ejecucion", "id_origen", "id_maestro", "score",
                          "decidido_por", "justificacion"]).to_excel(out / "log_fusiones.xlsx", index=False)

    print(f"Registros generados: {len(clientes)} en '{out}/'")
    resumen = verdad[verdad.tipo_caso != "ORIGINAL"].groupby(["dificultad", "tipo_caso"]).size()
    print(resumen.to_string())


if __name__ == "__main__":
    main()
