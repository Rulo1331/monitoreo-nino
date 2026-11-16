"""
Recolector: postura oficial del ENFEN (comunicados oficiales), carga desde una carpeta.

La web del ENFEN no es accesible desde los servidores de GitHub, y los comunicados salen cada
~15 días, así que el flujo es manual y simple:

  1. Descargas el PDF del comunicado desde https://enfen.imarpe.gob.pe/
  2. Lo subes a la carpeta datos/comunicados_enfen/ del repositorio (GitHub → Add file → Upload files)
  3. El workflow "ENFEN comunicados" se ejecuta solo, lee TODOS los PDF de la carpeta y reemplaza
     la tabla enfen_comunicados.

Qué guarda:
  - enfen_comunicados: una fila por PDF con número, fecha, estado del sistema de alerta, titular,
    magnitud y horizonte para Niño 1+2 y Niño 3.4, próximo comunicado, enlace al PDF en GitHub
    y el texto completo.
  - enfen_probabilidades: las probabilidades vienen en una FIGURA del comunicado, así que se
    cargan a mano en datos/enfen_probabilidades.csv.

El número del comunicado se toma del texto del PDF ("N° 20-2026"); si no aparece, del nombre
del archivo. Si un dato queda vacío o mal leído (revisar = true), se corrige en
datos/enfen_correcciones.csv.

Uso:
    python recolectores/enfen.py --validar
    python recolectores/enfen.py --carpeta otra/carpeta --sin-guardar     # prueba local
"""
import argparse
import io
import json
import os
import re
import sqlite3
import sys
from datetime import date, datetime, timezone
from pathlib import Path
from urllib.parse import quote

import pandas as pd

RAIZ = Path(__file__).resolve().parent.parent
CARPETA = RAIZ / "datos" / "comunicados_enfen"
CSV_PROB = RAIZ / "datos" / "enfen_probabilidades.csv"
CSV_CORR = RAIZ / "datos" / "enfen_correcciones.csv"
FUENTE = "ENFEN, comunicado oficial (PDF cargado al repositorio)"
NUMERO_TEXTO = re.compile(r"comunicado\s+oficial\s+(?:enfen\s+)?n\s*[°º.o]*\s*(\d{1,2})\s*[-–]\s*(\d{4})", re.I)
NUMERO_ARCHIVO = re.compile(r"(?<!\d)(\d{1,2})\s*[-_ ]\s*(20\d{2})(?!\d)")

MESES = {"enero": 1, "febrero": 2, "marzo": 3, "abril": 4, "mayo": 5, "junio": 6, "julio": 7,
         "agosto": 8, "setiembre": 9, "septiembre": 9, "octubre": 10, "noviembre": 11, "diciembre": 12}
MES_RE = "|".join(MESES)
# Fechas como las escribe el ENFEN: "15 de enero 2026", "28 setiembre 2026", "30 de enero de 2026"
FECHA = re.compile(rf"\b(\d{{1,2}})\s+(?:de\s+)?({MES_RE})\s+(?:de(?:l)?\s+)?(\d{{4}})", re.I)
FIN_PALABRA = r"(?![a-záéíóúñ])"          # en vez de \b: las notas al pie se pegan ("Costero1", "fuerte3")
ESTADO = re.compile(r"\b(no\s+activo|(?:vigilancia|alerta)\s+de\s+(?:el\s+niño|la\s+niña)\s+coster[oa])"
                    + FIN_PALABRA, re.I)
MAGS = r"extraordinaria|muy\s+fuerte|fuerte|moderad[ao]|débil"
MAGNITUD = re.compile(
    rf"magnitud(?:es)?\s+(?:(?:más\s+probables?|de|es|serían|sería|una)\s+)*(?P<entre>entre\s+)?"
    rf"(?P<a>{MAGS})(?:e?s)?{FIN_PALABRA}\s*\d?\s*(?:\([^)]*\)\s*)?"
    rf"(?:(?P<sep>y|o)\s+(?P<b>{MAGS}){FIN_PALABRA})?", re.I)
