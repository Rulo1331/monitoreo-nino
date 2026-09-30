"""
Recolector de la variable 5 (parte 1 de 2): LLUVIA por ZONAS desde Open-Meteo.
(La parte 2, lluvia medida por satélite NASA GPM IMERG, va en otro script.)

Para evitar el sesgo de un solo punto, cada zona se cubre con una GRILLA de puntos:
  - se generan puntos dentro de un rectángulo aproximado de la zona,
  - se descartan los que caen en el mar o fuera del rango de altitud de la zona
    (con el modelo de elevación de Open-Meteo, 90 m),
  - por zona y día se calcula: lluvia media, lluvia máxima y % del área sobre un umbral.

Datos que obtiene:
  1. Pronóstico determinista: últimos 7 días + 16 días adelante (modelo best_match).
  2. Ensemble ECMWF (51 miembros, 7 días): probabilidad de superar 1, 5, 10 y 20 mm.
  3. Umbrales con el método de SENAMHI (NT 001-2014): percentiles de los días con lluvia
     (>0.1 mm, sin el máximo). Categorías: Normal / Moderadamente lluvioso / Lluvioso /
     Muy lluvioso / Extremadamente lluvioso, y nivel de aviso Moderado / Fuerte / Extremo.

Tablas:
  - lluvia_zonas  : una fila por zona, fecha y día de emisión (se acumula el historial
                    de pronósticos para poder contrastarlos luego con el satélite).
  - lluvia_puntos : lluvia de cada punto de la grilla (última emisión, para mapas).
  - lluvia_umbrales    : umbrales P75/P90/P95/P99 por zona: oficiales SENAMHI donde existen
                         (Chao; Virú por aproximación) y del modelo en el resto, más el contraste.
  - eventos_historicos : emergencias INDECI 2006-2017 (Cuadro 01, CENEPRED 2017) para validación.

Uso: python lluvia.py [--validar] [--reconstruir-clima]
Destino: BQ_PROYECTO, BQ_DATASET, GCP_SA_KEY (igual que los otros recolectores).
Plan comercial de Open-Meteo: definir OPEN_METEO_API_KEY (usa servidores 'customer-').
Datos: Open-Meteo.com, licencia CC BY 4.0 (el dashboard debe mostrar la atribución).
"""
import argparse, json, math, os, sqlite3, sys, time
from datetime import date, datetime, timedelta, timezone
import numpy as np
import pandas as pd
import requests

# ------------------------------------------------------------------ Zonas
# POLÍGONOS aproximados (lat, lon). Valle Chao y Cuenca alta Chao se digitalizaron a partir de
# la Figura 01 del informe CENEPRED 2017 (UTM 17S convertido a lat/lon). El resto son
# aproximaciones a revisar. Se reemplazarán por polígonos oficiales (shapefiles ANA/SIG).
# elev_min / elev_max separan el valle (costa) de la cuenca alta (sierra) aunque los polígonos se toquen.
ZONAS = [
    {"zona": "Valle Virú", "tipo": "local", "paso": 0.03, "elev_min": 2, "elev_max": 800,
     "poligono": [(-8.34, -78.90), (-8.30, -78.78), (-8.31, -78.62), (-8.33, -78.54), (-8.38, -78.55),
                  (-8.42, -78.68), (-8.47, -78.80), (-8.49, -78.90)]},
    {"zona": "Valle Chao", "tipo": "local", "paso": 0.03, "elev_min": 2, "elev_max": 800,
     "poligono": [(-8.588, -78.724), (-8.508, -78.706), (-8.488, -78.657), (-8.446, -78.548), (-8.432, -78.466),
                  (-8.482, -78.457), (-8.523, -78.566), (-8.574, -78.638), (-8.619, -78.692)]},
    {"zona": "Trujillo", "tipo": "referencial", "paso": 0.03, "elev_min": 2, "elev_max": 500,
     "poligono": [(-8.03, -79.08), (-8.03, -78.98), (-8.10, -78.96), (-8.17, -78.99), (-8.17, -79.06)]},
    {"zona": "Cuenca alta Virú", "tipo": "cuenca alta", "paso": 0.08, "elev_min": 1500, "elev_max": 5000,
     "poligono": [(-8.31, -78.62), (-8.15, -78.64), (-7.97, -78.52), (-7.97, -78.30), (-8.12, -78.25),
                  (-8.32, -78.40), (-8.34, -78.50)]},
    {"zona": "Cuenca alta Chao", "tipo": "cuenca alta", "paso": 0.06, "elev_min": 1000, "elev_max": 5000,
     "poligono": [(-8.437, -78.566), (-8.401, -78.485), (-8.364, -78.403), (-8.476, -78.280), (-8.603, -78.279),
                  (-8.649, -78.383), (-8.677, -78.501), (-8.605, -78.583), (-8.514, -78.547), (-8.473, -78.448)]},
    {"zona": "Cuenca alta Santa", "tipo": "cuenca alta", "paso": 0.15, "elev_min": 2000, "elev_max": 6000,
     "poligono": [(-8.20, -78.20), (-8.20, -77.75), (-8.70, -77.60), (-9.50, -77.40), (-10.15, -77.25),
                  (-10.15, -77.55), (-9.40, -77.85), (-8.80, -78.10), (-8.66, -78.26)]},
]

