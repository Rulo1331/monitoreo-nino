"""
Recolector de la variable 5b: LLUVIA MEDIDA POR SATÉLITE (NASA GPM IMERG V07, 0.1° ≈ 11 km).

Usa exactamente las mismas zonas, umbrales y categorías SENAMHI que la variable 5a
(lee las tablas lluvia_puntos y lluvia_umbrales), para que satélite y modelo sean comparables.

Productos:
  - GPM_3IMERGDL (Late, diario): monitoreo reciente, disponible ~14 h después del día UTC.
  - GPM_3IMERGDF (Final, diario): eventos históricos (V07 cubre 2000 a sep-2025).

Tablas:
  - lluvia_satelite   : lluvia diaria por zona según el satélite (media, máximo, % del área, categorías).
  - lluvia_contraste  : pronóstico de Open-Meteo (emitido 0 a 7 días antes) frente a lo que midió el satélite.
  - eventos_satelite  : lluvia satelital en los días previos a cada emergencia INDECI (se calcula una vez).

Nota: el día de IMERG es UTC (00-24 h UTC = 19-19 h de Perú); el de Open-Meteo es hora de Perú.

Uso: python lluvia_satelite.py [--validar] [--reconstruir-eventos]
Credenciales: EARTHDATA_USER y EARTHDATA_PASS (secrets de GitHub), más las de BigQuery.
"""
import argparse, os, sys, tempfile, time
from datetime import date, datetime, timedelta, timezone
import numpy as np
import pandas as pd

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from lluvia import (ZONAS, UMBRAL_AREA_MM, categoria, nivel_aviso, leer_tabla, guardar)  # mismas reglas que 5a

PRODUCTO_RECIENTE = ("GPM_3IMERGDL", "07", "IMERG Late V07")
PRODUCTO_HISTORICO = ("GPM_3IMERGDF", "07", "IMERG Final V07")
FIN_FINAL_V07 = date(2025, 9, 30)            # la serie Final V07 terminó en esta fecha
DIAS_INICIALES = 60                          # primera ejecución: últimos 60 días
DIAS_REPASO = 3                              # cada ejecución vuelve a revisar los últimos días
VENTANA_EVENTO = 7                           # días evaluados antes de cada emergencia (incluido el día)
CUENCA_DE_VALLE = {"Valle Virú": "Cuenca alta Virú", "Valle Chao": "Cuenca alta Chao"}
# Referencia PROVISIONAL de lluvia significativa en la sierra, obtenida de los eventos INDECI:
# los desbordes del río Virú (mar-2017) tuvieron 34-47 mm acumulados en 7 días en la cuenca alta
# (IMERG); los eventos de lluvia local, 8-21 mm. Se revisará con cada temporada.
REF_ACUM_7D_CUENCA = 30.0
NIVEL = {None: -1, "Sin lluvia": 0, "Normal": 1, "Moderadamente lluvioso": 2, "Lluvioso": 3,
         "Muy lluvioso": 4, "Extremadamente lluvioso": 5}
MAX_DIAS_SIN_DATO = 4
CAJA = (-79.4, -10.4, -77.0, -7.7)           # lon_min, lat_min, lon_max, lat_max (todas las zonas)


# ------------------------------------------------------------------ Acceso a NASA
def iniciar_sesion():
    import earthaccess
    os.environ.setdefault("EARTHDATA_USERNAME", os.getenv("EARTHDATA_USER", ""))
    os.environ.setdefault("EARTHDATA_PASSWORD", os.getenv("EARTHDATA_PASS", ""))
    if not os.environ["EARTHDATA_USERNAME"] or not os.environ["EARTHDATA_PASSWORD"]:
        print("ERROR: faltan los secrets EARTHDATA_USER y/o EARTHDATA_PASS.")
        sys.exit(1)
    auth = earthaccess.login(strategy="environment")
    if not getattr(auth, "authenticated", False):
        print("ERROR: NASA Earthdata rechazó el usuario o la contraseña.")
        sys.exit(1)
    print("Sesión NASA Earthdata iniciada.")


