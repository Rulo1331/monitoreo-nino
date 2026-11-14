"""
Recolector: calor bajo la superficie del Pacífico ecuatorial (temperatura media de los primeros 300 m).

Qué mide: la anomalía de la temperatura promedio entre la superficie y 300 m de profundidad, a lo
largo del Pacífico ecuatorial. Es la "reserva de calor" que las ondas Kelvin llevan hacia la costa
sudamericana; suele adelantarse 1 a 3 meses a lo que luego se ve en la superficie (Niño 1+2, ICEN).

Fuente: NOAA CPC, análisis oceánico GODAS (mensual, desde 1979, climatología 1981-2010)
    https://www.cpc.ncep.noaa.gov/products/analysis_monitoring/ocean/index/heat_content_index.txt

Columnas del archivo (franjas de longitud, siempre entre 5°N y 5°S):
    130E-80W  : todo el Pacífico ecuatorial
    160E-80W  : centro y oriente
    180W-100W : centro-oriente (la franja que NOAA usa para seguir El Niño)

Tabla: calor_subsuperficial (se reemplaza completa en cada ejecución, como icen y roni).

Uso:
    python recolectores/calor_subsuperficial.py --validar
    python recolectores/calor_subsuperficial.py --archivo prueba.txt --sin-guardar
"""
import argparse
import json
import os
import sqlite3
import sys
from datetime import datetime, timezone

import pandas as pd
import requests

URL = "https://www.cpc.ncep.noaa.gov/products/analysis_monitoring/ocean/index/heat_content_index.txt"
FUENTE = "NOAA CPC GODAS, anomalía 0-300 m (clim. 1981-2010)"
FRANJAS = {"130E-80W": "t300_130e_80w", "160E-80W": "t300_160e_80w", "180W-100W": "t300_180w_100w"}
MESES_PROVISIONALES = 2          # GODAS puede reajustar los meses más recientes
MAX_DIAS_SIN_DATO = 75           # el dato de un mes sale a inicios del mes siguiente
RANGO = (-5.0, 5.0)              # anomalías físicamente razonables (°C)

# Control contra el valor leído al construir el recolector (14-nov-2026). Si NOAA revisa la serie
# más de lo tolerado, la validación avisa para revisar el archivo en vez de guardar a ciegas.
CONTROL = {(2026, 9, "t300_180w_100w"): 3.39}
TOLERANCIA = 0.25


def descargar(url):
    r = requests.get(url, timeout=60, headers={"User-Agent": "monitoreo-nino/1.0 (uso interno)"})
    r.raise_for_status()
    return r.content.decode("utf-8", errors="replace")


def a_numero(token):
    """Convierte un valor del archivo; los códigos de dato faltante (-999, -99.9, ***) quedan vacíos."""
    try:
        v = float(token)
    except ValueError:
        return None
    return None if v <= -99 else v


def parsear(texto):
    lineas = texto.splitlines()
    cabecera = next((i for i, l in enumerate(lineas) if l.split()[:2] == ["YR", "MON"]), None)
    if cabecera is None:
        raise ValueError("No se encontró la fila de títulos 'YR MON ...': ¿cambió el formato del archivo?")
    franjas = lineas[cabecera].split()[2:]
    if franjas != list(FRANJAS):
        raise ValueError(f"Columnas inesperadas {franjas}; se esperaban {list(FRANJAS)}.")
    if "300" not in " ".join(lineas[:cabecera]):
        raise ValueError("La descripción del archivo ya no menciona los 300 m: revisar la fuente.")

    filas = []
    for n, linea in enumerate(lineas[cabecera + 1:], start=cabecera + 2):
        partes = linea.split()
        if not partes:
            continue
        if len(partes) != 2 + len(FRANJAS):
            raise ValueError(f"Línea {n} con formato inesperado: {linea!r}")
        fila = {"anio": int(partes[0]), "mes": int(partes[1])}
        for col, token in zip(FRANJAS.values(), partes[2:]):
            fila[col] = a_numero(token)
        filas.append(fila)
    if not filas:
        raise ValueError("El archivo no trae datos.")

    df = pd.DataFrame(filas).sort_values(["anio", "mes"]).reset_index(drop=True)
    df = df.dropna(subset=list(FRANJAS.values()), how="all")
    df["provisional"] = df.index >= len(df) - MESES_PROVISIONALES
    df["fuente"] = FUENTE
    df["actualizado_en"] = datetime.now(timezone.utc)
    return df[[n for n, _ in ESQUEMA]]