# Umbrales OFICIALES de lluvia diaria (mm). SENAMHI, Nota Técnica 001-SENAMHI-DGM-2014, citados en
# el informe CENEPRED 2017 (Cuadro 12) para el distrito de Chao. Percentiles de los DÍAS CON LLUVIA
# (RR > 0.1 mm), excluyendo el valor máximo registrado.
UMBRALES_CHAO = {"p75": 0.82, "p90": 1.96, "p95": 3.04, "p99": 6.02}
UMBRALES_OFICIALES = {
    "Valle Chao": {**UMBRALES_CHAO, "fuente": "SENAMHI 2014 (NT 001) vía CENEPRED 2017 - distrito Chao"},
    # Virú no tiene umbral oficial en el informe: se usan los de Chao (valle vecino, mismo clima desértico).
    "Valle Virú": {**UMBRALES_CHAO, "fuente": "Aproximación: umbrales oficiales de Chao aplicados a Virú"},
}
DIA_CON_LLUVIA_MM = 0.1                 # criterio SENAMHI
UMBRAL_AREA_MM = {"local": 1.0, "referencial": 1.0, "cuenca alta": 10.0}   # para % del área con lluvia
UMBRALES_ENSEMBLE = [1, 5, 10, 20]      # mm/día sobre la media de la zona
CLIMA_DESDE, CLIMA_HASTA = "1991-01-01", "2020-12-31"
MAX_ZONAS_CLIMA_POR_EJECUCION = 3       # reparte el cálculo histórico para no exceder el límite diario
PUNTOS_POR_CONSULTA = 50
ZONA_HORARIA = "America/Lima"


# ------------------------------------------------------------------ Acceso a Open-Meteo
def url_api(servicio):
    """servicio: api | ensemble-api | archive-api. Con API key usa los servidores comerciales."""
    base = os.getenv("OPEN_METEO_URL")            # solo para pruebas
    if base:
        return base.rstrip("/")
    prefijo = "customer-" if os.getenv("OPEN_METEO_API_KEY") else ""
    return f"https://{prefijo}{servicio}.open-meteo.com"


def pedir(servicio, ruta, params, intentos=4):
    params = dict(params)
    if os.getenv("OPEN_METEO_API_KEY"):
        params["apikey"] = os.getenv("OPEN_METEO_API_KEY")
    url = f"{url_api(servicio)}{ruta}"
    for i in range(intentos):
        try:
            r = requests.get(url, params=params, timeout=120,
                             headers={"User-Agent": "monitoreo-nino/1.0 (uso interno)"})
        except requests.RequestException as e:
            print(f"   [{servicio}] intento {i + 1}: sin respuesta ({type(e).__name__})", flush=True)
            time.sleep(10 * (i + 1))
            continue
        if r.status_code == 200:
            return r.json()
        mensaje = " ".join(r.text.split())[:300]
        print(f"   [{servicio}] intento {i + 1}: HTTP {r.status_code} -> {mensaje}", flush=True)
        if r.status_code == 429:
            raise LimiteExcedido(mensaje)
        if r.status_code == 400:
            raise ValueError(f"Open-Meteo rechazó la consulta: {mensaje}")
        time.sleep(10 * (i + 1))
    raise RuntimeError(f"Open-Meteo no respondió tras {intentos} intentos ({servicio}{ruta})")


class LimiteExcedido(Exception):
    pass


def en_lotes(lista, n):
    for i in range(0, len(lista), n):
        yield lista[i:i + n]


