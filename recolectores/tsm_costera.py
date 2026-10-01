"""
Recolector de la variable 3: temperatura superficial del mar (TSM) DIARIA frente a la costa norte.

Fuente: NOAA OISST v2.1 (0.25°, diaria) servida por NOAA CoastWatch ERDDAP.
  - ncdcOisst21Agg_LonPM180    : versión FINAL (llega ~2 semanas después)
  - ncdcOisst21NrtAgg_LonPM180 : versión PRELIMINAR (1 día de retraso)
La final reemplaza a la preliminar cuando está disponible.

Anomalías:
  - anom_1991_2020 : calculada aquí con la climatología 1991-2020 de cada punto
                     (la misma base que usan el ICEN y el ENFEN).
  - anom_noaa_1971_2000 : la que publica NOAA (base 1971-2000), solo como referencia.

Tablas que genera:
  - tsm_costera       : una fila por punto y día (desde 1991).
  - tsm_costera_clima : climatología diaria 1991-2020 de cada punto.

Primera ejecución: descarga el histórico desde 1991 (puede tardar varios minutos).
Ejecuciones siguientes: solo descarga los últimos ~45 días (segundos).

Uso:
  python tsm_costera.py [--validar] [--reconstruir]
Destino: mismas variables que los otros recolectores (BQ_PROYECTO, BQ_DATASET,
GCP_SA_KEY...). Tablas: BQ_TABLA_TSM (por defecto 'tsm_costera') y
BQ_TABLA_TSM_CLIMA (por defecto 'tsm_costera_clima'). Sin BQ_PROYECTO usa SQLite local.
"""
import argparse, io, json, math, os, re, sqlite3, sys, time
from datetime import date, datetime, timedelta, timezone
import numpy as np
import pandas as pd
import requests

DS_FINAL = "ncdcOisst21Agg_LonPM180"
DS_NRT = "ncdcOisst21NrtAgg_LonPM180"
INICIO_HISTORICO = date(1991, 1, 1)
CLIMA_DESDE, CLIMA_HASTA = date(1991, 1, 1), date(2020, 12, 31)
AÑOS_POR_CONSULTA = 5          # ERDDAP recomienda consultas de menos de ~9 años
DIAS_REFRESCO = 45             # días recientes que se vuelven a descargar en cada ejecución
RADIO_BUSQUEDA = 0.75          # grados alrededor del puerto para buscar la celda de mar más cercana
MAX_DIAS_SIN_DATO = 5

# Puntos de monitoreo (coordenadas aproximadas de cada puerto; el script busca
# automáticamente la celda de mar más cercana, porque las celdas pegadas a la costa
# pueden estar marcadas como tierra).
PUNTOS = [
    {"punto": "Zorritos",  "region": "Tumbes",      "lat": -3.68,  "lon": -80.68},
    {"punto": "Paita",     "region": "Piura",       "lat": -5.08,  "lon": -81.11},
    {"punto": "Pimentel",  "region": "Lambayeque",  "lat": -6.84,  "lon": -79.94},
    {"punto": "Salaverry", "region": "La Libertad", "lat": -8.23,  "lon": -78.98},
    {"punto": "Chimbote",  "region": "Áncash",      "lat": -9.08,  "lon": -78.59},
    {"punto": "Callao",    "region": "Lima",        "lat": -12.05, "lon": -77.15},
]


# ---------------- Acceso a ERDDAP ----------------
# Servidores ERDDAP de NOAA que se prueban en orden (se pueden cambiar con ERDDAP_URLS,
# separados por comas). Si uno falla o no tiene el dataset, se usa el siguiente.
SERVIDORES = [u.strip().rstrip("/") for u in (os.getenv("ERDDAP_URLS") or os.getenv("ERDDAP_URL") or
              "https://coastwatch.pfeg.noaa.gov/erddap,https://upwell.pfeg.noaa.gov/erddap").split(",")]