def fecha_granulo(g):
    return date.fromisoformat(g["umm"]["TemporalExtent"]["RangeDateTime"]["BeginningDateTime"][:10])


def buscar(producto, desde, hasta):
    import earthaccess
    nombre, version, _ = producto
    granulos = earthaccess.search_data(short_name=nombre, version=version,
                                       temporal=(desde.isoformat(), hasta.isoformat()), bounding_box=CAJA)
    unicos = {}
    for g in granulos:                    # un granulo por día (si hubiera duplicados, el último)
        unicos[fecha_granulo(g)] = g
    return dict(sorted(unicos.items()))


def abrir(g):
    """Abre un archivo diario leyendo solo lo necesario por HTTPS; si falla, lo descarga completo."""
    import earthaccess, xarray as xr
    def a_dataset(origen):
        try:
            ds = xr.open_dataset(origen, engine="h5netcdf")
            if "precipitation" not in ds:
                ds = xr.open_dataset(origen, engine="h5netcdf", group="Grid")
            return ds
        except Exception:
            raise
    try:
        return a_dataset(earthaccess.open([g], show_progress=False)[0])
    except Exception as e:
        print(f"   lectura directa falló ({type(e).__name__}); descargando el archivo completo...", flush=True)
        carpeta = tempfile.mkdtemp()
        ruta = earthaccess.download([g], carpeta, show_progress=False)[0]
        return a_dataset(str(ruta))


def lluvia_en_celdas(g, celdas):
    """Lluvia diaria (mm) en cada celda (lat, lon) del conjunto."""
    ds = abrir(g)
    p = ds["precipitation"]
    if "time" in p.dims:
        p = p.isel(time=0)
    lats = np.array(sorted({c[0] for c in celdas})); lons = np.array(sorted({c[1] for c in celdas}))
    sub = p.sel(lat=slice(lats.min() - 0.06, lats.max() + 0.06),
                lon=slice(lons.min() - 0.06, lons.max() + 0.06)).load()
    salida = {}
    for la, lo in celdas:
        v = float(sub.sel(lat=la, lon=lo, method="nearest"))
        salida[(la, lo)] = None if np.isnan(v) or v < 0 else round(v, 2)
    ds.close()
    return salida


# ------------------------------------------------------------------ Zonas -> celdas IMERG
def celda_imerg(v):
    """Centro de la celda IMERG de 0.1° que contiene la coordenada (centros en x.x5)."""
    return round(np.floor(v * 10) / 10 + 0.05, 2)


def celdas_por_zona():
    """Las celdas IMERG que contienen los puntos de la grilla de la 5a (ya filtrados por polígono y altitud)."""
    pts = leer_tabla("BQ_TABLA_LLUVIA_PUNTOS", "lluvia_puntos")
    if pts is None:
        print("ERROR: no existe la tabla lluvia_puntos. Ejecuta primero la variable 5a.")
        sys.exit(1)
    pts = pts.drop_duplicates(["zona", "lat", "lon"])
    return {z: sorted({(celda_imerg(a), celda_imerg(b)) for a, b in zip(g["lat"], g["lon"])})
            for z, g in pts.groupby("zona")}


def resumen_zonas(valores_dia, zonas_celdas, umbrales):
    """Estadísticas por zona para un día: media, máximo, % del área sobre el umbral y categorías."""
    tipo = {z["zona"]: z["tipo"] for z in ZONAS}
    filas = []
    for zona, celdas in zonas_celdas.items():
        v = np.array([valores_dia[c] for c in celdas if valores_dia.get(c) is not None])
        u = umbrales.get(zona)
        umbral_area = UMBRAL_AREA_MM.get(tipo.get(zona, "local"), 1.0)
        media = round(float(v.mean()), 2) if len(v) else None
        maximo = round(float(v.max()), 2) if len(v) else None
        filas.append({"zona": zona, "celdas": int(len(v)), "lluvia_media": media, "lluvia_max": maximo,
                      "umbral_mm": umbral_area,
                      "pct_area_sobre_umbral": round(float((v >= umbral_area).mean() * 100), 1) if len(v) else None,
                      "categoria_media": categoria(media, u), "categoria_max": categoria(maximo, u),
                      "nivel_aviso_max": nivel_aviso(maximo, u)})
    return filas


