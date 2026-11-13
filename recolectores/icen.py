"""
Recolector de la variable 1: Índice Costero El Niño (ICEN).

Fuente principal: archivo oficial del IGP/ENFEN
    http://met.igp.gob.pe/datos/ICEN.txt
(es el enlace de descarga de la página SIOFEN de IMARPE).

Respaldo manual: datos/icen_manual.csv
    Solo se usa para los meses que el archivo del IGP todavía no trae.

Qué hace en cada ejecución:
 1. Descarga ICEN.txt y lo valida (formato, meses continuos, rango de valores).
 2. Lee los valores cargados a mano (si hay).
 3. Lee la tabla actual de BigQuery para conservar las columnas que el IGP no
    publica (anom_nino12 y anom_nino34, que venían de NOAA ERSSTv5).
 4. Arma la serie completa, clasifica cada mes con los umbrales del ENFEN y
    reemplaza la tabla.

Si el IGP no responde, no borra nada: conserva el ICEN que ya estaba en la
tabla, aplica los valores manuales y termina con un AVISO (código 0).

Uso:
    python recolectores/icen.py --validar      # valida y guarda (lo que corre GitHub)
    python recolectores/icen.py --no-guardar   # solo muestra el resultado

Variables de entorno (las mismas de antes):
    GCP_SA_KEY      JSON de la cuenta de servicio (secret de GitHub)
    BQ_PROYECTO     proyecto de BigQuery
    BQ_DATASET      dataset (puede ser "otro-proyecto.dataset")
    BQ_TABLA_ICEN   nombre de la tabla (por defecto "icen")
"""

import argparse
import datetime as dt
import io
import json
import os
import sys
import time
from pathlib import Path

import pandas as pd
import requests

# ----------------------------------------------------------------------------
# Configuración
# ----------------------------------------------------------------------------
URL_IGP = os.environ.get("ICEN_URL", "http://met.igp.gob.pe/datos/ICEN.txt")
RUTA_MANUAL = Path(__file__).resolve().parent.parent / "datos" / "icen_manual.csv"

FUENTE_IGP = "IGP/ENFEN ICEN.txt (oficial)"
FUENTE_MANUAL = "Manual"

# Umbrales ENFEN para condiciones cálidas (°C).
# UMBRAL_EXTRAORDINARIA = 3.0 es provisional: confirmarlo en la Nota Técnica
# ENFEN 01-2024. Si encuentras el valor exacto, cambia solo este número.
UMBRAL_NEUTRA_FRIA = -0.7
UMBRAL_DEBIL = 0.5
UMBRAL_MODERADA = 1.3
UMBRAL_FUERTE = 2.1
UMBRAL_EXTRAORDINARIA = 3.0

MESES_PROVISIONALES = 2      # últimos meses que aún pueden corregirse
MINIMO_FILAS_IGP = 900       # desde 1950 deben ser > 900 meses
RANGO_VALIDO = (-4.0, 6.5)   # fuera de esto, el archivo cambió o está dañado

# Valor oficial publicado por el ENFEN, para comprobar que leemos bien el archivo
# (Informe Técnico SENAMHI-ENFEN N° 06-2026: ICEN de mayo 2026 = +1,98).
CONTROL_OFICIAL = {(2026, 5): 1.98}

COLUMNAS = ["anio", "mes", "anom_nino12", "icen", "categoria", "provisional",
            "anom_nino34", "fuente", "actualizado_en"]


# ----------------------------------------------------------------------------
# 1. Descarga
# ----------------------------------------------------------------------------
def descargar(url: str, intentos: int = 3) -> str | None:
    """Devuelve el texto del archivo o None si no se pudo descargar."""
    cabeceras = {"User-Agent": "monitoreo-fen-elrocio/1.0 (descarga semanal del ICEN)"}
    for i in range(1, intentos + 1):
        try:
            r = requests.get(url, headers=cabeceras, timeout=60)
            r.raise_for_status()
            r.encoding = r.encoding or "utf-8"
            return r.text
        except requests.RequestException as e:
            print(f"   Intento {i}/{intentos} falló: {e}")
            if i < intentos:
                time.sleep(20 * i)
    return None


