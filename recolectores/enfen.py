"""
Recolector: postura oficial del ENFEN (comunicados oficiales).

Fuente: https://enfen.imarpe.gob.pe/ (la portada lista los últimos comunicados en PDF).

Qué guarda:
  1. enfen_comunicados (automático): una fila por comunicado con número, fecha, estado del
     sistema de alerta, titular, magnitud esperada y horizonte para Niño 1+2 (El Niño costero)
     y Niño 3.4 (Pacífico central), fecha del próximo comunicado y el texto completo del PDF.
     La tabla ACUMULA: los comunicados que ya no aparecen en la portada se conservan.
  2. enfen_probabilidades (manual): las probabilidades por magnitud vienen en una FIGURA del
     comunicado, no en el texto, así que se cargan a mano en datos/enfen_probabilidades.csv.

Correcciones: si el texto de un comunicado viene redactado de otra forma y algún campo queda
vacío o mal leído, se corrige en datos/enfen_correcciones.csv (no hace falta tocar el código).
La columna `revisar` = true marca los comunicados con algún campo clave sin leer.

Si la web del ENFEN no responde, no borra nada: conserva la tabla, carga los CSV y termina
con un AVISO (código 0).

Uso:
    python recolectores/enfen.py --validar
    python recolectores/enfen.py --reprocesar          # vuelve a leer los PDF de la portada
    python recolectores/enfen.py --pdf prueba.pdf --sin-guardar   # prueba con un PDF local
"""
import argparse
import html
import io
import json
import os
import re
import sqlite3
import sys
import time
from datetime import date, datetime, timezone
from pathlib import Path

import pandas as pd
import requests

PORTADA = "https://enfen.imarpe.gob.pe/"
LISTADO = "https://enfen.imarpe.gob.pe/comunicados/"
RAIZ = Path(__file__).resolve().parent.parent
CSV_PROB = RAIZ / "datos" / "enfen_probabilidades.csv"
CSV_CORR = RAIZ / "datos" / "enfen_correcciones.csv"
FUENTE = "ENFEN, comunicado oficial (PDF)"
CABECERAS = {"User-Agent": "Mozilla/5.0 (X11; Linux x86_64) monitoreo-nino/1.0 (uso interno)",
             "Accept-Language": "es-PE,es;q=0.9"}

ENLACE = re.compile(r'href="(https://enfen\.imarpe\.gob\.pe/download/'
                    r'comunicado-oficial-enfen-n-?(\d{1,2})-(\d{4})[^"]*)"', re.I)
MESES = {"enero": 1, "febrero": 2, "marzo": 3, "abril": 4, "mayo": 5, "junio": 6, "julio": 7,
         "agosto": 8, "setiembre": 9, "septiembre": 9, "octubre": 10, "noviembre": 11, "diciembre": 12}
MES_RE = "|".join(MESES)
FECHA = re.compile(rf"\b(\d{{1,2}})\s+de\s+({MES_RE})\s+(?:de|del)\s+(\d{{4}})", re.I)
# "muy fuerte" va antes que "fuerte" para que gane la coincidencia más larga
MAGNITUDES = ["extraordinaria", "muy fuerte", "fuerte", "moderada", "débil"]
MAG_RE = re.compile(r"\b(" + "|".join(MAGNITUDES) + r")\b", re.I)
ESTADO = re.compile(r"\b(no activo|(?:vigilancia|alerta)\s+de\s+(?:el\s+niño|la\s+niña)\s+coster[oa])\b", re.I)
HORIZONTE = re.compile(rf"\bhasta\s+((?:(?:inicios|fines|mediados|finales|el\s+verano|el\s+otoño|el\s+invierno)"
                       rf"\s+(?:de|del)\s+)?(?:(?:{MES_RE}|verano|otoño|invierno|primavera)\s+(?:de|del)\s+)?\d{{4}})", re.I)
PROXIMO = re.compile(rf"pr[oó]ximo\s+comunicado.{{0,160}}?(\d{{1,2}})\s+de\s+({MES_RE})\s+(?:de|del)\s+(\d{{4}})",
                     re.I | re.S)

REGIONES_PROB = {"Niño 1+2", "Niño 3.4"}
MAGNITUDES_PROB = {"Neutra", "Débil", "Moderada", "Fuerte", "Muy fuerte", "Extraordinaria",
                   "Fría débil", "Fría moderada", "Fría fuerte"}
CAMPOS_CORREGIBLES = {"fecha", "estado_alerta", "titular", "magnitud_nino12", "horizonte_nino12",
                      "magnitud_nino34", "horizonte_nino34", "proximo_comunicado"}