def descargar_dias(producto, desde, hasta, zonas_celdas, umbrales):
    granulos = buscar(producto, desde, hasta)
    todas = sorted({c for cs in zonas_celdas.values() for c in cs})
    filas = []
    for d, g in granulos.items():
        valores = lluvia_en_celdas(g, todas)
        for f in resumen_zonas(valores, zonas_celdas, umbrales):
            filas.append({**f, "fecha": d, "producto": producto[2]})
        print(f"   {producto[2]} {d}: OK", flush=True)
        time.sleep(0.3)
    return pd.DataFrame(filas)


def acumulados(sat):
    """Lluvia acumulada de 3 y 7 días por zona (media de la zona). Solo suma días consecutivos con dato:
    si falta algún día de la ventana, el acumulado queda vacío para no subestimarlo."""
    salida = []
    for zona, g in sat.groupby("zona"):
        g = g.sort_values("fecha").copy()
        serie = g.set_index(pd.to_datetime(g["fecha"]))["lluvia_media"].asfreq("D")
        for n in (3, 7):
            acum = serie.rolling(n, min_periods=n).sum().round(2)
            g[f"acum_{n}d"] = acum.reindex(pd.to_datetime(g["fecha"])).values
        salida.append(g)
    return pd.concat(salida, ignore_index=True)


# ------------------------------------------------------------------ Contraste pronóstico vs satélite
def contraste(sat):
    pron = leer_tabla("BQ_TABLA_LLUVIA", "lluvia_zonas")
    if pron is None or sat.empty:
        return pd.DataFrame()
    pron = pron[(pron["dias_adelante"] >= 0) & (pron["dias_adelante"] <= 7)]
    c = pron.merge(sat[["zona", "fecha", "lluvia_media", "lluvia_max"]], on=["zona", "fecha"],
                   suffixes=("_pronostico", "_satelite"))
    c = c.dropna(subset=["lluvia_media_pronostico", "lluvia_media_satelite"])
    c["error_media"] = (c["lluvia_media_pronostico"] - c["lluvia_media_satelite"]).round(2)
    c["fuente"] = "Open-Meteo (pronóstico) vs NASA IMERG Late (satélite)"
    c["actualizado_en"] = datetime.now(timezone.utc)
    return c


# ------------------------------------------------------------------ Eventos históricos INDECI
def ventana(diarios, zona, fecha, dias):
    return diarios[(diarios["zona"] == zona) & (diarios["fecha"] <= fecha) &
                   (diarios["fecha"] > fecha - timedelta(days=dias))]


def indicio_origen(cat_valle, cat_cuenca, acum_7d_cuenca=None):
    """Pista (no diagnóstico) del origen del daño. Valle: lluvia diaria ≥ 'Muy lluvioso' (umbral oficial).
    Sierra: categoría diaria ≥ 'Muy lluvioso' (umbral provisional del modelo) o acumulado de 7 días
    ≥ REF_ACUM_7D_CUENCA, porque lo que satura la cuenca es la lluvia de varios días."""
    valle = NIVEL.get(cat_valle, -1) >= 4
    cuenca = NIVEL.get(cat_cuenca, -1) >= 4 or (acum_7d_cuenca is not None and not pd.isna(acum_7d_cuenca)
                                                and acum_7d_cuenca >= REF_ACUM_7D_CUENCA)
    if valle and cuenca:
        return "Lluvia fuerte en valle y sierra"
    if valle:
        return "Lluvia local (valle)"
    if cuenca:
        return "Lluvia en la sierra (posible crecida o huaico)"
    return "No concluyente"