def coords(puntos):
    return {"latitude": ",".join(f"{p['lat']:.4f}" for p in puntos),
            "longitude": ",".join(f"{p['lon']:.4f}" for p in puntos)}


def como_lista(respuesta):
    """Con varias coordenadas Open-Meteo devuelve una lista; con una sola, un objeto."""
    return respuesta if isinstance(respuesta, list) else [respuesta]


# ------------------------------------------------------------------ 1. Grilla por zona
def dentro(lat, lon, poligono):
    """Punto en polígono (algoritmo de trazado de rayos)."""
    adentro, n = False, len(poligono)
    for i in range(n):
        (y1, x1), (y2, x2) = poligono[i], poligono[(i + 1) % n]
        if (y1 > lat) != (y2 > lat) and lon < (x2 - x1) * (lat - y1) / (y2 - y1) + x1:
            adentro = not adentro
    return adentro


def construir_grilla():
    candidatos = []
    for z in ZONAS:
        lats = [p[0] for p in z["poligono"]]; lons = [p[1] for p in z["poligono"]]
        for la in np.arange(min(lats), max(lats) + 1e-9, z["paso"]):
            for lo in np.arange(min(lons), max(lons) + 1e-9, z["paso"]):
                if dentro(la, lo, z["poligono"]):
                    candidatos.append({"zona": z["zona"], "lat": round(float(la), 4), "lon": round(float(lo), 4)})
    for lote in en_lotes(candidatos, 100):          # la API de elevación acepta hasta 100 puntos
        elev = pedir("api", "/v1/elevation", coords(lote))["elevation"]
        for p, e in zip(lote, elev):
            p["elevacion"] = e
    limites = {z["zona"]: z for z in ZONAS}
    puntos = [p for p in candidatos if p["elevacion"] is not None and not math.isnan(p["elevacion"])
              and limites[p["zona"]]["elev_min"] <= p["elevacion"] <= limites[p["zona"]]["elev_max"]]
    for z in ZONAS:
        n = sum(p["zona"] == z["zona"] for p in puntos)
        total = sum(p["zona"] == z["zona"] for p in candidatos)
        print(f"   {z['zona']}: {n} de {total} puntos dentro del rango de altitud")
    return puntos


# ------------------------------------------------------------------ 2. Pronóstico determinista
def pronostico(puntos):
    filas = []
    for lote in en_lotes(puntos, PUNTOS_POR_CONSULTA):
        resp = pedir("api", "/v1/forecast", {**coords(lote), "timezone": ZONA_HORARIA,
                     "past_days": 7, "forecast_days": 16,
                     "daily": "precipitation_sum,precipitation_hours,precipitation_probability_max"})
        for p, r in zip(lote, como_lista(resp)):
            d = r["daily"]
            for i, f in enumerate(d["time"]):
                filas.append({**p, "fecha": date.fromisoformat(f),
                              "lluvia_mm": d["precipitation_sum"][i],
                              "horas_lluvia": d["precipitation_hours"][i],
                              "prob_lluvia_modelo": d.get("precipitation_probability_max", [None] * len(d["time"]))[i]})
    return pd.DataFrame(filas)


# ------------------------------------------------------------------ 3. Ensemble (probabilidades)
def ensemble(puntos):
    """Devuelve, por zona y fecha, la probabilidad (fracción de miembros) de que la lluvia
    MEDIA de la zona supere cada umbral, más la media y el rango p10-p90 de los miembros."""
    miembros = []   # filas: zona, fecha, miembro, lluvia
    for lote in en_lotes(puntos, PUNTOS_POR_CONSULTA):
        resp = pedir("ensemble-api", "/v1/ensemble", {**coords(lote), "timezone": ZONA_HORARIA,
                     "models": "ecmwf_ifs025", "forecast_days": 7, "daily": "precipitation_sum"})
        for p, r in zip(lote, como_lista(resp)):
            d = r["daily"]
            claves = [k for k in d if k.startswith("precipitation_sum")]
            for k in claves:
                for i, f in enumerate(d["time"]):
                    miembros.append({"zona": p["zona"], "fecha": date.fromisoformat(f), "miembro": k,
                                     "lluvia": d[k][i]})
    df = pd.DataFrame(miembros)
    # media espacial de la zona para cada miembro y día
    media_zona = df.groupby(["zona", "fecha", "miembro"])["lluvia"].mean().reset_index()
    salida = []
    for (zona, fecha), g in media_zona.groupby(["zona", "fecha"]):
        v = g["lluvia"].dropna()
        fila = {"zona": zona, "fecha": fecha, "ens_miembros": len(v),
                "ens_media": round(v.mean(), 2), "ens_p10": round(v.quantile(0.1), 2),
                "ens_p90": round(v.quantile(0.9), 2)}
        for u in UMBRALES_ENSEMBLE:
            fila[f"prob_ens_{u}mm"] = round(float((v > u).mean()), 3)
        salida.append(fila)
    return pd.DataFrame(salida)