# ---------------------------------------------------------------- descarga
def pedir(url, binario=False, intentos=3):
    for i in range(1, intentos + 1):
        try:
            r = requests.get(url, headers=CABECERAS, timeout=90)
            r.raise_for_status()
            return r.content if binario else r.text
        except requests.RequestException as e:
            print(f"   Intento {i}/{intentos} falló ({url[:70]}...): {e}")
            if i < intentos:
                time.sleep(15 * i)
    return None


def listar_comunicados():
    """Devuelve {"20-2026": url_pdf, ...} con los comunicados enlazados en la portada y el listado."""
    encontrados = {}
    for pagina in (PORTADA, LISTADO):
        texto = pedir(pagina)
        if texto is None:
            continue
        for url, nro, anio in ENLACE.findall(texto):
            encontrados.setdefault(f"{int(nro)}-{anio}", html.unescape(url))
    return encontrados


def texto_pdf(contenido):
    from pypdf import PdfReader
    if not contenido.startswith(b"%PDF"):
        raise ValueError("La descarga no es un PDF (¿la web devolvió una página en su lugar?).")
    lector = PdfReader(io.BytesIO(contenido))
    return "\n".join((p.extract_text() or "") for p in lector.pages)


# ---------------------------------------------------------------- lectura del texto
def a_fecha(d, mes, a):
    try:
        return date(int(a), MESES[mes.lower()], int(d))
    except (KeyError, ValueError):
        return None


def oraciones(texto):
    plano = re.sub(r"\s+", " ", texto)
    return [o.strip() for o in re.split(r"(?<=[.;])\s+(?=[A-ZÁÉÍÓÚÑ(])", plano) if o.strip()]


def region_de(oracion):
    o = oracion.lower()
    central = bool(re.search(r"3[.,]4|pacífico\s+(?:ecuatorial\s+)?central", o))
    costero = bool(re.search(r"1\s*\+\s*2|coster[oa]|costa\s+peruana|frente\s+a\s+la\s+costa", o))
    if central and not costero:
        return "nino34"
    if costero and not central:
        return "nino12"
    return None


def capitalizar(estado):
    e = re.sub(r"\s+", " ", estado.strip().lower())
    return e[0].upper() + e[1:].replace("el niño", "El Niño").replace("la niña", "La Niña").replace("costero", "Costero").replace("costera", "Costera")


def leer_comunicado(texto, comunicado):
    nro, anio = (int(x) for x in comunicado.split("-"))
    fila = {"comunicado": comunicado, "anio": anio, "numero": nro, "fecha": None, "estado_alerta": None,
            "titular": None, "magnitud_nino12": None, "horizonte_nino12": None, "magnitud_nino34": None,
            "horizonte_nino34": None, "proximo_comunicado": None}
    plano = re.sub(r"\s+", " ", texto)

    m = FECHA.search(plano)
    if m:
        fila["fecha"] = a_fecha(*m.groups())

    tras_rotulo = re.search(r"estado\s+del?\s+sistema\s+de\s+alerta\s*:?\s*(.{0,80})", plano, re.I)
    m = ESTADO.search(tras_rotulo.group(1)) if tras_rotulo else None
    m = m or ESTADO.search(plano)
    if m:
        fila["estado_alerta"] = capitalizar(m.group(1))

    for linea in texto.splitlines():
        l = linea.strip()
        letras = [c for c in l if c.isalpha()]
        if (len(l) >= 25 and letras and sum(c.isupper() for c in letras) / len(letras) > 0.9
                and "COMUNICADO" not in l.upper()):
            fila["titular"] = re.sub(r"\s+", " ", l)
            break

    for o in oraciones(texto):
        if "magnitud" not in o.lower():
            continue
        region = region_de(o)
        if region is None or fila[f"magnitud_{region}"]:
            continue
        # La magnitud es la que sigue a la palabra "magnitud" ("magnitud extraordinaria",
        # "magnitud entre fuerte y extraordinaria"); el horizonte, el primer "hasta ..." después de ella.
        pos = o.lower().index("magnitud")
        cola = o[pos:]
        rango = re.match(rf"magnitud(?:es)?\s+(?:\w+\s+){{0,3}}?entre\s+({MAG_RE.pattern[3:-3]})\s+y\s+({MAG_RE.pattern[3:-3]})",
                         cola, re.I)
        simple = re.match(rf"magnitud(?:es)?\s+(?:\w+\s+){{0,4}}?({MAG_RE.pattern[3:-3]})\b", cola, re.I)
        if rango:
            texto_mag = f"{rango.group(1)} a {rango.group(2)}"
        elif simple:
            texto_mag = simple.group(1)
        else:
            continue
        fila[f"magnitud_{region}"] = texto_mag.lower().capitalize()
        h = HORIZONTE.search(cola[:160])
        if h:
            fila[f"horizonte_{region}"] = "hasta " + re.sub(r"\s+", " ", h.group(1))

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
               ("proximo_comunicado", "DATE"), ("revisar", "BOOL"), ("url", "STRING"), ("texto", "STRING"),
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


