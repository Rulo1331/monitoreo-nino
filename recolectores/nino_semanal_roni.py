"""
Recolector de la variable 2 del dashboard: temperatura del mar en las regiones Niño.

Genera DOS tablas independientes (no modifica la tabla del ICEN):
  1. nino_semanal : anomalías SEMANALES (OISST v2.1) en Niño 1+2, 3, 3.4 y 4.
                    Fuente: NOAA CPC wksst9120.for (actualización cada lunes).
  2. roni         : Índice Oceánico El Niño Relativo (ERSST v6), índice OFICIAL de
                    NOAA desde 2026. Fuente: NOAA CPC RONI.ascii.txt (mensual).

Uso:
  python nino_semanal_roni.py                 # descarga y guarda
  python nino_semanal_roni.py --validar       # además comprueba formato y valores
  python nino_semanal_roni.py --archivo-semanal a.txt --archivo-roni b.txt   # pruebas

Destino (mismas variables que icen.py):
  BQ_PROYECTO, BQ_DATASET (por defecto 'monitoreo_nino'),
  BQ_TABLA_SEMANAL (por defecto 'nino_semanal'), BQ_TABLA_RONI (por defecto 'roni'),
  credenciales en GCP_SA_KEY (GitHub) o GOOGLE_APPLICATION_CREDENTIALS (PC).
  Sin BQ_PROYECTO se guarda en SQLite local 'datos_nino.db'.
"""
import argparse, json, os, re, sqlite3, sys
from datetime import datetime, timezone
import pandas as pd
import requests

URL_SEMANAL = "https://www.cpc.ncep.noaa.gov/data/indices/wksst9120.for"
URL_RONI = "https://www.cpc.ncep.noaa.gov/data/indices/RONI.ascii.txt"
REGIONES = ["nino12", "nino3", "nino34", "nino4"]
TEMPORADAS = ["DJF", "JFM", "FMA", "MAM", "AMJ", "MJJ", "JJA", "JAS", "ASO", "SON", "OND", "NDJ"]

# Valores oficiales publicados por NOAA (ENSO update del 21-sep-2026) para validar
REFERENCIA_RONI = {(2026, "JJA"): 1.4}      # tabla oficial, redondeada a 1 decimal
TOLERANCIA_RONI = 0.1
MAX_DIAS_SIN_DATO = 21                       # alerta si la última semana es muy antigua

FILA_SEMANAL = re.compile(r"^\s*(\d{2}[A-Z]{3}\d{4})\s+(.*)$")
NUMERO = re.compile(r"-?\d+\.\d+")           # separa valores pegados como "23.4-0.4"


def descargar(url):
    r = requests.get(url, timeout=60, headers={"User-Agent": "monitoreo-nino/1.0 (uso interno)"})
    r.raise_for_status()
    return r.content.decode("utf-8", errors="replace")


# ---------- 1. Datos semanales ----------
def parsear_semanal(texto):
    filas = []
    for linea in texto.splitlines():
        m = FILA_SEMANAL.match(linea)
        if not m:
            continue
        valores = [float(v) for v in NUMERO.findall(m.group(2))]
        fila = {"semana": datetime.strptime(m.group(1).title(), "%d%b%Y").date()}
        if len(valores) == 8:        # formato SST + anomalía por región
            for i, reg in enumerate(REGIONES):
                fila[f"sst_{reg}"], fila[f"anom_{reg}"] = valores[2 * i], valores[2 * i + 1]
        elif len(valores) == 4:      # formato solo anomalías
            for i, reg in enumerate(REGIONES):
                fila[f"sst_{reg}"], fila[f"anom_{reg}"] = None, valores[i]
        else:
            raise ValueError(f"Línea con formato inesperado: {linea!r}")
        filas.append(fila)
    if not filas:
        raise ValueError("No se encontraron datos semanales: ¿cambió el formato del archivo?")
    df = pd.DataFrame(filas).sort_values("semana").reset_index(drop=True)
    df["fuente"] = "NOAA CPC OISST v2.1 semanal (wksst9120)"
    df["actualizado_en"] = datetime.now(timezone.utc)
    return df