# ------------------------------------------------------------------ 4. Umbrales por zona
def percentiles_senamhi(valores):
    """Método de la Nota Técnica 001 SENAMHI-DGM-2014: percentiles de los días con lluvia
    (RR > 0.1 mm), excluyendo el valor máximo registrado."""
    humedos = np.sort(np.asarray([v for v in valores if v is not None and v > DIA_CON_LLUVIA_MM]))
    if len(humedos) < 20:
        return None, len(humedos)
    muestra = humedos[:-1]                                   # excluir el máximo
    q = np.percentile(muestra, [75, 90, 95, 99])
    return dict(zip(["p75", "p90", "p95", "p99"], np.round(q, 2))), len(humedos)


def umbrales_zona(zona, puntos):
    """Umbrales del modelo (ERA5-Land 1991-2020) con el método SENAMHI. Cada punto representativo
    se trata como una 'estación' (serie puntual), igual que los umbrales oficiales."""
    pz = [p for p in puntos if p["zona"] == zona]
    representativos = pz if len(pz) <= 4 else [pz[i] for i in np.linspace(0, len(pz) - 1, 4).astype(int)]
    resp = pedir("archive-api", "/v1/archive", {**coords(representativos), "timezone": ZONA_HORARIA,
                 "start_date": CLIMA_DESDE, "end_date": CLIMA_HASTA, "models": "era5_land",
                 "daily": "precipitation_sum"})
    valores, total = [], 0
    for r in como_lista(resp):
        serie = r["daily"]["precipitation_sum"]
        total += sum(v is not None for v in serie)
        valores += serie
    modelo, n_humedos = percentiles_senamhi(valores)
    fila = {"zona": zona, "puntos_usados": len(representativos), "dias_con_lluvia": n_humedos,
            "pct_dias_con_lluvia": round(100 * n_humedos / total, 2) if total else None}
    for k in ("p75", "p90", "p95", "p99"):
        fila[f"modelo_{k}"] = None if modelo is None else float(modelo[k])
    return fila


def completar_umbrales(tabla):
    """Elige los umbrales a usar: oficiales si existen; si no, los del modelo."""
    tabla = tabla.copy()
    for k in ("p75", "p90", "p95", "p99"):
        tabla[k] = [UMBRALES_OFICIALES.get(z, {}).get(k, m) for z, m in zip(tabla["zona"], tabla[f"modelo_{k}"])]
    tabla["fuente_umbral"] = [UMBRALES_OFICIALES[z]["fuente"] if z in UMBRALES_OFICIALES
                              else "Modelo ERA5-Land 1991-2020 (método SENAMHI)" for z in tabla["zona"]]
    return tabla


def categoria(valor, u):
    """Escala SENAMHI / CENEPRED de caracterización de lluvias extremas."""
    if valor is None or pd.isna(valor) or u is None or pd.isna(u.get("p99")):
        return None
    if valor <= DIA_CON_LLUVIA_MM:
        return "Sin lluvia"
    if valor <= u["p75"]:
        return "Normal"
    if valor <= u["p90"]:
        return "Moderadamente lluvioso"
    if valor <= u["p95"]:
        return "Lluvioso"
    if valor <= u["p99"]:
        return "Muy lluvioso"
    return "Extremadamente lluvioso"


def nivel_aviso(valor, u):
    """Niveles de peligro de los avisos SENAMHI (PR-DMA-002): p90-p95 moderado, p95-p99 fuerte, >p99 extremo."""
    if valor is None or pd.isna(valor) or u is None or pd.isna(u.get("p99")):
        return None
    if valor > u["p99"]:
        return "Extremo"
    if valor > u["p95"]:
        return "Fuerte"
    if valor > u["p90"]:
        return "Moderado"
    return "Sin aviso"