# ---------------------------------------------------------------- programa principal
def procesar(comunicado, url, contenido=None):
    contenido = contenido if contenido is not None else pedir(url, binario=True)
    if contenido is None:
        return None
    texto = texto_pdf(contenido)
    if len(texto.strip()) < 200:
        raise ValueError(f"El PDF del comunicado {comunicado} no tiene texto legible (¿es una imagen escaneada?).")
    fila = leer_comunicado(texto, comunicado)
    fila.update(url=url, texto=re.sub(r"[ \t]+", " ", texto).strip(), fuente=FUENTE,
                actualizado_en=datetime.now(timezone.utc))
    return fila


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--validar", action="store_true")
    ap.add_argument("--reprocesar", action="store_true", help="vuelve a leer todos los PDF de la portada")
    ap.add_argument("--pdf", help="probar con un PDF local (se trata como comunicado --numero)")
    ap.add_argument("--numero", default="99-2026")
    ap.add_argument("--sin-guardar", action="store_true")
    a = ap.parse_args()

    aviso = None
    if a.pdf:
        nuevos = [procesar(a.numero, a.pdf, open(a.pdf, "rb").read())]
        existente = None
        cliente = ds = None
    else:
        cliente, ds = destino() if not a.sin_guardar else (None, None)
        nombre_com = os.getenv("BQ_TABLA_ENFEN") or "enfen_comunicados"
        existente = None if a.sin_guardar else leer_existente(cliente, ds, nombre_com, ESQUEMA_COM)
        ya = set() if existente is None or a.reprocesar else set(existente["comunicado"])

        print(f"1) Buscando comunicados en {PORTADA}")
        enlaces = listar_comunicados()
        if not enlaces:
            aviso = "No se pudo leer la web del ENFEN (o no se encontraron enlaces); se conserva la tabla."
            print(f"   AVISO: {aviso}")
        else:
            print(f"   Encontrados: {', '.join(sorted(enlaces, key=lambda c: (c[-4:], int(c.split('-')[0]))))}")
        nuevos = []
        for com, url in sorted(enlaces.items()):
            if com in ya:
                continue
            print(f"2) Leyendo comunicado {com}")
            fila = procesar(com, url)
            if fila is None:
                aviso = f"No se pudo descargar el PDF del comunicado {com}; se intentará en la próxima ejecución."
                print(f"   AVISO: {aviso}")
                continue
            nuevos.append(fila)

    nuevos_df = pd.DataFrame(nuevos, columns=[n for n, _ in ESQUEMA_COM if n != "revisar"])
    if existente is not None and not existente.empty:
        quedan = existente[~existente["comunicado"].isin(nuevos_df["comunicado"])]
        tabla = pd.concat([quedan.drop(columns="revisar"), nuevos_df], ignore_index=True)
    else:
        tabla = nuevos_df
    if not tabla.empty:
        tabla = marcar_revision(aplicar_correcciones(tabla))
        tabla = tabla.sort_values(["anio", "numero"]).reset_index(drop=True)

        vista = tabla[["comunicado", "fecha", "estado_alerta", "magnitud_nino12", "horizonte_nino12",
                       "magnitud_nino34", "horizonte_nino34", "proximo_comunicado", "revisar"]]
        print("\nComunicados (últimos 4):")
        with pd.option_context("display.width", 250, "display.max_columns", 20):
            print(vista.tail(4).to_string(index=False))

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
                  f"Corrígelo en {CSV_CORR.name} o avísame para ajustar la lectura.")
        sin_prob = sorted(set(tabla["comunicado"].tail(1)) - set(prob["comunicado"]))
        if sin_prob:
            print(f"AVISO: el último comunicado ({sin_prob[0]}) aún no tiene probabilidades en {CSV_PROB.name}.")

    if a.sin_guardar or a.pdf:
        print("\nModo de prueba: no se guardó nada.")
    else:
        if existente is not None and len(tabla) < len(existente):
            print("ERROR: la tabla nueva tendría menos comunicados que la actual. No se escribe nada.")
            return 1
        if not tabla.empty:
            print("\nGuardado en:", guardar(cliente, ds, os.getenv("BQ_TABLA_ENFEN") or "enfen_comunicados",
                                             tabla, ESQUEMA_COM))
        if error_prob is None:
            print("Guardado en:", guardar(cliente, ds, os.getenv("BQ_TABLA_ENFEN_PROB") or "enfen_probabilidades",
                                           prob, ESQUEMA_PROB))
    if aviso:
        print(f"AVISO: {aviso}")
    return 1 if error_prob else 0


if __name__ == "__main__":
    sys.exit(main())