# ---------- 2. RONI ----------
def fase_roni(v):
    if v >= 0.5:
        return "El Niño"
    if v <= -0.5:
        return "La Niña"
    return "Neutral"


def intensidad_roni(v):
    """Convención de intensidad usada por NOAA para El Niño / La Niña."""
    a = abs(v)
    if a < 0.5:
        return None
    if a < 1.0:
        return "Débil"
    if a < 1.5:
        return "Moderada"
    if a < 2.0:
        return "Fuerte"
    return "Muy fuerte"


def parsear_roni(texto):
    filas = []
    for linea in texto.splitlines():
        partes = linea.split()
        if len(partes) == 3 and partes[0] in TEMPORADAS:
            temporada, anio, valor = partes[0], int(partes[1]), float(partes[2])
            filas.append({"anio": anio, "temporada": temporada,
                          "mes_central": TEMPORADAS.index(temporada) + 1, "roni": valor})
    if not filas:
        raise ValueError("No se encontraron datos del RONI: ¿cambió el formato del archivo?")
    df = pd.DataFrame(filas)
    df["fase"] = df["roni"].apply(fase_roni)
    df["intensidad"] = df["roni"].apply(intensidad_roni)
    # NOAA puede revisar el RONI hasta 2 meses después: las 2 últimas temporadas son provisionales
    df["provisional"] = df.index >= len(df) - 2
    df["fuente"] = "NOAA CPC RONI (ERSST v6)"
    df["actualizado_en"] = datetime.now(timezone.utc)
    return df[["anio", "mes_central", "temporada", "roni", "fase", "intensidad",
               "provisional", "fuente", "actualizado_en"]]


# ---------- Validación ----------
def validar(sem, roni):
    ok = True
    print("\nValidación:")
    anomalias = sem[[f"anom_{r}" for r in REGIONES]]
    rango_ok = anomalias.abs().max().max() < 8
    print(f"  Semanal: {len(sem)} semanas, rango físico de anomalías {'OK' if rango_ok else 'REVISAR'}")
    ok &= bool(rango_ok)
    pasos = sem["semana"].diff().dropna().apply(lambda d: d.days)
    pasos_ok = bool((pasos == 7).all())
    print(f"  Semanal: semanas consecutivas cada 7 días {'OK' if pasos_ok else 'REVISAR'}")
    ok &= pasos_ok
    dias = (datetime.now(timezone.utc).date() - sem["semana"].iloc[-1]).days
    fresco = dias <= MAX_DIAS_SIN_DATO
    print(f"  Semanal: última semana {sem['semana'].iloc[-1]} (hace {dias} días) "
          f"{'OK' if fresco else 'DESACTUALIZADO'}")
    ok &= fresco
    for (anio, temp), oficial in REFERENCIA_RONI.items():
        fila = roni[(roni.anio == anio) & (roni.temporada == temp)]
        calc = fila["roni"].iloc[0] if len(fila) else float("nan")
        bien = abs(calc - oficial) <= TOLERANCIA_RONI
        print(f"  RONI {temp} {anio}: archivo {calc:+.2f} | oficial {oficial:+.1f} -> "
              f"{'OK' if bien else 'REVISAR'}")
        ok &= bool(bien)
    return ok


# ---------- Guardado ----------
def cliente_bigquery(proyecto):
    from google.cloud import bigquery
    clave = os.getenv("GCP_SA_KEY")
    if clave:
        from google.oauth2 import service_account
        cred = service_account.Credentials.from_service_account_info(json.loads(clave))
        return bigquery.Client(project=proyecto, credentials=cred)
    return bigquery.Client(project=proyecto)