HORIZONTE = re.compile(
    rf"\bhasta\s+((?:el\s+)?(?:(?:próximo|inicios|fines|mediados|finales)\s+(?:de\s+|del\s+)?)?"
    rf"(?:(?:el\s+)?(?:{MES_RE}|verano|otoño|invierno|primavera)(?:\s+(?:de\s+|del\s+)?(?:\d{{4}}|presente\s+año))?"
    rf"|\d{{4}}))(?=\d?(?!\d))", re.I)
# "de setiembre 2026 a enero de 2027": el final del rango sirve de horizonte si no hay un "hasta"
RANGO_A = re.compile(rf"\bde\s+(?:{MES_RE})\s+(?:de\s+)?\d{{4}}\s+a\s+((?:{MES_RE})\s+(?:de\s+)?\d{{4}})", re.I)
# "las condiciones cálidas débiles son las más probables" (sin la palabra "magnitud")
CONDICION = re.compile(rf"condici[oó]n(?:es)?\s+cálidas?\s+(?P<a>{MAGS})(?:e?s)?{FIN_PALABRA}", re.I)
PROXIMO = re.compile(rf"pr[oó]ximo\s+comunicado.{{0,160}}?(\d{{1,2}})\s+de\s+({MES_RE})\s+(?:de\s+|del\s+)?(\d{{4}})",
                     re.I | re.S)
# Dónde empieza el párrafo del Pacífico central y dónde termina el resumen de los océanos
INICIO_34 = re.compile(r"Niño\s*3[.,]\s*4|Pacífico\s+(?:ecuatorial\s+)?central", re.I)
FIN_RESUMEN = re.compile(r"precipitaci|lluvias|pronóstico\s+hidrológico|recursos\s+pesqueros|"
                         r"ante\s+estos\s+escenarios|se\s+recomienda", re.I)

REGIONES_PROB = {"Niño 1+2", "Niño 3.4"}
MAGNITUDES_PROB = {"Neutra", "Débil", "Moderada", "Fuerte", "Muy fuerte", "Extraordinaria",
                   "Fría débil", "Fría moderada", "Fría fuerte"}
CAMPOS_CORREGIBLES = {"fecha", "estado_alerta", "titular", "magnitud_nino12", "horizonte_nino12",
                      "magnitud_nino34", "horizonte_nino34", "proximo_comunicado"}


# ---------------------------------------------------------------- lectura del texto
def a_fecha(d, mes, a):
    try:
        return date(int(a), MESES[mes.lower()], int(d))
    except (KeyError, ValueError):
        return None


def capitalizar(estado):
    e = re.sub(r"\s+", " ", estado.strip().lower())
    e = e[0].upper() + e[1:]
    for a, b in (("el niño", "El Niño"), ("la niña", "La Niña"), ("costero", "Costero"), ("costera", "Costera")):
        e = e.replace(a, b)
    return e


def frase_magnitud(m):
    a = re.sub(r"\s+", " ", m.group("a").lower()).replace("moderado", "moderada")
    if m.group("b"):
        b = re.sub(r"\s+", " ", m.group("b").lower()).replace("moderado", "moderada")
        return f"Entre {a} y {b}" if m.group("entre") else f"{a.capitalize()} {m.group('sep').lower()} {b}"
    return a.capitalize()


def leer_bloque(bloque, anio):
    """Magnitud y horizonte de un párrafo.
    Magnitud: la primera mencionada ("magnitud ..." o, si no aparece, "condiciones cálidas ...").
    Horizonte: el primer "hasta ..." del párrafo que no hable de la condición neutra; si no hay,
    el final de un rango "de <mes> a <mes>"."""
    m = MAGNITUD.search(bloque) or CONDICION.search(bloque)
    if not m:
        return None, None
    magnitud = frase_magnitud(m) if "sep" in m.groupdict() else frase_magnitud_simple(m.group("a"))
    horizonte = None
    for h in HORIZONTE.finditer(bloque):
        if "neutra" not in bloque[max(0, h.start() - 60):h.start()].lower():
            horizonte = h.group(1)
            break
    if horizonte is None:
        r = RANGO_A.search(bloque)
        horizonte = r.group(1) if r else None
    if horizonte:
        horizonte = re.sub(r"\s+", " ", horizonte).lower().replace("del presente año", f"de {anio}")
        horizonte = "hasta " + horizonte
    return magnitud, horizonte