def pedir_csv(dataset, consulta, intentos=3):
    """Descarga un CSV de ERDDAP. Muestra el código y el mensaje de cada respuesta fallida,
    reintenta ante errores temporales y prueba el siguiente servidor si uno no responde."""
    from urllib.parse import quote
    q = quote(consulta, safe="=&,")          # ERDDAP exige codificar [ ] ( ) :
    historial = []
    for servidor in list(SERVIDORES):
        url = f"{servidor}/griddap/{dataset}.csv?{q}"
        for i in range(intentos):
            try:
                r = requests.get(url, timeout=600,
                                 headers={"User-Agent": "monitoreo-nino/1.0 (uso interno)"})
            except requests.RequestException as e:
                historial.append(f"{servidor}: {type(e).__name__}")
                print(f"   [{servidor}] intento {i + 1}: sin respuesta ({type(e).__name__}: {e})", flush=True)
                time.sleep(5 * (i + 1))
                continue
            if r.status_code == 200:
                if servidor != SERVIDORES[0]:            # recordar el servidor que sí funcionó
                    SERVIDORES.remove(servidor)
                    SERVIDORES.insert(0, servidor)
                return pd.read_csv(io.StringIO(r.text), skiprows=[1])   # fila 2 = unidades
            mensaje = " ".join(r.text.split())[:300]
            historial.append(f"{servidor}: HTTP {r.status_code}")
            print(f"   [{servidor}] intento {i + 1}: HTTP {r.status_code} -> {mensaje}", flush=True)
            if r.status_code == 404 and "no matching results" in r.text.lower():
                raise ValueError(f"La consulta no tiene datos en ese rango: {mensaje}")
            if r.status_code == 400:
                raise ValueError(f"ERDDAP rechazó la sintaxis de la consulta: {mensaje}")
            if r.status_code in (403, 404):
                break                                    # bloqueado o sin el dataset: siguiente servidor
            time.sleep(5 * (i + 1))                      # 429 / 5xx: esperar y reintentar
    raise RuntimeError("Ningún servidor ERDDAP respondió correctamente. Resumen: " + "; ".join(historial))


def ultima_fecha(dataset):
    df = pedir_csv(dataset, "time[(last)]")
    return pd.to_datetime(df["time"].iloc[0]).date()


def rango(v0, v1=None):
    v1 = v0 if v1 is None else v1
    return f"[({v0}):1:({v1})]"


def consulta_grilla(t0, t1, lat0, lat1, lon0, lon1):
    ejes = rango(f"{t0}T12:00:00Z", f"{t1}T12:00:00Z") + rango(0.0) + rango(lat0, lat1) + rango(lon0, lon1)
    return f"sst{ejes},anom{ejes}"


def elegir_celda(p, dia):
    """Busca la celda de mar con dato válido más cercana al puerto."""
    r = RADIO_BUSQUEDA
    df = pedir_csv(DS_NRT, consulta_grilla(dia, dia, p["lat"] - r, p["lat"] + r,
                                            p["lon"] - r, p["lon"] + r))
    df = df.dropna(subset=["sst"])
    if df.empty:
        raise ValueError(f"No hay celdas de mar con datos cerca de {p['punto']}")
    # Distancia aproximada en km (suficiente a esta escala)
    dlat = (df["latitude"] - p["lat"]) * 111.0
    dlon = (df["longitude"] - p["lon"]) * 111.0 * math.cos(math.radians(p["lat"]))
    df = df.assign(dist=np.hypot(dlat, dlon)).sort_values("dist")
    c = df.iloc[0]
    return float(c["latitude"]), float(c["longitude"]), round(float(c["dist"]), 1)


def descargar_serie(dataset, lat, lon, desde, hasta, version):
    partes, ini = [], desde
    while ini <= hasta:
        fin = min(date(ini.year + AÑOS_POR_CONSULTA, 1, 1) - timedelta(days=1), hasta)
        print(f"   {version}: {ini} → {fin}")
        partes.append(pedir_csv(dataset, consulta_grilla(ini, fin, lat, lat, lon, lon)))
        ini = fin + timedelta(days=1)
        time.sleep(1)  # cortesía con el servidor
    df = pd.concat(partes, ignore_index=True)
    df["fecha"] = pd.to_datetime(df["time"]).dt.date
    df = df.rename(columns={"sst": "sst", "anom": "anom_noaa_1971_2000"})
    df["version"] = version
    return df[["fecha", "sst", "anom_noaa_1971_2000", "version"]]