def recalcular_indicios(ev):
    """Aplica la regla vigente a los eventos ya guardados (sin volver a descargar datos)."""
    ev = ev.copy()
    ev["indicio_origen"] = [indicio_origen(a, b, c) for a, b, c in
                            zip(ev["categoria_max"], ev["categoria_cuenca"], ev["acum_7d_cuenca"])]
    return ev


def eventos_satelite(zonas_celdas, umbrales):
    ev = leer_tabla("BQ_TABLA_EVENTOS", "eventos_historicos")
    if ev is None:
        print("   No existe la tabla eventos_historicos (se crea en la variable 5a).")
        return None
    ev = ev[(ev["usar_para_lluvia"].astype(bool)) & (ev["zona"].isin(zonas_celdas.keys())) &
            (ev["fecha"] <= FIN_FINAL_V07)]
    dias = sorted({f - timedelta(days=k) for f in ev["fecha"] for k in range(VENTANA_EVENTO)})
    tramos, ini, prev = [], dias[0], dias[0]
    for d in dias[1:] + [None]:
        if d is None or (d - prev).days > 1:
            tramos.append((ini, prev)); ini = d
        prev = d if d is not None else prev
    diarios = pd.concat([descargar_dias(PRODUCTO_HISTORICO, a, b, zonas_celdas, umbrales) for a, b in tramos],
                        ignore_index=True)
    filas = []
    for _, e in ev.iterrows():
        zona, cuenca = e["zona"], CUENCA_DE_VALLE.get(e["zona"])
        v7, v3 = ventana(diarios, zona, e["fecha"], 7), ventana(diarios, zona, e["fecha"], 3)
        u = umbrales.get(zona)
        mx = v7["lluvia_max"].max() if len(v7) else None
        fila = {"n": int(e["n"]), "fecha": e["fecha"], "zona": zona, "localidades": e["localidades"],
                "confianza_zona": e["confianza_zona"], "dias_con_datos": int(len(v7)),
                "lluvia_media_max_dia": None if v7.empty else round(float(v7["lluvia_media"].max()), 2),
                "lluvia_max_dia": None if mx is None or pd.isna(mx) else round(float(mx), 2),
                "acumulado_media": None if v3.empty else round(float(v3["lluvia_media"].sum()), 2),
                "acum_7d_valle": None if v7.empty else round(float(v7["lluvia_media"].sum()), 2),
                "categoria_max": categoria(mx, u), "nivel_aviso_max": nivel_aviso(mx, u),
                "cuenca_alta": cuenca, "fuente": PRODUCTO_HISTORICO[2]}
        if cuenca:
            c7, c3 = ventana(diarios, cuenca, e["fecha"], 7), ventana(diarios, cuenca, e["fecha"], 3)
            cmx = c7["lluvia_media"].max() if len(c7) else None      # media de la cuenca: lluvia generalizada
            fila.update({"max_dia_cuenca": None if cmx is None or pd.isna(cmx) else round(float(cmx), 2),
                         "acum_3d_cuenca": None if c3.empty else round(float(c3["lluvia_media"].sum()), 2),
                         "acum_7d_cuenca": None if c7.empty else round(float(c7["lluvia_media"].sum()), 2),
                         "categoria_cuenca": categoria(cmx, umbrales.get(cuenca))})
        else:
            fila.update({"max_dia_cuenca": None, "acum_3d_cuenca": None, "acum_7d_cuenca": None, "categoria_cuenca": None})
        fila["indicio_origen"] = indicio_origen(fila["categoria_max"], fila["categoria_cuenca"], fila["acum_7d_cuenca"])
        filas.append(fila)
    return pd.DataFrame(filas)