# ----------------------------------------------------------------------------
# 2. Lectura y validación del archivo del IGP
# ----------------------------------------------------------------------------
def parsear_igp(texto: str) -> pd.DataFrame:
    """Convierte ICEN.txt en una tabla anio, mes, icen. Falla con mensaje claro
    si el formato cambió."""
    cabecera = [l for l in texto.splitlines() if l.strip().startswith("%")]
    if not any("ICEN" in l for l in cabecera):
        raise ValueError("El archivo no tiene la cabecera '% ... ICEN'. "
                         "¿El IGP cambió el formato o devolvió una página de error?")

    filas = []
    for n, linea in enumerate(texto.splitlines(), start=1):
        s = linea.strip()
        if not s or s.startswith("%"):
            continue
        partes = s.split()
        if len(partes) != 3:
            raise ValueError(f"Línea {n} con formato inesperado: '{s}'")
        try:
            filas.append((int(partes[0]), int(partes[1]), float(partes[2])))
        except ValueError:
            raise ValueError(f"Línea {n} no es 'año mes valor': '{s}'")

    df = pd.DataFrame(filas, columns=["anio", "mes", "icen"])

    if len(df) < MINIMO_FILAS_IGP:
        raise ValueError(f"Solo {len(df)} meses; se esperaban más de {MINIMO_FILAS_IGP}.")
    if df["anio"].iloc[0] != 1950 or df["mes"].iloc[0] != 1:
        raise ValueError("La serie no empieza en enero de 1950.")
    if not df["mes"].between(1, 12).all():
        raise ValueError("Hay meses fuera de 1-12.")
    if df.duplicated(["anio", "mes"]).any():
        raise ValueError("Hay meses repetidos.")
    indice = df["anio"] * 12 + df["mes"]
    if not (indice.diff().dropna() == 1).all():
        raise ValueError("La serie tiene huecos o está desordenada.")
    fuera = df[~df["icen"].between(*RANGO_VALIDO)]
    if not fuera.empty:
        raise ValueError(f"Valores fuera de rango:\n{fuera.to_string(index=False)}")

    for (a, m), oficial in CONTROL_OFICIAL.items():
        fila = df[(df.anio == a) & (df.mes == m)]
        if not fila.empty and abs(fila["icen"].iloc[0] - oficial) > 0.05:
            raise ValueError(f"ICEN {a}-{m:02d} = {fila['icen'].iloc[0]} no coincide "
                             f"con el oficial {oficial}.")
    return df


# ----------------------------------------------------------------------------
# 3. Valores manuales
# ----------------------------------------------------------------------------
def leer_manual(ruta: Path) -> pd.DataFrame:
    vacio = pd.DataFrame({"anio": pd.Series(dtype="int64"), "mes": pd.Series(dtype="int64"),
                          "icen": pd.Series(dtype="float64"), "nota": pd.Series(dtype="object")})
    if not ruta.exists():
        return vacio
    df = pd.read_csv(ruta, comment="#", skip_blank_lines=True)
    if df.empty:
        return vacio
    faltan = {"anio", "mes", "icen"} - set(df.columns)
    if faltan:
        raise ValueError(f"{ruta.name}: faltan las columnas {sorted(faltan)}.")
    if "nota" not in df.columns:
        df["nota"] = ""
    df = df.dropna(subset=["anio", "mes", "icen"])
    df["anio"] = df["anio"].astype(int)
    df["mes"] = df["mes"].astype(int)
    df["icen"] = df["icen"].astype(float)
    df["nota"] = df["nota"].fillna("").astype(str)
    if not df["mes"].between(1, 12).all() or not df["icen"].between(*RANGO_VALIDO).all():
        raise ValueError(f"{ruta.name}: hay un mes o un valor fuera de rango.")
    if df.duplicated(["anio", "mes"]).any():
        raise ValueError(f"{ruta.name}: hay meses repetidos.")
    return df[["anio", "mes", "icen", "nota"]]


# ----------------------------------------------------------------------------
# 4. Combinación y clasificación
# ----------------------------------------------------------------------------
def clasificar(v) -> str | None:
    if pd.isna(v):
        return None
    if v < UMBRAL_NEUTRA_FRIA:
        return "Fría"
    if v <= UMBRAL_DEBIL:
        return "Neutra"
    if v <= UMBRAL_MODERADA:
        return "Cálida débil"
    if v <= UMBRAL_FUERTE:
        return "Cálida moderada"
    if UMBRAL_EXTRAORDINARIA is None or v <= UMBRAL_EXTRAORDINARIA:
        return "Cálida fuerte"
    return "Cálida extraordinaria"