# ---------------- Climatología y anomalías ----------------
def climatologia(df):
    """Media diaria 1991-2020 por punto, suavizada con una ventana circular de 31 días."""
    base = df[(df["fecha"] >= CLIMA_DESDE) & (df["fecha"] <= CLIMA_HASTA)].dropna(subset=["sst"])
    if base.empty:
        raise ValueError("No hay datos 1991-2020 para calcular la climatología.")
    dias = pd.date_range("2000-01-01", "2000-12-31")          # año bisiesto: 366 días
    claves = pd.DataFrame({"mes": dias.month, "dia": dias.day})
    salida = []
    for punto, g in base.groupby("punto"):
        fechas = pd.to_datetime(g["fecha"])
        medias = g.assign(mes=fechas.dt.month, dia=fechas.dt.day).groupby(["mes", "dia"])["sst"].mean()
        serie = claves.merge(medias.reset_index(), on=["mes", "dia"], how="left")["sst"]
        ext = pd.concat([serie.iloc[-15:], serie, serie.iloc[:15]], ignore_index=True)
        suave = ext.rolling(31, center=True, min_periods=15).mean().iloc[15:-15].reset_index(drop=True)
        salida.append(claves.assign(punto=punto, sst_clima=suave.round(3).values))
    return pd.concat(salida, ignore_index=True)[["punto", "mes", "dia", "sst_clima"]]


def aplicar_anomalias(df, clima):
    f = pd.to_datetime(df["fecha"])
    df = df.assign(mes=f.dt.month, dia=f.dt.day).merge(clima, on=["punto", "mes", "dia"], how="left")
    df["anom_1991_2020"] = (df["sst"] - df["sst_clima"]).round(2)
    return df.drop(columns=["mes", "dia"])


# ---------------- Almacenamiento (BigQuery o SQLite) ----------------
ESQUEMA_TSM = [("punto", "STRING"), ("region", "STRING"), ("fecha", "DATE"),
               ("lat_celda", "FLOAT64"), ("lon_celda", "FLOAT64"), ("distancia_km", "FLOAT64"),
               ("sst", "FLOAT64"), ("sst_clima", "FLOAT64"), ("anom_1991_2020", "FLOAT64"),
               ("anom_noaa_1971_2000", "FLOAT64"), ("version", "STRING"),
               ("fuente", "STRING"), ("actualizado_en", "TIMESTAMP")]
ESQUEMA_CLIMA = [("punto", "STRING"), ("mes", "INT64"), ("dia", "INT64"), ("sst_clima", "FLOAT64")]


def cliente_bigquery(proyecto):
    from google.cloud import bigquery
    clave = os.getenv("GCP_SA_KEY")
    if clave:
        from google.oauth2 import service_account
        cred = service_account.Credentials.from_service_account_info(json.loads(clave))
        return bigquery.Client(project=proyecto, credentials=cred)
    return bigquery.Client(project=proyecto)


def nombre_tabla(env, defecto):
    return os.getenv(env) or defecto


def ruta_bq(proyecto, tabla):
    ds = os.getenv("BQ_DATASET") or "monitoreo_nino"
    ds = ds if "." in ds else f"{proyecto}.{ds}"
    return ds, f"{ds}.{tabla}"


def leer_tabla(env, defecto):
    tabla = nombre_tabla(env, defecto)
    proyecto = os.getenv("BQ_PROYECTO")
    if not proyecto:
        if not os.path.exists("datos_nino.db"):
            return None
        with sqlite3.connect("datos_nino.db") as con:
            existe = con.execute("SELECT name FROM sqlite_master WHERE name=?", (tabla,)).fetchone()
            if not existe:
                return None
            df = pd.read_sql(f'SELECT * FROM "{tabla}"', con)
    else:
        from google.api_core.exceptions import NotFound
        cliente = cliente_bigquery(proyecto)
        try:
            df = cliente.list_rows(ruta_bq(proyecto, tabla)[1]).to_dataframe(create_bqstorage_client=False)
        except NotFound:
            return None
    if "fecha" in df.columns:
        df["fecha"] = pd.to_datetime(df["fecha"]).dt.date
    return df