def frase_magnitud_simple(a):
    return re.sub(r"\s+", " ", a.lower()).replace("moderado", "moderada").capitalize()


def leer_comunicado(texto, comunicado):
    nro, anio = (int(x) for x in comunicado.split("-"))
    fila = {"comunicado": comunicado, "anio": anio, "numero": nro, "fecha": None, "estado_alerta": None,
            "titular": None, "magnitud_nino12": None, "horizonte_nino12": None, "magnitud_nino34": None,
            "horizonte_nino34": None, "proximo_comunicado": None}
    plano = re.sub(r"\s+", " ", texto)
    cab = NUMERO_TEXTO.search(plano)
    pos = cab.end() if cab else 0

    # Fecha: la que va justo debajo del número del comunicado
    m = FECHA.match(plano, pos) or FECHA.search(plano, pos, pos + 80) or FECHA.search(plano, pos, pos + 400)
    if m:
        fila["fecha"] = a_fecha(*m.groups())
        pos = m.end()

    # Estado: con o sin el rótulo "Estado del sistema de alerta:", en las líneas siguientes
    m = ESTADO.search(plano, pos, pos + 200)
    if m:
        fila["estado_alerta"] = capitalizar(m.group(1))
        pos = m.end()

    # Titular: líneas en mayúsculas después del estado (los comunicados recientes lo traen)
    resto = texto[texto.find(plano[pos - 20:pos].strip()[-12:]):] if pos else texto
    titular = []
    for linea in resto.splitlines()[1:12]:
        l = linea.strip()
        letras = [c for c in l if c.isalpha()]
        mayus = bool(letras) and sum(c.isupper() for c in letras) / len(letras) > 0.9
        if mayus and len(l) >= 15 and "RESUMEN" not in l.upper():
            titular.append(l)
        elif titular:
            break
    if titular:
        fila["titular"] = re.sub(r"\s+", " ", " ".join(titular))
        i = plano.find(fila["titular"], pos)
        if i != -1:
            pos = i + len(fila["titular"])

    # Magnitud y horizonte: párrafo de Niño 1+2 (antes de la mención del Pacífico central)
    # y párrafo de Niño 3.4 (desde esa mención hasta que el comunicado pasa a lluvias, ríos o pesca)
    resumen = plano[pos:pos + 2500]
    c34 = INICIO_34.search(resumen)
    b12 = resumen[:c34.start()] if c34 else resumen
    fila["magnitud_nino12"], fila["horizonte_nino12"] = leer_bloque(b12, anio)
    if c34:
        resto34 = resumen[c34.start():]
        f = FIN_RESUMEN.search(resto34)
        b34 = resto34[: f.start() if f else 900][:900]
        fila["magnitud_nino34"], fila["horizonte_nino34"] = leer_bloque(b34, anio)

    m = PROXIMO.search(plano)
    if m:
        fila["proximo_comunicado"] = a_fecha(*m.groups())
    return fila


# ---------------------------------------------------------------- archivos manuales
def leer_csv(ruta, columnas):
    if not ruta.exists():
        return pd.DataFrame(columns=columnas)
    df = pd.read_csv(ruta, comment="#", dtype=str, skip_blank_lines=True).fillna("")
    faltan = set(columnas) - set(df.columns)
    if faltan:
        raise ValueError(f"{ruta.name}: faltan las columnas {sorted(faltan)}.")
    return df[columnas].apply(lambda s: s.str.strip())