# ------------------------------------------------------------------ Esquemas
ESQUEMA_SAT = [("zona", "STRING"), ("fecha", "DATE"), ("producto", "STRING"), ("celdas", "INT64"),
               ("lluvia_media", "FLOAT64"), ("lluvia_max", "FLOAT64"), ("umbral_mm", "FLOAT64"),
               ("pct_area_sobre_umbral", "FLOAT64"), ("categoria_media", "STRING"), ("categoria_max", "STRING"),
               ("nivel_aviso_max", "STRING"), ("actualizado_en", "TIMESTAMP"),
               ("acum_3d", "FLOAT64"), ("acum_7d", "FLOAT64")]
ESQUEMA_CONTRASTE = [("zona", "STRING"), ("fecha", "DATE"), ("emitido", "DATE"), ("dias_adelante", "INT64"),
                     ("lluvia_media_pronostico", "FLOAT64"), ("lluvia_max_pronostico", "FLOAT64"),
                     ("lluvia_media_satelite", "FLOAT64"), ("lluvia_max_satelite", "FLOAT64"),
                     ("error_media", "FLOAT64"), ("fuente", "STRING"), ("actualizado_en", "TIMESTAMP")]
ESQUEMA_EVENTOS_SAT = [("n", "INT64"), ("fecha", "DATE"), ("zona", "STRING"), ("localidades", "STRING"),
                       ("confianza_zona", "STRING"), ("dias_con_datos", "INT64"),
                       ("lluvia_media_max_dia", "FLOAT64"), ("lluvia_max_dia", "FLOAT64"),
                       ("acumulado_media", "FLOAT64"), ("categoria_max", "STRING"), ("nivel_aviso_max", "STRING"),
                       ("fuente", "STRING"), ("acum_7d_valle", "FLOAT64"), ("cuenca_alta", "STRING"),
                       ("max_dia_cuenca", "FLOAT64"), ("acum_3d_cuenca", "FLOAT64"), ("acum_7d_cuenca", "FLOAT64"),
                       ("categoria_cuenca", "STRING"), ("indicio_origen", "STRING")]