# ------------------------------------------------------------------ Agregación por zona
def agregar_zonas(det, ens, umbrales, emitido):
    tipo = {z["zona"]: z["tipo"] for z in ZONAS}
    umbral = {z: UMBRAL_AREA_MM[t] for z, t in tipo.items()}
    filas = []
    for (zona, fecha), g in det.groupby(["zona", "fecha"]):
        v = g["lluvia_mm"].dropna()
        filas.append({"zona": zona, "tipo_zona": tipo[zona], "fecha": fecha, "emitido": emitido,
                      "dias_adelante": (fecha - emitido).days,
                      "tipo_dato": "pasado (modelo)" if fecha < emitido else "pronóstico",
                      "puntos": int(len(v)), "lluvia_media": round(v.mean(), 2) if len(v) else None,
                      "lluvia_max": round(v.max(), 2) if len(v) else None,
                      "umbral_mm": umbral[zona],
                      "pct_area_sobre_umbral": round(float((v >= umbral[zona]).mean() * 100), 1) if len(v) else None})
    df = pd.DataFrame(filas).merge(ens, on=["zona", "fecha"], how="left")
    u = {r["zona"]: r for r in umbrales.to_dict("records")} if umbrales is not None else {}
    # La media representa a la zona; el máximo, al punto más afectado (comparable con una estación).
    df["categoria_media"] = [categoria(v, u.get(z)) for v, z in zip(df["lluvia_media"], df["zona"])]
    df["categoria_max"] = [categoria(v, u.get(z)) for v, z in zip(df["lluvia_max"], df["zona"])]
    df["nivel_aviso_max"] = [nivel_aviso(v, u.get(z)) for v, z in zip(df["lluvia_max"], df["zona"])]
    df["fuente"] = "Open-Meteo (best_match + ensemble ECMWF IFS 0.25)"
    df["actualizado_en"] = datetime.now(timezone.utc)
    return df


# ------------------------------------------------------------------ Almacenamiento
ESQUEMA_ZONAS = ([("zona", "STRING"), ("tipo_zona", "STRING"), ("fecha", "DATE"), ("emitido", "DATE"),
                  ("dias_adelante", "INT64"), ("tipo_dato", "STRING"), ("puntos", "INT64"),
                  ("lluvia_media", "FLOAT64"), ("lluvia_max", "FLOAT64"), ("umbral_mm", "FLOAT64"),
                  ("pct_area_sobre_umbral", "FLOAT64"), ("ens_miembros", "INT64"), ("ens_media", "FLOAT64"),
                  ("ens_p10", "FLOAT64"), ("ens_p90", "FLOAT64")]
                 + [(f"prob_ens_{u}mm", "FLOAT64") for u in UMBRALES_ENSEMBLE]
                 + [("categoria_media", "STRING"), ("categoria_max", "STRING"), ("nivel_aviso_max", "STRING"),
                    ("fuente", "STRING"), ("actualizado_en", "TIMESTAMP")])
ESQUEMA_PUNTOS = [("zona", "STRING"), ("lat", "FLOAT64"), ("lon", "FLOAT64"), ("elevacion", "FLOAT64"),
                  ("fecha", "DATE"), ("emitido", "DATE"), ("lluvia_mm", "FLOAT64"), ("horas_lluvia", "FLOAT64"),
                  ("prob_lluvia_modelo", "FLOAT64"), ("actualizado_en", "TIMESTAMP")]
ESQUEMA_UMBRALES = ([("zona", "STRING"), ("p75", "FLOAT64"), ("p90", "FLOAT64"), ("p95", "FLOAT64"),
                     ("p99", "FLOAT64"), ("fuente_umbral", "STRING")]
                    + [(f"modelo_{k}", "FLOAT64") for k in ("p75", "p90", "p95", "p99")]
                    + [("dias_con_lluvia", "INT64"), ("pct_dias_con_lluvia", "FLOAT64"), ("puntos_usados", "INT64")])
ESQUEMA_EVENTOS = [("n", "INT64"), ("anio", "INT64"), ("fecha", "DATE"), ("codigo_indeci", "STRING"),
                   ("fenomeno", "STRING"), ("descripcion", "STRING"), ("localidades", "STRING"),
                   ("zona", "STRING"), ("confianza_zona", "STRING"), ("usar_para_lluvia", "BOOL"),
                   ("fuente", "STRING")]