def guardar(df, env, defecto, esquema_def):
    tabla = nombre_tabla(env, defecto)
    df = df[[n for n, _ in esquema_def]]
    proyecto = os.getenv("BQ_PROYECTO")
    if not proyecto:
        with sqlite3.connect("datos_nino.db") as con:
            df.to_sql(tabla, con, if_exists="replace", index=False)
        return f"SQLite local 'datos_nino.db', tabla '{tabla}' ({len(df)} filas)"

    from google.cloud import bigquery
    from google.api_core.exceptions import NotFound
    cliente = cliente_bigquery(proyecto)
    ds_ref, ref = ruta_bq(proyecto, tabla)
    try:
        dataset = cliente.get_dataset(ds_ref)
    except NotFound:
        print(f"\nERROR: no existe el dataset '{ds_ref}'.")
        sys.exit(1)
    try:
        actuales = {c.name for c in cliente.get_table(ref).schema}
        if actuales != {n for n, _ in esquema_def}:
            print(f"\nERROR: la tabla '{ref}' ya existe con otra estructura y NO se sobrescribirá.")
            sys.exit(1)
    except NotFound:
        pass
    config = bigquery.LoadJobConfig(schema=[bigquery.SchemaField(n, t) for n, t in esquema_def],
                                    write_disposition="WRITE_TRUNCATE")
    cliente.load_table_from_dataframe(df, ref, job_config=config, location=dataset.location).result()
    return f"BigQuery {ref} ({cliente.get_table(ref).num_rows} filas)"


# ---------------- Proceso principal ----------------
def actualizar_erddap(reconstruir=False):
    fin_final, fin_nrt = ultima_fecha(DS_FINAL), ultima_fecha(DS_NRT)
    print(f"Datos disponibles en NOAA: final hasta {fin_final}, preliminar hasta {fin_nrt}")
    existente = None if reconstruir else leer_tabla("BQ_TABLA_TSM", "tsm_costera")
    nuevos = []
    for p in PUNTOS:
        lat, lon, dist = elegir_celda(p, fin_nrt)
        print(f"{p['punto']}: celda {lat}, {lon} ({dist} km del puerto)")
        previo = None if existente is None else existente[
            (existente["punto"] == p["punto"]) & (existente["lat_celda"] == lat) & (existente["lon_celda"] == lon)]
        completo = previo is None or previo.empty
        desde = INICIO_HISTORICO if completo else fin_final - timedelta(days=DIAS_REFRESCO)
        partes = [descargar_serie(DS_FINAL, lat, lon, desde, fin_final, "final")]
        if fin_nrt > fin_final:
            partes.append(descargar_serie(DS_NRT, lat, lon, fin_final + timedelta(days=1), fin_nrt, "preliminar"))
        serie = pd.concat(partes, ignore_index=True)
        serie = serie.assign(punto=p["punto"], region=p["region"], lat_celda=lat, lon_celda=lon,
                             distancia_km=dist)
        if not completo:  # conservar el histórico ya guardado y reemplazar solo lo reciente
            serie = pd.concat([previo[previo["fecha"] < desde], serie], ignore_index=True)
        nuevos.append(serie)

    datos = pd.concat(nuevos, ignore_index=True)
    datos = datos.sort_values(["punto", "fecha", "version"]).drop_duplicates(["punto", "fecha"], keep="first")

    clima = None if reconstruir else leer_tabla("BQ_TABLA_TSM_CLIMA", "tsm_costera_clima")
    if clima is None or set(clima["punto"]) != {p["punto"] for p in PUNTOS}:
        print("Calculando climatología 1991-2020...")
        clima = climatologia(datos)
        print("Guardado en:", guardar(clima, "BQ_TABLA_TSM_CLIMA", "tsm_costera_clima", ESQUEMA_CLIMA))

    datos = aplicar_anomalias(datos.drop(columns=["sst_clima", "anom_1991_2020"], errors="ignore"), clima)
    datos["fuente"] = "NOAA OISST v2.1 (CoastWatch ERDDAP)"
    datos["actualizado_en"] = datetime.now(timezone.utc)
    return datos, clima


