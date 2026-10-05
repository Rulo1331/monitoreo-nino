"""
Nowcasting de lluvia de SENAMHI (aviso a muy corto plazo) desde su geoservicio oficial IDESEP (WFS).

Producto: g_nowcasting:view_nowcasting, una "vista" que se consulta por nombre de fichero:
    nowcasting_<emisión>_analysis_<emisión>_web        -> tiempo actual
    nowcasting_<emisión>_forecast_<emisión+60min>_web  -> pronóstico a 1 hora
    nowcasting_<emisión>_forecast_<emisión+120min>_web -> pronóstico a 2 horas
Las horas del nombre están en HORA DE PERÚ, en múltiplos de 10 minutos (fecha1/fecha2 internas: UTC).
Toda emisión trae un polígono de fondo de nivel 0, así que "existe" = al menos 1 elemento.

Niveles (leyenda de la página de SENAMHI): 0 blanco (sin aviso), 1 amarillo (lluvia moderada),
2 naranja (lluvia fuerte), 3 rojo (lluvia extrema).
"""
from datetime import datetime, timedelta, timezone

import requests

import os
WFS = os.getenv("SENAMHI_WFS", "https://idesep.senamhi.gob.pe/geoserver/g_nowcasting/ows")
LIMA = timezone(timedelta(hours=-5))
CAJA = (-79.6, -10.4, -76.9, -7.7)          # lon_min, lat_min, lon_max, lat_max: nuestras zonas
NIVELES = {0: "Sin aviso", 1: "Lluvia moderada", 2: "Lluvia fuerte", 3: "Lluvia extrema"}
COLORES = {0: "#FFFFFF", 1: "#FFE600", 2: "#FF9900", 3: "#E60000"}
HORIZONTES = {"Ahora": 0, "+1 hora": 60, "+2 horas": 120}
UA = {"User-Agent": "monitoreo-nino/1.0 (uso interno; consulta cada 10 min)"}


def nombre_fichero(emision, minutos):
    e = emision.strftime("%Y%m%d-%H%M")
    if minutos == 0:
        return f"nowcasting_{e}_analysis_{e}_web"
    return f"nowcasting_{e}_forecast_{(emision + timedelta(minutes=minutos)).strftime('%Y%m%d-%H%M')}_web"


def consultar(fichero, caja=CAJA, sesion=requests):
    """GeoJSON de un fichero, recortado a la caja de nuestras zonas (mucho más liviano que el país completo)."""
    params = {"service": "WFS", "version": "1.0.0", "request": "GetFeature", "typeName": "g_nowcasting:view_nowcasting",
              "outputFormat": "application/json", "viewparams": f"fichero:{fichero}",
              "bbox": ",".join(str(v) for v in caja)}
    r = sesion.get(WFS, params=params, headers=UA, timeout=30)
    r.raise_for_status()
    return r.json()


def ultima_emision(ahora=None, intentos=9, sesion=requests):
    """Busca hacia atrás, de 10 en 10 minutos, la emisión más reciente publicada (hasta 90 min)."""
    ahora = (ahora or datetime.now(timezone.utc)).astimezone(LIMA)
    t = ahora.replace(second=0, microsecond=0, minute=ahora.minute - ahora.minute % 10)
    for _ in range(intentos):
        try:
            if consultar(nombre_fichero(t, 0), sesion=sesion).get("features"):
                return t
        except (requests.RequestException, ValueError):
            pass
        t -= timedelta(minutes=10)
    return None


def _dentro_anillo(lon, lat, anillo):
    adentro, n = False, len(anillo)
    for i in range(n):
        (x1, y1), (x2, y2) = anillo[i][:2], anillo[(i + 1) % n][:2]
        if (y1 > lat) != (y2 > lat) and lon < (x2 - x1) * (lat - y1) / (y2 - y1) + x1:
            adentro = not adentro
    return adentro


def dentro_geometria(lon, lat, geom):
    """Punto en Polygon/MultiPolygon GeoJSON, respetando los huecos (anillos interiores)."""
    poligonos = [geom["coordinates"]] if geom["type"] == "Polygon" else geom["coordinates"] if geom["type"] == "MultiPolygon" else []
    for anillos in poligonos:
        if _dentro_anillo(lon, lat, anillos[0]) and not any(_dentro_anillo(lon, lat, h) for h in anillos[1:]):
            return True
    return False


def resumen_zonas(geojson, zonas_puntos):
    """Para cada zona: nivel máximo que la toca y % de sus puntos bajo aviso (nivel >= 1).
    zonas_puntos: {zona: [(lat, lon), ...]} (los mismos puntos de la grilla de lluvia)."""
    avisos = [f for f in geojson.get("features", []) if (f["properties"].get("nivel") or 0) >= 1]
    filas = []
    for zona, pts in zonas_puntos.items():
        niveles = []
        for lat, lon in pts:
            n = max((f["properties"]["nivel"] for f in avisos if dentro_geometria(lon, lat, f["geometry"])), default=0)
            niveles.append(n)
        nmax = max(niveles, default=0)
        filas.append({"zona": zona, "nivel_max": nmax, "aviso": NIVELES.get(nmax, str(nmax)),
                      "pct_area_con_aviso": round(100 * sum(n >= 1 for n in niveles) / len(niveles), 1) if niveles else 0.0})
    return filas