def combinar(igp: pd.DataFrame | None, manual: pd.DataFrame,
             existente: pd.DataFrame | None) -> pd.DataFrame:
    """Prioridad del ICEN: IGP > manual > valor que ya estaba en la tabla."""
    partes = [d[["anio", "mes"]] for d in (igp, manual, existente)
              if d is not None and not d.empty]
    if not partes:
        raise ValueError("No hay ningún dato de ICEN (ni IGP, ni manual, ni tabla previa).")
    meses = pd.concat(partes).drop_duplicates()
    ini = int(meses.assign(i=meses.anio * 12 + meses.mes - 1)["i"].min())
    fin = int(meses.assign(i=meses.anio * 12 + meses.mes - 1)["i"].max())
    base = pd.DataFrame({"i": range(ini, fin + 1)})
    base["anio"] = base["i"] // 12
    base["mes"] = base["i"] % 12 + 1
    base = base.drop(columns="i")

    if existente is not None and not existente.empty:
        prev = existente[["anio", "mes", "anom_nino12", "anom_nino34", "icen", "fuente"]]
        prev = prev.rename(columns={"icen": "icen_prev", "fuente": "fuente_prev"})
        base = base.merge(prev, on=["anio", "mes"], how="left")
    else:
        base[["anom_nino12", "anom_nino34", "icen_prev"]] = float("nan")
        base["fuente_prev"] = None

    if igp is not None:
        base = base.merge(igp.rename(columns={"icen": "icen_igp"}), on=["anio", "mes"], how="left")
    else:
        base["icen_igp"] = float("nan")
    base = base.merge(manual.rename(columns={"icen": "icen_man"}), on=["anio", "mes"], how="left")
    for c in ("icen_igp", "icen_man", "icen_prev", "anom_nino12", "anom_nino34"):
        base[c] = pd.to_numeric(base[c], errors="coerce").astype("float64")
    base["nota"] = base["nota"].fillna("").astype(str)

    base["icen"] = base["icen_igp"]
    base["fuente"] = FUENTE_IGP
    base.loc[base["icen_igp"].isna(), "fuente"] = None

    usar_man = base["icen"].isna() & base["icen_man"].notna()
    base.loc[usar_man, "icen"] = base.loc[usar_man, "icen_man"]
    base.loc[usar_man, "fuente"] = (FUENTE_MANUAL + ": " + base.loc[usar_man, "nota"]).str.rstrip(": ")

    usar_prev = base["icen"].isna() & base["icen_prev"].notna()
    base.loc[usar_prev, "icen"] = base.loc[usar_prev, "icen_prev"]
    base.loc[usar_prev, "fuente"] = base.loc[usar_prev, "fuente_prev"]

    # Si un mes nuevo no tiene ningún valor, igual queda la fila (con nulos)
    base["fuente"] = base["fuente"].fillna("Sin ICEN")

    base["categoria"] = base["icen"].map(clasificar)

    # Provisionales: los últimos meses con ICEN y todo lo cargado a mano
    con_icen = base.index[base["icen"].notna()]
    base["provisional"] = False
    base.loc[con_icen[-MESES_PROVISIONALES:], "provisional"] = True
    base.loc[usar_man, "provisional"] = True

    base["icen"] = base["icen"].round(2)
    base["actualizado_en"] = pd.Timestamp.now(tz="UTC")
    return base[COLUMNAS].reset_index(drop=True)


# ----------------------------------------------------------------------------
# 5. BigQuery
# ----------------------------------------------------------------------------
def cliente_y_tabla():
    from google.cloud import bigquery
    from google.oauth2 import service_account

    clave = os.environ.get("GCP_SA_KEY")
    if clave:
        info = json.loads(clave)
        cred = service_account.Credentials.from_service_account_info(info)
        proyecto = os.environ.get("BQ_PROYECTO") or info.get("project_id")
        cliente = bigquery.Client(project=proyecto, credentials=cred)
    else:  # GOOGLE_APPLICATION_CREDENTIALS
        proyecto = os.environ.get("BQ_PROYECTO")
        cliente = bigquery.Client(project=proyecto)

    dataset = os.environ.get("BQ_DATASET") or "monitoreo_nino"
    tabla = os.environ.get("BQ_TABLA_ICEN") or "icen"
    if "." not in dataset:
        dataset = f"{cliente.project}.{dataset}"
    return cliente, f"{dataset}.{tabla}"