# ---------------- Actualización diaria desde NCEI (fuente original de OISST) ----------------
# Los mismos archivos diarios que ERDDAP redistribuye, publicados por NOAA NCEI.
# Se usan para la actualización diaria: solo se descargan los días nuevos o los que pasaron
# de preliminar a final (normalmente 2 a 5 archivos de ~1.6 MB).
NCEI_URL = os.getenv("NCEI_URL", "https://www.ncei.noaa.gov/data/sea-surface-temperature-optimum-interpolation/v2.1/access/avhrr")
ARCHIVO_NCEI = re.compile(r'href="(oisst-avhrr-v02r01\.(\d{8})(_preliminary)?\.nc)"')
DIAS_REVISION_NCEI = 30        # ventana máxima hacia atrás que se revisa en cada ejecución


def http_get(url, intentos=3):
    for i in range(intentos):
        try:
            r = requests.get(url, timeout=180, headers={"User-Agent": "monitoreo-nino/1.0 (uso interno)"})
            if r.status_code == 200:
                return r
            mensaje = " ".join(r.text.split())[:300]
            print(f"   [NCEI] intento {i + 1}: HTTP {r.status_code} -> {mensaje}", flush=True)
            if r.status_code in (403, 404):
                break
        except requests.RequestException as e:
            print(f"   [NCEI] intento {i + 1}: sin respuesta ({type(e).__name__})", flush=True)
        time.sleep(10 * (i + 1))
    raise RuntimeError(f"NCEI no respondió correctamente: {url}")


def indice_mes(anio_mes):
    """Lista de archivos disponibles en el directorio mensual: {fecha: {'final': nombre, 'preliminar': nombre}}."""
    html = http_get(f"{NCEI_URL}/{anio_mes}/").text
    disponibles = {}
    for nombre, ymd, prelim in ARCHIVO_NCEI.findall(html):
        d = datetime.strptime(ymd, "%Y%m%d").date()
        disponibles.setdefault(d, {})["preliminar" if prelim else "final"] = nombre
    return disponibles


def leer_archivo_ncei(anio_mes, nombre, celdas):
    """Descarga un archivo diario y extrae SST y anomalía en las celdas de cada punto."""
    import netCDF4
    contenido = http_get(f"{NCEI_URL}/{anio_mes}/{nombre}").content
    with netCDF4.Dataset("oisst", memory=contenido) as nc:
        lats, lons = nc.variables["lat"][:], nc.variables["lon"][:]
        sst, anom = nc.variables["sst"], nc.variables["anom"]
        valores = {}
        for punto, (lat, lon) in celdas.items():
            i = int(np.argmin(np.abs(lats - lat)))
            j = int(np.argmin(np.abs(lons - (lon % 360))))      # NCEI usa longitudes 0-360
            if abs(lats[i] - lat) > 0.01 or abs(lons[j] - (lon % 360)) > 0.01:
                raise ValueError(f"La grilla de NCEI no coincide con la celda de {punto}")
            v_sst, v_anom = sst[0, 0, i, j], anom[0, 0, i, j]
            valores[punto] = (None if np.ma.is_masked(v_sst) else round(float(v_sst), 2),
                              None if np.ma.is_masked(v_anom) else round(float(v_anom), 2))
    return valores


MAX_RELLENO_POR_EJECUCION = int(os.getenv("TSM_MAX_RELLENO", "300"))  # días de hueco que se rellenan por ejecución


def describir_tramos(fechas):
    """Resume una lista de fechas como tramos continuos: '2021-05-01 a 2024-08-09 (1196 días)'."""
    fechas = sorted(fechas); tramos, ini, prev = [], fechas[0], fechas[0]
    for d in fechas[1:] + [None]:
        if d is None or (d - prev).days > 1:
            tramos.append(f"{ini} a {prev} ({(prev - ini).days + 1} días)"); ini = d
        prev = d if d is not None else prev
    return "; ".join(tramos[:5]) + (" ..." if len(tramos) > 5 else "")