# Cuadro N° 01 del informe CENEPRED 2017 (Reportes de Emergencias INDECI, provincia de Virú).
# 'zona' y 'confianza_zona' son asignaciones propias a partir de las localidades mencionadas.
EVENTOS_INDECI = [
    (1, "2006-06-17", "16436", "Inundación", "Oleaje anómalo: 225 hab. y 8 viv. afectados", "Puerto Morín", "Valle Virú", "media", False),
    (2, "2006-04-05", "15293", "Lluvias", "15 ha de cultivo perdidas; caminos rurales y reservorios afectados", "", "sin asignar", "baja", True),
    (3, "2006-01-19", "17538", "Inundación", "Lluvias intensas: 7 viv. y 42 hab. afectados", "La Gloria", "Valle Virú", "media", True),
    (4, "2009-03-03", "32250", "Lluvias", "42 viv. y 217 hab. afectados", "Llacamate", "Valle Chao", "media", True),
    (5, "2009-01-12", "31316", "Lluvias", "173 ha de cultivo afectadas", "Huamanzaña", "Valle Chao", "media", True),
    (6, "2010-02-11", "38593", "Lluvias", "37 viv. y 185 hab. afectados", "Huacapongo", "Valle Virú", "alta", True),
    (7, "2013-03-18", "57219", "Lluvias", "60 viv. y 300 hab. afectados", "Llacamate", "Valle Chao", "media", True),
    (8, "2013-03-17", "57524", "Inundación", "Desborde del canal Santa Clara, viviendas inundadas", "Zaraque", "sin asignar", "baja", True),
    (9, "2013-02-05", "56234", "Lluvias", "1,000 viv. y 5,000 hab. afectados", "Nuevo Chao", "Valle Chao", "alta", True),
    (10, "2014-11-06", "67338", "Lluvias", "1,200 familias y 7,500 hab. afectados",
     "Chao, Chorobal, Huamanzaña, Llacamate, Palmabal", "Valle Chao", "alta", True),
    (11, "2017-04-14", "86333", "Lluvias", "Viviendas colapsadas e inhabitables, 1,095 damnificados, canales y caminos",
     "El Inca, Chorobal, Huamanzaña, El Tizal, San Carlos, San Jorge", "Valle Chao", "alta", True),
    (12, "2017-03-22", "87568", "Inundación", "Desborde del río Virú en ambas márgenes", "Tomabal, Susanga", "Valle Virú", "alta", True),
    (13, "2017-03-21", "87563", "Inundación", "Desborde del río Virú en ambas márgenes", "La Gloria, Tomabal", "Valle Virú", "alta", True),
    (14, "2017-03-20", "87552", "Inundación", "Reincidió en las localidades antes indicadas", "La Gloria, Tomabal, Susanga", "Valle Virú", "media", True),
    (15, "2017-03-17", "87551", "Inundación", "Reincidió en las localidades antes indicadas", "La Gloria, Tomabal, Susanga", "Valle Virú", "media", True),
    (16, "2017-03-16", "87550", "Inundación", "Reincidió en las localidades antes indicadas", "La Gloria, Tomabal, Susanga", "Valle Virú", "media", True),
    (17, "2017-03-15", "87548", "Inundación", "Reincidió en las localidades antes indicadas", "La Gloria, Tomabal, Susanga", "Valle Virú", "media", True),
    (18, "2017-03-15", "87409", "Lluvias", "2,859 damnificados, 405 viv. colapsadas; daños en todo el distrito",
     "Distrito de Chao", "Valle Chao", "alta", True),
    (19, "2017-03-14", "87545", "Lluvias", "Reincidió en las localidades antes indicadas", "Distrito de Chao", "Valle Chao", "media", True),
    (20, "2017-03-14", "84747", "Lluvias", "Desborde del río Virú los días 14-17, 19, 24, 25 y 29 de marzo",
     "Distrito de Virú", "Valle Virú", "alta", True),
    (21, "2017-03-14", "83447", "Lluvias", "Reincidió en las localidades antes indicadas", "Distrito de Virú", "Valle Virú", "media", True),
    (22, "2017-02-02", "81871", "Lluvias", "256 hab. y 64 viv. afectados", "", "sin asignar", "baja", True),
]