# ------------------------------------------------------------------ Principal
def validar(sat, zonas_celdas):
    ok = True
    print("\nValidación:")
    hoy = datetime.now(timezone.utc).date()
    for zona, celdas in zonas_celdas.items():
        g = sat[sat["zona"] == zona]
        ultimo = g["fecha"].max() if len(g) else None
        checks = {"celdas IMERG": len(celdas) >= 1,
                  "valores físicos": bool(g["lluvia_max"].dropna().between(0, 500).all()),
                  "dato reciente": ultimo is not None and (hoy - ultimo).days <= MAX_DIAS_SIN_DATO}
        ok &= all(checks.values())
        print(f"  {zona:18s} ({len(celdas):2d} celdas, último {ultimo}): " +
              ", ".join(f"{k} {'OK' if v else 'REVISAR'}" for k, v in checks.items()))
    return ok


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--validar", action="store_true")
    ap.add_argument("--reconstruir-eventos", action="store_true")
    a = ap.parse_args()

    iniciar_sesion()
    zonas_celdas = celdas_por_zona()
    print("Celdas IMERG por zona: " + ", ".join(f"{z} {len(c)}" for z, c in zonas_celdas.items()))
    umb = leer_tabla("BQ_TABLA_LLUVIA_UMBRALES", "lluvia_umbrales")
    umbrales = {} if umb is None else {r["zona"]: r for r in umb.to_dict("records")}

    # 1. Lluvia reciente (IMERG Late)
    hoy = datetime.now(timezone.utc).date()
    previo = leer_tabla("BQ_TABLA_LLUVIA_SAT", "lluvia_satelite")
    desde = hoy - timedelta(days=DIAS_INICIALES) if previo is None or previo.empty \
        else previo["fecha"].max() - timedelta(days=DIAS_REPASO)
    print(f"1. Satélite reciente ({PRODUCTO_RECIENTE[2]}) desde {desde}...")
    nuevos = descargar_dias(PRODUCTO_RECIENTE, desde, hoy - timedelta(days=1), zonas_celdas, umbrales)
    if not nuevos.empty:
        nuevos["actualizado_en"] = datetime.now(timezone.utc)
    sat = pd.concat([d for d in (previo, nuevos) if d is not None and not d.empty], ignore_index=True)
    sat = sat.drop_duplicates(["zona", "fecha"], keep="last").sort_values(["zona", "fecha"])
    sat = acumulados(sat)

    # 2. Contraste con el pronóstico
    print("2. Contraste pronóstico vs satélite...")
    con = contraste(sat)
    if not con.empty:
        resumen = con[con["dias_adelante"] == 1].groupby("zona").agg(
            pares=("error_media", "size"), pron=("lluvia_media_pronostico", "mean"),
            sat=("lluvia_media_satelite", "mean"), error=("error_media", "mean"))
        if resumen.empty:
            print(f"   Hay {len(con)} pares del mismo día (pronóstico de 0 días), pero aún no pronósticos emitidos "
                  f"con 1 día de anticipación. El resumen aparecerá en los próximos días.")
        else:
            print("   Pronóstico a 1 día vs satélite (promedios diarios, mm):")
        for z, r in resumen.iterrows():
            print(f"   {z:18s} {int(r.pares):3d} días | pronóstico {r.pron:5.2f} | satélite {r.sat:5.2f} | "
                  f"diferencia {r.error:+5.2f}")
    else:
        print("   Aún no hay pronósticos y satélite del mismo día para comparar (se acumulan con los días).")

    # 3. Eventos históricos (una sola vez)
    ev_sat = None if a.reconstruir_eventos else leer_tabla("BQ_TABLA_EVENTOS_SAT", "eventos_satelite")
    if ev_sat is not None and "indicio_origen" not in ev_sat.columns:
        print("3. La tabla de eventos es de la versión anterior: se recalcula con la cuenca alta.")
        ev_sat = None
    if ev_sat is None:
        print(f"3. Eventos INDECI con {PRODUCTO_HISTORICO[2]} (solo la primera vez)...")
        ev_sat = eventos_satelite(zonas_celdas, umbrales)
        if ev_sat is not None and not ev_sat.empty:
            print("   Guardado en:", guardar(ev_sat, "BQ_TABLA_EVENTOS_SAT", "eventos_satelite", ESQUEMA_EVENTOS_SAT))
            print("   fecha       zona        valle máx.  cuenca: 3 d / 7 d     indicio")
            for _, r in ev_sat.sort_values("fecha").iterrows():
                cuenca = (f"{r.acum_3d_cuenca:5.1f} / {r.acum_7d_cuenca:5.1f} mm" if pd.notna(r.acum_3d_cuenca)
                          else "      sin dato     ")
                print(f"   {r.fecha} {r.zona:11s} {r.lluvia_max_dia:6.1f} mm  {cuenca}  {r.indicio_origen}")
    else:
        nuevo_ev = recalcular_indicios(ev_sat)
        if not nuevo_ev["indicio_origen"].equals(ev_sat["indicio_origen"]):
            print("3. Eventos INDECI: se actualizan los indicios con la regla vigente (sin descargar datos).")
            print("   Guardado en:", guardar(nuevo_ev, "BQ_TABLA_EVENTOS_SAT", "eventos_satelite", ESQUEMA_EVENTOS_SAT))
            for _, r in nuevo_ev.sort_values("fecha").iterrows():
                print(f"   {r.fecha} {r.zona:11s} cuenca 7 d {r.acum_7d_cuenca:5.1f} mm -> {r.indicio_origen}")
        else:
            print("3. Eventos INDECI: ya calculados (usa --reconstruir-eventos para repetir).")

    if a.validar and not validar(sat, zonas_celdas):
        sys.exit(1)
    print("\nGuardado en:", guardar(sat, "BQ_TABLA_LLUVIA_SAT", "lluvia_satelite", ESQUEMA_SAT))
    if not con.empty:
        print("Guardado en:", guardar(con, "BQ_TABLA_LLUVIA_CONTRASTE", "lluvia_contraste", ESQUEMA_CONTRASTE))
