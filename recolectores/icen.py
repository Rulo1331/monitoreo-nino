"""
Recolector del Índice Costero El Niño (ICEN) — Variable 1 del dashboard.

Definición oficial (ENFEN, Nota Técnica 01-2024): media móvil de 3 meses de las
anomalías mensuales de TSM en la región Niño 1+2 (ERSSTv5), climatología 1991-2020.

Fuente: NOAA CPC publica esas anomalías mensuales en texto plano y permite
acceso automatizado. Calculamos el ICEN nosotros y lo contrastamos con el
valor oficial del ENFEN (que prevalece ante cualquier discrepancia).

Uso:
  python icen.py                       # descarga, calcula y guarda
  python icen.py --archivo muestra.txt # usa un archivo local (pruebas)
  python icen.py --validar             # compara con valores oficiales conocidos

Destino de los datos:
  - Si existe la variable BQ_PROYECTO  -> BigQuery (tabla <BQ_PROYECTO>.<BQ_DATASET>.<BQ_TABLA_ICEN>)
      BQ_DATASET    (opcional, por defecto 'monitoreo_nino'; admite 'otro_proyecto.dataset')
      BQ_TABLA_ICEN (opcional, por defecto 'icen')
      Credenciales: GCP_SA_KEY (contenido JSON, usado en GitHub Actions) o
      GOOGLE_APPLICATION_CREDENTIALS (ruta al archivo JSON, usado en tu PC).
  - Si no existe                       -> SQLite local 'datos_nino.db' (pruebas).
"""
import argparse, json, os, re, sqlite3, sys
from datetime import datetime, timezone
import numpy as np
import pandas as pd
import requests

URL_NOAA = ("https://www.cpc.ncep.noaa.gov/products/GODAS/multiora/index/"
            "mnth.ersstv5.clim19912020.nino_current.txt")
FILA = re.compile(r"^\s*(\d{4})\s+(\d{1,2})\s+(\S+)\s+(\S+)\s+(\S+)\s+(\S+)\s*$")

# Valores oficiales publicados, para validar el cálculo (fuente: Informe Técnico ENFEN 06-2026)
REFERENCIAS_OFICIALES = {(2026, 5): 1.98}   # ICEN centrado en mayo 2026
TOLERANCIA = 0.05                            # °C (diferencias por redondeo de NOAA)

# Umbrales ENFEN (Nota Técnica 01-2024). Neutra, débil y moderada están confirmados
# en los comunicados; revisa en la Nota Técnica el límite de "extraordinaria".
UMBRAL_FRIA = -0.7
UMBRAL_DEBIL = 0.5
UMBRAL_MODERADA = 1.3
UMBRAL_FUERTE = 2.1
UMBRAL_EXTRAORDINARIA = None   # completar con el valor de la Nota Técnica 01-2024


def descargar(url=URL_NOAA):
    r = requests.get(url, timeout=60, headers={"User-Agent": "monitoreo-nino/1.0 (uso interno)"})
    r.raise_for_status()
    return r.text


def parsear(texto):
    """Extrae año, mes y anomalías Niño 1+2 y Niño 3.4 del archivo de NOAA."""
    filas = []
    for linea in texto.splitlines():
        m = FILA.match(linea)
        if not m:
            continue  # encabezados y líneas vacías
        anio, mes, _n4, n34, _n3, n12 = m.groups()
        num = lambda v: np.nan if "*" in v else float(v)   # '********' = mes sin dato
        filas.append({"anio": int(anio), "mes": int(mes),
                      "anom_nino12": num(n12), "anom_nino34": num(n34)})
    if not filas:
        raise ValueError("No se encontraron datos: ¿cambió el formato del archivo?")
    df = pd.DataFrame(filas)
    df["periodo"] = pd.PeriodIndex.from_fields(year=df.anio, month=df.mes, freq="M")
    # Serie mensual continua: los huecos quedan como NaN y no contaminan la media móvil
    completo = pd.period_range(df.periodo.min(), df.periodo.max(), freq="M")
    return df.set_index("periodo").reindex(completo).rename_axis("periodo")


def categoria(icen):
    if pd.isna(icen):
        return None
    if icen < UMBRAL_FRIA:
        return "Fría"
    if icen <= UMBRAL_DEBIL:
        return "Neutra"
    if icen <= UMBRAL_MODERADA:
        return "Cálida débil"
    if icen <= UMBRAL_FUERTE:
        return "Cálida moderada"
    if UMBRAL_EXTRAORDINARIA is not None and icen > UMBRAL_EXTRAORDINARIA:
        return "Cálida extraordinaria"
    return "Cálida fuerte"


def calcular_icen(df):
    df = df.copy()
    # Media móvil CENTRADA: el ICEN de mayo usa abril, mayo y junio
    df["icen"] = df["anom_nino12"].rolling(3, center=True, min_periods=3).mean().round(2)
    df["categoria"] = df["icen"].apply(categoria)
    # ERSSTv5 puede revisar los 2 últimos meses: se marcan como provisionales
    ultimo = df["icen"].last_valid_index()
    df["provisional"] = False
    if ultimo is not None:
        df.loc[df.index >= ultimo - 1, "provisional"] = True
    df["anio"] = df.index.year
    df["mes"] = df.index.month
    df["fuente"] = "NOAA CPC ERSSTv5 (calculado)"
    df["actualizado_en"] = datetime.now(timezone.utc)
    return df.reset_index(drop=True)[["anio", "mes", "anom_nino12", "icen", "categoria",
                                       "provisional", "anom_nino34", "fuente", "actualizado_en"]]