def tabla_eventos():
    cols = ["n", "fecha", "codigo_indeci", "fenomeno", "descripcion", "localidades", "zona", "confianza_zona",
            "usar_para_lluvia"]
    df = pd.DataFrame(EVENTOS_INDECI, columns=cols)
    df["fecha"] = pd.to_datetime(df["fecha"]).dt.date
    df["anio"] = [f.year for f in df["fecha"]]
    df["fuente"] = "INDECI vía CENEPRED 2017 (Cuadro N° 01)"
    return df


def cliente_bigquery(proyecto):
    from google.cloud import bigquery
    clave = os.getenv("GCP_SA_KEY")
    if clave:
        from google.oauth2 import service_account
        cred = service_account.Credentials.from_service_account_info(json.loads(clave))
        return bigquery.Client(project=proyecto, credentials=cred)
    return bigquery.Client(project=proyecto)


def ruta_bq(proyecto, tabla):
    ds = os.getenv("BQ_DATASET") or "monitoreo_nino"
    ds = ds if "." in ds else f"{proyecto}.{ds}"
    return ds, f"{ds}.{tabla}"


def leer_tabla(env, defecto):
    tabla = os.getenv(env) or defecto
    proyecto = os.getenv("BQ_PROYECTO")
    if not proyecto:
        if not os.path.exists("datos_nino.db"):
            return None
        with sqlite3.connect("datos_nino.db") as con:
            if not con.execute("SELECT name FROM sqlite_master WHERE name=?", (tabla,)).fetchone():
                return None
            df = pd.read_sql(f'SELECT * FROM "{tabla}"', con)
    else:
        from google.api_core.exceptions import NotFound
        try:
            df = cliente_bigquery(proyecto).list_rows(ruta_bq(proyecto, tabla)[1]) \
                .to_dataframe(create_bqstorage_client=False)
        except NotFound:
            return None
    for c in ("fecha", "emitido"):
        if c in df.columns:
            df[c] = pd.to_datetime(df[c]).dt.date
    return df


def guardar(df, env, defecto, esquema_def):
    tabla = os.getenv(env) or defecto
    df = df[[n for n, _ in esquema_def]].copy()
    # Normalizar tipos según el esquema: al unir datos leídos de la base con datos nuevos,
    # una misma columna puede quedar con tipos mezclados (texto, fecha, Timestamp).
    for n, t in esquema_def:
        if t == "TIMESTAMP":
            df[n] = pd.to_datetime(df[n], utc=True)
        elif t == "DATE":
            df[n] = pd.to_datetime(df[n]).dt.date
        elif t == "FLOAT64":
            df[n] = pd.to_numeric(df[n], errors="coerce")
        elif t == "INT64":
            df[n] = pd.to_numeric(df[n], errors="coerce").astype("Int64")
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
        if {c.name for c in cliente.get_table(ref).schema} != {n for n, _ in esquema_def}:
            print(f"\nERROR: la tabla '{ref}' ya existe con otra estructura y NO se sobrescribirá.")
            sys.exit(1)
    except NotFound:
        pass
    config = bigquery.LoadJobConfig(schema=[bigquery.SchemaField(n, t) for n, t in esquema_def],
                                    write_disposition="WRITE_TRUNCATE")
    cliente.load_table_from_dataframe(df, ref, job_config=config, location=dataset.location).result()
    return f"BigQuery {ref} ({cliente.get_table(ref).num_rows} filas)"


# ------------------------------------------------------------------ Validación
def validar(zonas_df, puntos):
    ok = True
    print("\nValidación:")
    hoy = datetime.now(timezone.utc).astimezone().date()
    for z in ZONAS:
        n = sum(p["zona"] == z["zona"] for p in puntos)
        g = zonas_df[(zonas_df["zona"] == z["zona"]) & (zonas_df["emitido"] == zonas_df["emitido"].max())]
        checks = {
            "puntos en la zona": n >= 1,
            "valores físicos": bool(g["lluvia_media"].dropna().between(0, 500).all()),
            "pronóstico ≥14 días": bool(len(g) and g["dias_adelante"].max() >= 14),
            "ensemble ≥20 miembros": bool(g["ens_miembros"].dropna().ge(20).any()),
            "polígono con puntos": n >= 3,
        }
        ok &= all(checks.values())
        print(f"  {z['zona']:18s} ({n:2d} puntos): " +
              ", ".join(f"{k} {'OK' if v else 'REVISAR'}" for k, v in checks.items()))
    return ok