def leer_probabilidades():
    cols = ["comunicado", "region", "periodo", "magnitud", "probabilidad", "nota"]
    df = leer_csv(CSV_PROB, cols)
    if df.empty:
        return df.assign(probabilidad=pd.Series(dtype="float64"))
    errores = []
    if not df["comunicado"].str.fullmatch(r"\d{1,2}-\d{4}").all():
        errores.append("comunicado debe escribirse como 20-2026")
    if not df["region"].isin(REGIONES_PROB).all():
        errores.append(f"region debe ser una de {sorted(REGIONES_PROB)}")
    if not df["magnitud"].isin(MAGNITUDES_PROB).all():
        errores.append(f"magnitud debe ser una de {sorted(MAGNITUDES_PROB)}")
    if ((df["region"] == "Niño 3.4") & (df["magnitud"] == "Extraordinaria")).any():
        errores.append("Niño 3.4 usa la escala del RONI, cuya categoría más alta es 'Muy fuerte' "
                       "(no existe 'Extraordinaria')")
    df["probabilidad"] = pd.to_numeric(df["probabilidad"], errors="coerce")
    if not df["probabilidad"].between(0, 100).all():
        errores.append("probabilidad debe ser un número entre 0 y 100 (sin el signo %)")
    if df.duplicated(["comunicado", "region", "periodo", "magnitud"]).any():
        errores.append("hay filas repetidas (mismo comunicado, región, periodo y magnitud)")
    if errores:
        raise ValueError(f"{CSV_PROB.name}: " + "; ".join(errores))
    df["fuente"] = "ENFEN, comunicado oficial (figura de probabilidades; carga manual)"
    df["actualizado_en"] = datetime.now(timezone.utc)
    return df


def aplicar_correcciones(df):
    corr = leer_csv(CSV_CORR, ["comunicado", "campo", "valor"])
    for _, c in corr.iterrows():
        if c.campo not in CAMPOS_CORREGIBLES:
            raise ValueError(f"{CSV_CORR.name}: el campo '{c.campo}' no se puede corregir "
                             f"(usar uno de {sorted(CAMPOS_CORREGIBLES)}).")
        if c.comunicado not in set(df["comunicado"]):
            print(f"   AVISO: corrección para {c.comunicado}, que aún no está en la tabla (se aplicará cuando llegue).")
            continue
        valor = c.valor or None
        if valor and c.campo in ("fecha", "proximo_comunicado"):
            valor = date.fromisoformat(valor)          # formato AAAA-MM-DD
        df.loc[df["comunicado"] == c.comunicado, c.campo] = valor
    return df


def marcar_revision(df):
    clave = ["fecha", "estado_alerta", "magnitud_nino12"]
    df["revisar"] = df[clave].isna().any(axis=1)
    return df


# ---------------------------------------------------------------- BigQuery / SQLite
ESQUEMA_COM = [("comunicado", "STRING"), ("anio", "INT64"), ("numero", "INT64"), ("fecha", "DATE"),
               ("estado_alerta", "STRING"), ("titular", "STRING"), ("magnitud_nino12", "STRING"),
               ("horizonte_nino12", "STRING"), ("magnitud_nino34", "STRING"), ("horizonte_nino34", "STRING"),
               ("proximo_comunicado", "DATE"), ("revisar", "BOOL"), ("archivo", "STRING"), ("url", "STRING"), ("texto", "STRING"),
               ("fuente", "STRING"), ("actualizado_en", "TIMESTAMP")]
ESQUEMA_PROB = [("comunicado", "STRING"), ("region", "STRING"), ("periodo", "STRING"), ("magnitud", "STRING"),
                ("probabilidad", "FLOAT64"), ("nota", "STRING"), ("fuente", "STRING"), ("actualizado_en", "TIMESTAMP")]


def destino():
    proyecto = os.getenv("BQ_PROYECTO")
    if not proyecto:
        return None, None
    from google.cloud import bigquery
    clave = os.getenv("GCP_SA_KEY")
    if clave:
        from google.oauth2 import service_account
        cred = service_account.Credentials.from_service_account_info(json.loads(clave))
        cliente = bigquery.Client(project=proyecto, credentials=cred)
    else:
        cliente = bigquery.Client(project=proyecto)
    ds = os.getenv("BQ_DATASET") or "monitoreo_nino"
    return cliente, (ds if "." in ds else f"{proyecto}.{ds}")