def leer_existente(cliente, tabla_id) -> pd.DataFrame | None:
    from google.api_core.exceptions import NotFound
    try:
        tabla = cliente.get_table(tabla_id)
    except NotFound:
        return None
    columnas = {c.name for c in tabla.schema}
    if columnas != set(COLUMNAS):
        raise RuntimeError(f"La tabla {tabla_id} existe con otras columnas {sorted(columnas)}. "
                           "No se escribe nada para no dañar datos ajenos.")
    return cliente.list_rows(tabla).to_dataframe()


def guardar(cliente, tabla_id, df: pd.DataFrame) -> str:
    from google.cloud import bigquery
    esquema = [
        bigquery.SchemaField("anio", "INTEGER"),
        bigquery.SchemaField("mes", "INTEGER"),
        bigquery.SchemaField("anom_nino12", "FLOAT"),
        bigquery.SchemaField("icen", "FLOAT"),
        bigquery.SchemaField("categoria", "STRING"),
        bigquery.SchemaField("provisional", "BOOLEAN"),
        bigquery.SchemaField("anom_nino34", "FLOAT"),
        bigquery.SchemaField("fuente", "STRING"),
        bigquery.SchemaField("actualizado_en", "TIMESTAMP"),
    ]
    config = bigquery.LoadJobConfig(schema=esquema, write_disposition="WRITE_TRUNCATE")
    cliente.load_table_from_dataframe(df, tabla_id, job_config=config).result()
    return f"BigQuery {tabla_id} ({len(df)} filas)"


# ----------------------------------------------------------------------------
# Programa principal
# ----------------------------------------------------------------------------
def resumen(df: pd.DataFrame, n: int = 6) -> str:
    return df.dropna(subset=["icen"]).tail(n)[
        ["anio", "mes", "icen", "categoria", "provisional", "fuente"]].to_string(index=False)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--validar", action="store_true", help="muestra controles adicionales")
    ap.add_argument("--no-guardar", action="store_true", help="no escribe en BigQuery")
    args = ap.parse_args()

    print(f"1) Descargando {URL_IGP}")
    texto = descargar(URL_IGP)
    igp = None
    aviso = None
    if texto is None:
        aviso = "No se pudo descargar el archivo del IGP; se conserva el ICEN previo."
        print(f"   AVISO: {aviso}")
    else:
        igp = parsear_igp(texto)  # si el formato cambió, falla aquí sin escribir nada
        u = igp.iloc[-1]
        print(f"   OK: {len(igp)} meses, último {int(u.anio)}-{int(u.mes):02d} = {u.icen:+.2f}")

    print(f"2) Valores manuales: {RUTA_MANUAL.name}")
    manual = leer_manual(RUTA_MANUAL)
    print(f"   {len(manual)} fila(s)")

    cliente = tabla_id = None
    existente = None
    if not args.no_guardar:
        cliente, tabla_id = cliente_y_tabla()
        print(f"3) Leyendo tabla actual {tabla_id}")
        existente = leer_existente(cliente, tabla_id)
        print(f"   {0 if existente is None else len(existente)} filas previas")

    final = combinar(igp, manual, existente)

    if args.validar and existente is not None and igp is not None:
        comp = existente.merge(igp, on=["anio", "mes"], suffixes=("_prev", "_igp"))
        comp = comp.dropna(subset=["icen_prev"])
        if not comp.empty:
            dif = (comp["icen_prev"] - comp["icen_igp"]).abs()
            print(f"   Control vs. cálculo anterior (NOAA v5): {len(comp)} meses, "
                  f"diferencia media {dif.mean():.3f} °C, máxima {dif.max():.2f} °C")

    # Protección: nunca guardar menos meses con ICEN que los que ya había
    if existente is not None:
        antes = existente["icen"].notna().sum()
        ahora = final["icen"].notna().sum()
        if ahora < antes:
            print(f"ERROR: la nueva serie tiene {ahora} meses con ICEN y la tabla {antes}. "
                  "No se escribe nada.")
            return 1

    print("4) Últimos meses:")
    print(resumen(final))

    if args.no_guardar:
        print("Modo --no-guardar: no se escribió en BigQuery.")
    else:
        print("Guardado en:", guardar(cliente, tabla_id, final))

    if aviso:
        print(f"AVISO: {aviso}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