def validar(df):
    ok = True
    for (anio, mes), oficial in REFERENCIAS_OFICIALES.items():
        fila = df[(df.anio == anio) & (df.mes == mes)]
        calc = fila["icen"].iloc[0] if len(fila) else np.nan
        dif = abs(calc - oficial)
        estado = "OK" if dif <= TOLERANCIA else "REVISAR"
        ok &= estado == "OK"
        print(f"  {anio}-{mes:02d}: calculado {calc:+.2f} | oficial {oficial:+.2f} | dif {dif:.2f} -> {estado}")
    return ok


def cliente_bigquery(proyecto):
    from google.cloud import bigquery
    clave = os.getenv("GCP_SA_KEY")
    if clave:  # GitHub Actions: el JSON completo viene en un secret
        from google.oauth2 import service_account
        cred = service_account.Credentials.from_service_account_info(json.loads(clave))
        return bigquery.Client(project=proyecto, credentials=cred)
    return bigquery.Client(project=proyecto)  # tu PC: usa GOOGLE_APPLICATION_CREDENTIALS


def destino_bigquery(proyecto):
    """Arma la ruta de la tabla a partir de variables de entorno.
    BQ_DATASET acepta 'dataset' (mismo proyecto) o 'otro_proyecto.dataset'."""
    ds = os.getenv("BQ_DATASET") or "monitoreo_nino"
    ref_dataset = ds if "." in ds else f"{proyecto}.{ds}"
    tabla = os.getenv("BQ_TABLA_ICEN") or "icen"
    return ref_dataset, f"{ref_dataset}.{tabla}"


def verificar_dataset(cliente, ref_dataset):
    """Comprueba que el dataset exista y, si no, muestra qué datasets hay en ese proyecto."""
    from google.api_core.exceptions import NotFound
    try:
        return cliente.get_dataset(ref_dataset)
    except NotFound:
        proyecto_ds, nombre = ref_dataset.split(".", 1)
        existentes = [d.dataset_id for d in cliente.list_datasets(proyecto_ds)]
        print(f"\nERROR: no existe el dataset '{nombre}' en el proyecto indicado.")
        print(f"Datasets que SÍ existen en ese proyecto: {existentes or 'ninguno'}")
        print("Revisa BQ_PROYECTO (ID, no nombre) y la variable BQ_DATASET.")
        sys.exit(1)


def verificar_tabla_ajena(cliente, tabla, esquema):
    """Protección: no sobrescribir una tabla existente que tenga otra estructura
    (por ejemplo, una tabla creada por otra persona con el mismo nombre)."""
    from google.api_core.exceptions import NotFound
    try:
        existente = cliente.get_table(tabla)
    except NotFound:
        return  # no existe: se creará
    esperadas = {c.name for c in esquema}
    actuales = {c.name for c in existente.schema}
    if actuales != esperadas:
        print(f"\nERROR: la tabla '{tabla}' ya existe con otra estructura y NO se sobrescribirá.")
        print(f"Columnas actuales: {sorted(actuales)}")
        print("Usa otro nombre de tabla en la variable BQ_TABLA_ICEN.")
        sys.exit(1)


def guardar(df):
    df = df.dropna(subset=["anom_nino12"]).copy()
    proyecto = os.getenv("BQ_PROYECTO")
    if not proyecto:
        with sqlite3.connect("datos_nino.db") as con:
            df.to_sql("icen", con, if_exists="replace", index=False)
        return "SQLite local 'datos_nino.db'"

    from google.cloud import bigquery
    ref_dataset, tabla = destino_bigquery(proyecto)
    esquema = [
        bigquery.SchemaField("anio", "INT64"), bigquery.SchemaField("mes", "INT64"),
        bigquery.SchemaField("anom_nino12", "FLOAT64"), bigquery.SchemaField("icen", "FLOAT64"),
        bigquery.SchemaField("categoria", "STRING"), bigquery.SchemaField("provisional", "BOOL"),
        bigquery.SchemaField("anom_nino34", "FLOAT64"), bigquery.SchemaField("fuente", "STRING"),
        bigquery.SchemaField("actualizado_en", "TIMESTAMP"),
    ]
    cliente = cliente_bigquery(proyecto)
    dataset = verificar_dataset(cliente, ref_dataset)
    verificar_tabla_ajena(cliente, tabla, esquema)
    # WRITE_TRUNCATE reemplaza la tabla completa: absorbe las revisiones de NOAA sin duplicar filas
    config = bigquery.LoadJobConfig(schema=esquema, write_disposition="WRITE_TRUNCATE")
    # La carga se ejecuta en la misma ubicación del dataset (US, southamerica-west1, etc.)
    cliente.load_table_from_dataframe(df, tabla, job_config=config,
                                      location=dataset.location).result()
    return f"BigQuery {tabla} ({cliente.get_table(tabla).num_rows} filas)"


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--archivo", help="leer un archivo local en vez de descargar")
    ap.add_argument("--validar", action="store_true")
    ap.add_argument("--sin-guardar", action="store_true")
    a = ap.parse_args()

    texto = open(a.archivo, encoding="utf-8").read() if a.archivo else descargar()
    datos = calcular_icen(parsear(texto))
    print(datos.dropna(subset=["anom_nino12"]).tail(8).drop(columns=["fuente", "actualizado_en"])
          .to_string(index=False))
    if a.validar:
        print("\nValidación contra valores oficiales ENFEN:")
        if not validar(datos):
            sys.exit(1)
    if not a.sin_guardar:
        print(f"\nGuardado en: {guardar(datos)}")