def leer_existente(cliente, ds, nombre, esquema):
    if cliente is None:
        if not os.path.exists("datos_nino.db"):
            return None
        with sqlite3.connect("datos_nino.db") as con:
            if not con.execute("SELECT name FROM sqlite_master WHERE name=?", (nombre,)).fetchone():
                return None
            df = pd.read_sql(f'SELECT * FROM "{nombre}"', con)
        for c in ("fecha", "proximo_comunicado"):
            df[c] = pd.to_datetime(df[c]).dt.date
        return df
    from google.api_core.exceptions import NotFound
    try:
        tabla = cliente.get_table(f"{ds}.{nombre}")
    except NotFound:
        return None
    if {c.name for c in tabla.schema} != {n for n, _ in esquema}:
        print(f"ERROR: la tabla {ds}.{nombre} existe con otra estructura y NO se sobrescribirá.")
        sys.exit(1)
    return cliente.list_rows(tabla).to_dataframe(create_bqstorage_client=False)


def guardar(cliente, ds, nombre, df, esquema):
    df = df[[n for n, _ in esquema]]
    if cliente is None:  # modo local de prueba: SQLite solo acepta texto para fechas
        df = df.copy()
        df["actualizado_en"] = pd.to_datetime(df["actualizado_en"], utc=True).astype(str)
        for c in ("fecha", "proximo_comunicado"):
            if c in df:
                df[c] = df[c].astype(str).replace({"None": None, "NaT": None})
        with sqlite3.connect("datos_nino.db") as con:
            df.to_sql(nombre, con, if_exists="replace", index=False)
        return f"SQLite local 'datos_nino.db', tabla '{nombre}' ({len(df)} filas)"
    from google.cloud import bigquery
    dataset = cliente.get_dataset(ds)
    config = bigquery.LoadJobConfig(schema=[bigquery.SchemaField(n, t) for n, t in esquema],
                                    write_disposition="WRITE_TRUNCATE")
    cliente.load_table_from_dataframe(df, f"{ds}.{nombre}", job_config=config,
                                      location=dataset.location).result()
    return f"BigQuery {ds}.{nombre} ({len(df)} filas)"


# ---------------------------------------------------------------- lectura de la carpeta
def texto_pdf(contenido):
    from pypdf import PdfReader
    if not contenido.startswith(b"%PDF"):
        raise ValueError("el archivo no es un PDF")
    lector = PdfReader(io.BytesIO(contenido))
    return "\n".join((p.extract_text() or "") for p in lector.pages)


def numero_de(texto, nombre):
    m = NUMERO_TEXTO.search(re.sub(r"\s+", " ", texto[:3000])) or NUMERO_ARCHIVO.search(nombre)
    return f"{int(m.group(1))}-{m.group(2)}" if m else None


def enlace_github(nombre):
    """Enlace para abrir el PDF desde el dashboard (solo dentro de GitHub Actions)."""
    repo = os.getenv("GITHUB_REPOSITORY")
    if not repo:
        return None
    servidor = os.getenv("GITHUB_SERVER_URL", "https://github.com")
    rama = os.getenv("GITHUB_REF_NAME", "main")
    return f"{servidor}/{repo}/blob/{rama}/datos/comunicados_enfen/{quote(nombre)}"


def leer_carpeta(carpeta):
    filas, errores = [], []
    archivos = sorted(p for p in carpeta.glob("*") if p.suffix.lower() == ".pdf")
    print(f"1) PDF en {carpeta.relative_to(RAIZ) if carpeta.is_relative_to(RAIZ) else carpeta}: {len(archivos)}")
    for ruta in archivos:
        try:
            texto = texto_pdf(ruta.read_bytes())
        except Exception as e:
            errores.append(f"{ruta.name}: no se pudo leer ({e})")
            continue
        comunicado = numero_de(texto, ruta.name)
        if comunicado is None:
            errores.append(f"{ruta.name}: no se encontró el número ni en el texto ni en el nombre "
                           f"(renómbralo, por ejemplo, Comunicado-20-2026.pdf)")
            continue
        if len(texto.strip()) < 200:      # PDF escaneado: se registra igual, para corregirlo a mano
            print(f"   AVISO: {ruta.name} casi no tiene texto (¿es una imagen?); completa sus datos en {CSV_CORR.name}")
            texto = ""
        fila = leer_comunicado(texto, comunicado)
        fila.update(archivo=ruta.name, url=enlace_github(ruta.name), texto=re.sub(r"[ \t]+", " ", texto).strip(),
                    fuente=FUENTE, actualizado_en=datetime.now(timezone.utc))
        filas.append(fila)
    df = pd.DataFrame(filas, columns=[n for n, _ in ESQUEMA_COM if n != "revisar"])
    repetidos = df[df.duplicated("comunicado", keep=False)]
    if not repetidos.empty:
        errores.append("comunicados repetidos: " + ", ".join(f"{r.comunicado} ({r.archivo})" for r in repetidos.itertuples()))
        df = df.drop_duplicates("comunicado", keep="last")
    return df, errores