def actualizar_ncei(existente):
    info = existente.drop_duplicates("punto").set_index("punto")
    celdas = {p: (float(info.loc[p, "lat_celda"]), float(info.loc[p, "lon_celda"])) for p in info.index}
    ayer = datetime.now(timezone.utc).date() - timedelta(days=1)
    finales = existente[existente["version"] == "final"].groupby("punto")["fecha"].max()
    desde = max(finales.min() + timedelta(days=1), ayer - timedelta(days=DIAS_REVISION_NCEI))
    guardadas = existente[existente["fecha"] >= desde].groupby("fecha")["version"].agg(
        lambda v: "final" if (v == "final").all() else "preliminar").to_dict()

    # Huecos del histórico (días que faltan antes de la ventana reciente): se rellenan por tandas
    presentes = set(existente.groupby("fecha")["punto"].nunique().loc[lambda n: n == len(celdas)].index)
    inicio = existente["fecha"].min()
    huecos = [inicio + timedelta(days=k) for k in range((desde - inicio).days)
              if inicio + timedelta(days=k) not in presentes]
    relleno = huecos[:MAX_RELLENO_POR_EJECUCION]
    if huecos:
        print(f"Histórico: faltan {len(huecos)} días ({describir_tramos(huecos)}). "
              f"En esta ejecución se rellenan {len(relleno)}.")

    fechas_revisar = relleno + [desde + timedelta(days=k) for k in range((ayer - desde).days + 1)]
    meses = sorted({d.strftime("%Y%m") for d in fechas_revisar})
    disponibles = {}
    for m in meses:
        disponibles.update(indice_mes(m))
    for d in relleno:                      # los huecos se tratan como días aún no guardados
        guardadas.pop(d, None)

    filas, n_final, n_prelim = [], 0, 0
    for d in fechas_revisar:
        opciones, actual = disponibles.get(d, {}), guardadas.get(d)
        if actual == "final":
            continue
        if "final" in opciones:
            version, nombre = "final", opciones["final"]; n_final += 1
        elif "preliminar" in opciones and actual is None:
            version, nombre = "preliminar", opciones["preliminar"]; n_prelim += 1
        else:
            continue       # sin archivo nuevo para ese día
        for punto, (v_sst, v_anom) in leer_archivo_ncei(d.strftime("%Y%m"), nombre, celdas).items():
            filas.append({"punto": punto, "region": info.loc[punto, "region"], "fecha": d,
                          "lat_celda": celdas[punto][0], "lon_celda": celdas[punto][1],
                          "distancia_km": info.loc[punto, "distancia_km"], "sst": v_sst,
                          "anom_noaa_1971_2000": v_anom, "version": version})
        time.sleep(0.5)    # cortesía con el servidor
    print(f"NCEI: {n_final} archivos finales y {n_prelim} preliminares descargados "
          f"(revisado desde {desde} hasta {ayer}{', más huecos del histórico' if relleno else ''})")
    if not filas:
        return existente
    nuevos = pd.DataFrame(filas)
    clave = set(zip(nuevos["punto"], nuevos["fecha"]))
    conservar = existente[[(p, f) not in clave for p, f in zip(existente["punto"], existente["fecha"])]]
    return pd.concat([conservar, nuevos], ignore_index=True)