# ------------------------------------------------------------------ Principal
if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--validar", action="store_true")
    ap.add_argument("--reconstruir-clima", action="store_true")
    a = ap.parse_args()
    emitido = datetime.now(timezone(timedelta(hours=-5))).date()   # fecha en hora de Perú

    print("1. Construyendo grilla por zona...")
    puntos = construir_grilla()
    print("2. Pronóstico determinista (7 días atrás + 16 adelante)...")
    det = pronostico(puntos)
    print("3. Ensemble ECMWF (7 días)...")
    ens = ensemble(puntos)

    print("4. Umbrales por zona (método SENAMHI)...")
    umb = None if a.reconstruir_clima else leer_tabla("BQ_TABLA_LLUVIA_UMBRALES", "lluvia_umbrales")
    hechas = set() if umb is None else set(umb["zona"])
    pendientes = [z["zona"] for z in ZONAS if z["zona"] not in hechas]
    nuevas = []
    for zona in pendientes[:MAX_ZONAS_CLIMA_POR_EJECUCION]:
        try:
            nuevas.append(umbrales_zona(zona, puntos))
            print(f"   {zona}: umbrales del modelo calculados")
        except LimiteExcedido:
            print("   Límite de Open-Meteo alcanzado: el resto se calculará en la próxima ejecución.")
            break
    if nuevas:
        base = umb[[c for c in umb.columns if c.startswith("modelo_") or c in
                    ("zona", "dias_con_lluvia", "pct_dias_con_lluvia", "puntos_usados")]] if umb is not None else None
        umb = pd.concat([c for c in (base, pd.DataFrame(nuevas)) if c is not None], ignore_index=True)
    if umb is not None:
        umb = completar_umbrales(umb)
        print("   Guardado en:", guardar(umb, "BQ_TABLA_LLUVIA_UMBRALES", "lluvia_umbrales", ESQUEMA_UMBRALES))
        for z in UMBRALES_OFICIALES:
            f = umb[umb["zona"] == z]
            if len(f) and not pd.isna(f["modelo_p99"].iloc[0]):
                r = f.iloc[0]
                print(f"   Contraste {z}: oficial P75/P90/P95/P99 = {r.p75}/{r.p90}/{r.p95}/{r.p99} mm | "
                      f"modelo = {r.modelo_p75}/{r.modelo_p90}/{r.modelo_p95}/{r.modelo_p99} mm")
    faltan = len(pendientes) - len(nuevas)
    if faltan > 0:
        print(f"   Quedan {faltan} zonas sin umbrales del modelo (las que no tienen umbral oficial "
              f"quedarán sin categoría hasta completarse).")
    clima = umb
    print("   Guardado en:", guardar(tabla_eventos(), "BQ_TABLA_EVENTOS", "eventos_historicos", ESQUEMA_EVENTOS))

    zonas_df = agregar_zonas(det, ens, clima, emitido)
    previo = leer_tabla("BQ_TABLA_LLUVIA", "lluvia_zonas")
    if previo is not None:     # conservar emisiones anteriores; reemplazar la de hoy si se re-ejecuta
        zonas_df = pd.concat([previo[previo["emitido"] != emitido], zonas_df], ignore_index=True)

    ultima = zonas_df[zonas_df["emitido"] == emitido]
    print("\nResumen (emisión de hoy):")
    for z in ZONAS:
        g = ultima[ultima["zona"] == z["zona"]]
        pas = g[g["dias_adelante"] < 0]["lluvia_media"].sum()
        fut = g[(g["dias_adelante"] >= 0) & (g["dias_adelante"] < 7)]
        pmax = fut["prob_ens_5mm"].max() if "prob_ens_5mm" in fut else float("nan")
        print(f"  {z['zona']:18s} últimos 7 días: {pas:6.1f} mm | próximos 7 días: "
              f"{fut['lluvia_media'].sum():6.1f} mm | prob. máx. de >5 mm/día: {pmax:.0%}")

    if a.validar and not validar(zonas_df, puntos):
        sys.exit(1)
    det_ult = det.assign(emitido=emitido, actualizado_en=datetime.now(timezone.utc))
    print("\nGuardado en:", guardar(zonas_df, "BQ_TABLA_LLUVIA", "lluvia_zonas", ESQUEMA_ZONAS))
    print("Guardado en:", guardar(det_ult, "BQ_TABLA_LLUVIA_PUNTOS", "lluvia_puntos", ESQUEMA_PUNTOS))