def validar(df):
    ok = True
    print("\nValidación:")
    if not df["mes"].between(1, 12).all():
        print("  Meses fuera de 1-12 -> REVISAR")
        ok = False
    indice = df["anio"] * 12 + df["mes"]
    continuo = bool((indice.diff().dropna() == 1).all())
    print(f"  Meses consecutivos sin huecos: {'OK' if continuo else 'REVISAR'}")
    ok &= continuo
    valores = df[list(FRANJAS.values())]
    en_rango = bool(valores.stack().between(*RANGO).all())
    print(f"  Valores dentro de {RANGO} °C: {'OK' if en_rango else 'REVISAR'}")
    ok &= en_rango
    inicio_ok = int(df["anio"].iloc[0]) <= 1980
    print(f"  Serie desde {int(df['anio'].iloc[0])}: {'OK' if inicio_ok else 'REVISAR (debería empezar en 1979)'}")
    ok &= inicio_ok
    u = df.iloc[-1]
    ultimo = datetime(int(u.anio), int(u.mes), 1, tzinfo=timezone.utc)
    dias = (datetime.now(timezone.utc) - ultimo).days
    fresco = dias <= MAX_DIAS_SIN_DATO
    print(f"  Último mes {int(u.anio)}-{int(u.mes):02d} (hace {dias} días desde su inicio): "
          f"{'OK' if fresco else 'DESACTUALIZADO'}")
    ok &= fresco
    for (a, m, col), esperado in CONTROL.items():
        f = df[(df.anio == a) & (df.mes == m)]
        if f.empty:
            continue
        v = f[col].iloc[0]
        bien = v is not None and abs(v - esperado) <= TOLERANCIA
        print(f"  Control {col} {a}-{m:02d}: archivo {v:+.2f} | referencia {esperado:+.2f} -> "
              f"{'OK' if bien else 'REVISAR'}")
        ok &= bool(bien)
    return ok


# ---------- Guardado (mismo esquema que los demás recolectores) ----------
ESQUEMA = [("anio", "INT64"), ("mes", "INT64"), ("t300_130e_80w", "FLOAT64"), ("t300_160e_80w", "FLOAT64"),
           ("t300_180w_100w", "FLOAT64"), ("provisional", "BOOL"), ("fuente", "STRING"),
           ("actualizado_en", "TIMESTAMP")]


def cliente_bigquery(proyecto):
    from google.cloud import bigquery
    clave = os.getenv("GCP_SA_KEY")
    if clave:
        from google.oauth2 import service_account
        cred = service_account.Credentials.from_service_account_info(json.loads(clave))
        return bigquery.Client(project=proyecto, credentials=cred)
    return bigquery.Client(project=proyecto)


def guardar(df):
    nombre = os.getenv("BQ_TABLA_CALOR") or "calor_subsuperficial"
    proyecto = os.getenv("BQ_PROYECTO")
    if not proyecto:
        with sqlite3.connect("datos_nino.db") as con:
            df.to_sql(nombre, con, if_exists="replace", index=False)
        return f"SQLite local 'datos_nino.db', tabla '{nombre}'"

    from google.cloud import bigquery
    from google.api_core.exceptions import NotFound
    cliente = cliente_bigquery(proyecto)
    ds = os.getenv("BQ_DATASET") or "monitoreo_nino"
    ds = ds if "." in ds else f"{proyecto}.{ds}"
    dataset = cliente.get_dataset(ds)
    tabla = f"{ds}.{nombre}"
    try:
        actual = cliente.get_table(tabla)
        if {c.name for c in actual.schema} != {n for n, _ in ESQUEMA}:
            print(f"\nERROR: la tabla '{tabla}' ya existe con otra estructura y NO se sobrescribirá.")
            sys.exit(1)
        if actual.num_rows and len(df) < actual.num_rows:
            print(f"\nERROR: la serie nueva tiene {len(df)} meses y la tabla {actual.num_rows}. No se escribe nada.")
            sys.exit(1)
    except NotFound:
        pass
    config = bigquery.LoadJobConfig(schema=[bigquery.SchemaField(n, t) for n, t in ESQUEMA],
                                    write_disposition="WRITE_TRUNCATE")
    cliente.load_table_from_dataframe(df, tabla, job_config=config, location=dataset.location).result()
    return f"BigQuery {tabla} ({cliente.get_table(tabla).num_rows} filas)"


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--archivo", help="leer un archivo local en vez de descargar")
    ap.add_argument("--validar", action="store_true")
    ap.add_argument("--sin-guardar", action="store_true")
    a = ap.parse_args()

    texto = open(a.archivo, encoding="utf-8").read() if a.archivo else descargar(URL)
    datos = parsear(texto)
    print("Últimos meses (anomalía 0-300 m, °C):")
    print(datos[["anio", "mes", *FRANJAS.values(), "provisional"]].tail(6).to_string(index=False))

    if a.validar and not validar(datos):
        sys.exit(1)
    if not a.sin_guardar:
        print("\nGuardado en:", guardar(datos))