def actualizar(reconstruir=False):
    """Si ya existe el histórico, actualiza desde NCEI (liviano). Si no existe o se pide
    reconstruir, descarga el histórico completo desde ERDDAP."""
    existente = None if reconstruir else leer_tabla("BQ_TABLA_TSM", "tsm_costera")
    if existente is None or set(existente["punto"]) != {p["punto"] for p in PUNTOS}:
        print("No hay histórico completo guardado: se descarga desde ERDDAP.")
        return actualizar_erddap(reconstruir)
    datos = actualizar_ncei(existente)
    datos = datos.sort_values(["punto", "fecha", "version"]).drop_duplicates(["punto", "fecha"], keep="first")
    # Protección: una actualización nunca debe tener MENOS días que lo ya guardado
    antes_n = existente.groupby("punto")["fecha"].nunique()
    despues_n = datos.groupby("punto")["fecha"].nunique()
    perdidos = (antes_n - despues_n.reindex(antes_n.index).fillna(0)).clip(lower=0)
    if perdidos.sum() > 0:
        raise RuntimeError(f"Se perderían días al guardar ({perdidos.to_dict()}). No se guarda nada; revisar.")
    antes = set(existente.loc[(existente["fecha"] >= CLIMA_DESDE) & (existente["fecha"] <= CLIMA_HASTA), "fecha"])
    ahora = set(datos.loc[(datos["fecha"] >= CLIMA_DESDE) & (datos["fecha"] <= CLIMA_HASTA), "fecha"])
    clima = leer_tabla("BQ_TABLA_TSM_CLIMA", "tsm_costera_clima")
    if clima is None or ahora != antes:
        if clima is not None:
            print("Se completaron días del periodo 1991-2020: se recalcula la climatología.")
        clima = climatologia(datos)
        print("Guardado en:", guardar(clima, "BQ_TABLA_TSM_CLIMA", "tsm_costera_clima", ESQUEMA_CLIMA))
    datos = aplicar_anomalias(datos.drop(columns=["sst_clima", "anom_1991_2020"], errors="ignore"), clima)
    datos["fuente"] = datos["fuente"].fillna("NOAA OISST v2.1 (NCEI)") if "fuente" in datos else "NOAA OISST v2.1 (NCEI)"
    nuevos = datos["actualizado_en"].isna() if "actualizado_en" in datos else pd.Series(True, index=datos.index)
    datos.loc[nuevos, "fuente"] = "NOAA OISST v2.1 (NCEI)"
    datos["actualizado_en"] = datetime.now(timezone.utc)
    return datos, clima


def validar(datos):
    ok = True
    print("\nValidación:")
    hoy = datetime.now(timezone.utc).date()
    for punto, g in datos.groupby("punto"):
        ultimo = g["fecha"].max()
        reciente = g[g["fecha"] > ultimo - timedelta(days=365)]
        par = reciente[["anom_1991_2020", "anom_noaa_1971_2000"]].dropna()
        # Nuestra anomalía y la de NOAA deben SUBIR Y BAJAR JUNTAS (alta correlación).
        # Su diferencia media puede ser de 1-2 °C cerca de la costa: la climatología de NOAA
        # (1971-2000, producto OI.v2 más grueso) suaviza el afloramiento costero frío.
        corr = par.corr().iloc[0, 1] if len(par) > 30 else float("nan")
        media = (par.iloc[:, 0] - par.iloc[:, 1]).mean() if len(par) else float("nan")
        esperados = (ultimo - g["fecha"].min()).days + 1
        faltan = esperados - g["fecha"].nunique()
        checks = {
            "rango físico": bool(g["sst"].dropna().between(5, 35).all()),
            "dato reciente": (hoy - ultimo).days <= MAX_DIAS_SIN_DATO,
            "anomalía coherente": bool(corr >= 0.9 and abs(media) < 3.0),
        }
        ok &= all(checks.values())
        estado = ", ".join(f"{k} {'OK' if v else 'REVISAR'}" for k, v in checks.items())
        estado += f", histórico {'completo' if faltan == 0 else f'con {faltan} días por rellenar'}"
        print(f"  {punto:10s} último dato {ultimo}: {estado} "
              f"(correlación con NOAA {corr:.2f}, diferencia media {media:+.2f} °C)")
    return ok


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--validar", action="store_true")
    ap.add_argument("--reconstruir", action="store_true", help="vuelve a descargar todo el histórico")
    a = ap.parse_args()

    datos, clima = actualizar(a.reconstruir)
    ult = datos.sort_values("fecha").groupby("punto").tail(1)
    orden = {p["punto"]: i for i, p in enumerate(PUNTOS)}
    ult = ult.assign(o=ult["punto"].map(orden)).sort_values("o")
    print("\nÚltimo dato por punto (de norte a sur):")
    print(ult[["punto", "fecha", "sst", "sst_clima", "anom_1991_2020", "version"]].to_string(index=False))

    if a.validar and not validar(datos):
        sys.exit(1)
    print("\nGuardado en:", guardar(datos, "BQ_TABLA_TSM", "tsm_costera", ESQUEMA_TSM))