def ref_dataset(proyecto):
    ds = os.getenv("BQ_DATASET") or "monitoreo_nino"
    return ds if "." in ds else f"{proyecto}.{ds}"


def guardar(df, tabla_env, tabla_defecto, esquema_def):
    nombre = os.getenv(tabla_env) or tabla_defecto
    proyecto = os.getenv("BQ_PROYECTO")
    if not proyecto:
        with sqlite3.connect("datos_nino.db") as con:
            df.to_sql(nombre, con, if_exists="replace", index=False)
        return f"SQLite local 'datos_nino.db', tabla '{nombre}'"

    from google.cloud import bigquery
    from google.api_core.exceptions import NotFound
    esquema = [bigquery.SchemaField(n, t) for n, t in esquema_def]
    cliente = cliente_bigquery(proyecto)
    ds_ref = ref_dataset(proyecto)
    try:
        dataset = cliente.get_dataset(ds_ref)
    except NotFound:
        existentes = [d.dataset_id for d in cliente.list_datasets(ds_ref.split(".")[0])]
        print(f"\nERROR: no existe el dataset indicado. Datasets disponibles: {existentes or 'ninguno'}")
        sys.exit(1)
    tabla = f"{ds_ref}.{nombre}"
    try:  # protección: no sobrescribir una tabla ajena con otra estructura
        actuales = {c.name for c in cliente.get_table(tabla).schema}
        if actuales != {n for n, _ in esquema_def}:
            print(f"\nERROR: la tabla '{tabla}' ya existe con otra estructura y NO se sobrescribirá.")
            sys.exit(1)
    except NotFound:
        pass
    config = bigquery.LoadJobConfig(schema=esquema, write_disposition="WRITE_TRUNCATE")
    cliente.load_table_from_dataframe(df, tabla, job_config=config,
                                      location=dataset.location).result()
    return f"BigQuery {tabla} ({cliente.get_table(tabla).num_rows} filas)"


ESQUEMA_SEMANAL = [("semana", "DATE")] + [
    (f"{p}_{r}", "FLOAT64") for r in REGIONES for p in ("sst", "anom")
] + [("fuente", "STRING"), ("actualizado_en", "TIMESTAMP")]
ESQUEMA_RONI = [("anio", "INT64"), ("mes_central", "INT64"), ("temporada", "STRING"),
                ("roni", "FLOAT64"), ("fase", "STRING"), ("intensidad", "STRING"),
                ("provisional", "BOOL"), ("fuente", "STRING"), ("actualizado_en", "TIMESTAMP")]


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--archivo-semanal")
    ap.add_argument("--archivo-roni")
    ap.add_argument("--validar", action="store_true")
    ap.add_argument("--sin-guardar", action="store_true")
    a = ap.parse_args()

    leer = lambda ruta, url: open(ruta, encoding="utf-8").read() if ruta else descargar(url)
    semanal = parsear_semanal(leer(a.archivo_semanal, URL_SEMANAL))
    roni = parsear_roni(leer(a.archivo_roni, URL_RONI))
    # Columnas en el orden del esquema
    semanal = semanal[[n for n, _ in ESQUEMA_SEMANAL]]

    print("Últimas semanas (anomalías °C):")
    print(semanal[["semana"] + [f"anom_{r}" for r in REGIONES]].tail(4).to_string(index=False))
    print("\nÚltimas temporadas del RONI:")
    print(roni[["anio", "temporada", "roni", "fase", "intensidad", "provisional"]]
          .tail(4).to_string(index=False))

    if a.validar and not validar(semanal, roni):
        sys.exit(1)
    if not a.sin_guardar:
        print("\nGuardado en:", guardar(semanal, "BQ_TABLA_SEMANAL", "nino_semanal", ESQUEMA_SEMANAL))
        print("Guardado en:", guardar(roni, "BQ_TABLA_RONI", "roni", ESQUEMA_RONI))