# ---------------------------------------------------------------- programa principal
def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--validar", action="store_true")
    ap.add_argument("--carpeta", default=str(CARPETA))
    ap.add_argument("--sin-guardar", action="store_true")
    a = ap.parse_args()

    carpeta = Path(a.carpeta).resolve()
    carpeta.mkdir(parents=True, exist_ok=True)
    tabla, errores = leer_carpeta(carpeta)
    for e in errores:
        print(f"   ERROR: {e}")

    if not tabla.empty:
        tabla = marcar_revision(aplicar_correcciones(tabla))
        tabla = tabla.sort_values(["anio", "numero"]).reset_index(drop=True)
        vista = tabla[["comunicado", "fecha", "estado_alerta", "magnitud_nino12", "horizonte_nino12",
                       "magnitud_nino34", "horizonte_nino34", "proximo_comunicado", "revisar"]]
        print("\n2) Comunicados leídos:")
        with pd.option_context("display.width", 250, "display.max_columns", 20, "display.max_colwidth", 30):
            print(vista.to_string(index=False))
        nums = tabla.groupby("anio")["numero"].apply(set)
        for anio, vistos in nums.items():
            faltan = sorted(set(range(1, max(vistos) + 1)) - vistos)
            if len(faltan) > 5:
                print(f"   AVISO: faltan {len(faltan)} comunicados de {anio} (entre el 1 y el {max(faltan)})")
            elif faltan:
                print(f"   AVISO: faltan los comunicados {', '.join(f'{n}-{anio}' for n in faltan)}")

    error_prob = None
    try:
        prob = leer_probabilidades()
        print(f"\n3) Probabilidades manuales: {len(prob)} fila(s) en {CSV_PROB.name}")
    except ValueError as e:          # un error en el CSV no impide guardar los comunicados
        error_prob, prob = str(e), pd.DataFrame(columns=["comunicado"])
        print(f"\nERROR en las probabilidades manuales (no se actualizan): {error_prob}")

    if a.validar and not tabla.empty:
        pendientes = tabla[tabla["revisar"]]["comunicado"].tolist()
        if pendientes:
            print(f"AVISO: revisar {pendientes}: falta fecha, estado o magnitud de Niño 1+2. "
                  f"Corrígelo en {CSV_CORR.name}.")
        ultimo = tabla["comunicado"].iloc[-1]
        if ultimo not in set(prob["comunicado"]):
            print(f"AVISO: el último comunicado ({ultimo}) aún no tiene probabilidades en {CSV_PROB.name}.")

    if a.sin_guardar:
        print("\nModo de prueba: no se guardó nada.")
    else:
        cliente, ds = destino()
        nombre = os.getenv("BQ_TABLA_ENFEN") or "enfen_comunicados"
        existente = leer_existente(cliente, ds, nombre, ESQUEMA_COM)
        if existente is not None and len(tabla) < len(existente):
            print(f"ERROR: la carpeta tiene {len(tabla)} comunicados y la tabla {len(existente)}. "
                  "¿Se borró o renombró algún PDF? No se escribe nada.")
            return 1
        if not tabla.empty:
            print("\nGuardado en:", guardar(cliente, ds, nombre, tabla, ESQUEMA_COM))
        if error_prob is None:
            print("Guardado en:", guardar(cliente, ds, os.getenv("BQ_TABLA_ENFEN_PROB") or "enfen_probabilidades",
                                           prob, ESQUEMA_PROB))
    return 1 if (errores or error_prob) else 0


if __name__ == "__main__":
    sys.exit(main())
