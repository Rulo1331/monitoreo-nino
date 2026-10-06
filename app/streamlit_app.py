"""
Dashboard de monitoreo de El Niño Costero – Virú, Chao y costa norte del Perú.
Basado en el Modelo C v2 (sala de análisis). Lee las tablas que llenan los recolectores.

Ejecución local:   streamlit run app/streamlit_app.py
Credenciales:      .streamlit/secrets.toml (GCP_SA_KEY, BQ_PROYECTO, BQ_DATASET).
                   Sin credenciales lee la base local de prueba 'datos_nino.db'.
"""
import os
import sys
from datetime import timedelta
from pathlib import Path

import numpy as np
import pandas as pd
import plotly.graph_objects as go
import streamlit as st

st.set_page_config(page_title="Monitoreo El Niño – Virú y Chao", page_icon="🌊", layout="wide")

# ------------------------------------------------------------------ Configuración y datos
RAIZ = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(RAIZ / "recolectores"))

# --- Credenciales: se leen de st.secrets y se registran los problemas en lugar de ocultarlos
DIAG = {"claves_secrets": [], "error_secrets": None}
try:
    DIAG["claves_secrets"] = list(st.secrets.keys())
    for clave in ("GCP_SA_KEY", "BQ_PROYECTO", "BQ_DATASET", "NODERED_URL", "NODERED_TOKEN"):
        if clave in st.secrets:
            os.environ[clave] = str(st.secrets[clave]).strip()
except Exception as e:  # sin secrets, o el bloque de secrets tiene un error de formato TOML
    DIAG["error_secrets"] = ("no hay secrets configurados" if "SecretNotFound" in type(e).__name__
                             else f"error de formato en los secrets: {type(e).__name__}: {e}")

import lluvia as L          # zonas, umbrales y lectura de tablas (misma fuente que los recolectores)
import tsm_costera as T     # coordenadas de los puertos
import senamhi_nowcasting as SN   # aviso de lluvia a muy corto plazo (SENAMHI, IDESEP)


@st.cache_data(ttl=120, show_spinner=False)
def nowcasting(base, token):
    """Último aviso de SENAMHI guardado por Node-RED (que consulta IDESEP cada 5 minutos).
    base y token son parte de la llave de la caché: si cambian los secrets, se vuelve a consultar.
    Devuelve (emisión en hora de Perú, {horizonte: GeoJSON}, mensaje de error)."""
    import requests
    from datetime import datetime
    if not base or not token:
        return None, {}, "faltan NODERED_URL y/o NODERED_TOKEN en los secrets"
    try:
        r = requests.get(base.rstrip("/") + "/nowcasting/ultimo", params={"token": token}, timeout=8)
    except requests.RequestException as e:
        return None, {}, f"Node-RED no respondió ({type(e).__name__})"
    if r.status_code != 200:
        return None, {}, f"Node-RED respondió HTTP {r.status_code}: {r.text[:120]}"
    d = r.json()
    emision = datetime.strptime(d["emision"], "%Y%m%d-%H%M")
    geo = {k: {"features": v.get("avisos", []), **({} if v.get("disponible") else {"error": "no disponible"})}
           for k, v in d.get("horizontes", {}).items()}
    return emision, geo, None


AZUL, ROJO, AMBAR, GRIS, FONDO, PANEL, LINEA = "#7CC6E8", "#F06B4F", "#F2A541", "#9FB0C0", "#0C141C", "#131E28", "#22313F"
TEMP = ["DJF", "JFM", "FMA", "MAM", "AMJ", "MJJ", "JJA", "JAS", "ASO", "SON", "OND", "NDJ"]
MESES = ["Ene", "Feb", "Mar", "Abr", "May", "Jun", "Jul", "Ago", "Sep", "Oct", "Nov", "Dic"]


@st.cache_data(ttl=3600, show_spinner="Leyendo datos...")
def tabla(nombre, proyecto, dataset, firma_clave):
    """Devuelve (DataFrame, error). Nunca oculta el motivo de un fallo.
    proyecto, dataset y firma_clave forman parte de la llave de la caché: si cambian los secrets,
    los datos se vuelven a leer en vez de reutilizar un fallo anterior."""
    if not proyecto:
        df = L.leer_tabla("__SIN_ENV__", nombre)          # modo local de prueba (datos_nino.db)
        return (pd.DataFrame(), "sin BQ_PROYECTO y sin base local") if df is None else (df, None)
    try:
        from google.api_core.exceptions import NotFound
        cliente = L.cliente_bigquery(proyecto)
        ruta = L.ruta_bq(proyecto, nombre)[1]
        df = cliente.list_rows(ruta).to_dataframe(create_bqstorage_client=False)
        for c in ("fecha", "emitido"):
            if c in df.columns:
                df[c] = pd.to_datetime(df[c]).dt.date
        return df, None
    except NotFound:
        return pd.DataFrame(), f"no existe {L.ruta_bq(proyecto, nombre)[1]}"
    except Exception as e:
        return pd.DataFrame(), f"{type(e).__name__}: {str(e)[:300]}"


def diagnostico(errores):
    """Explica en pantalla por qué no se pudieron leer los datos."""
    st.error("No se pudieron leer los datos. Diagnóstico:")
    proyecto, dataset = os.getenv("BQ_PROYECTO"), os.getenv("BQ_DATASET") or "monitoreo_nino (valor por defecto)"
    if DIAG["error_secrets"]:
        st.markdown(f"- ❌ **Secrets:** {DIAG['error_secrets']}")
    st.markdown(f"- Claves encontradas en los secrets: `{', '.join(DIAG['claves_secrets']) or 'ninguna'}` "
                f"(se necesitan `GCP_SA_KEY`, `BQ_PROYECTO` y `BQ_DATASET`)")
    st.markdown(f"- Proyecto: `{proyecto or 'NO DEFINIDO'}` · Dataset: `{dataset}`")
    if os.getenv("GCP_SA_KEY"):
        try:
            import json
            correo = json.loads(os.environ["GCP_SA_KEY"])["client_email"]
            st.markdown(f"- ✅ Clave JSON válida · cuenta de servicio: `{correo}`")
        except Exception as e:
            st.markdown(f"- ❌ **GCP_SA_KEY no es un JSON válido:** `{type(e).__name__}: {str(e)[:150]}`")
    elif proyecto:
        st.markdown("- ❌ **Falta GCP_SA_KEY** en los secrets")
    if proyecto:
        try:
            cliente = L.cliente_bigquery(proyecto)
            datasets = [d.dataset_id for d in cliente.list_datasets(proyecto)]
            st.markdown(f"- Datasets que esta cuenta puede ver en `{proyecto}`: `{', '.join(datasets) or 'ninguno'}`")
            ds = L.ruta_bq(proyecto, "x")[0]
            tablas = [t.table_id for t in cliente.list_tables(ds)]
            st.markdown(f"- Tablas en `{ds}`: `{', '.join(tablas) or 'ninguna'}`")
        except Exception as e:
            st.markdown(f"- ❌ **Error al consultar BigQuery:** `{type(e).__name__}: {str(e)[:300]}`")
    st.markdown("**Detalle por tabla:**")
    for n, err in errores.items():
        st.markdown(f"- `{n}`: {err}")
    st.info("Después de corregir los secrets, usa el botón de abajo para volver a cargar los datos.")
    if st.button("Volver a cargar los datos"):
        st.cache_data.clear()
        st.rerun()


def estilo(fig, titulo_x, titulo_y, alto=360):
    """Estilo común: fondo oscuro y ejes SIEMPRE rotulados."""
    fig.update_layout(height=alto, paper_bgcolor=PANEL, plot_bgcolor=FONDO, font=dict(color="#E6EDF3", size=12),
                      margin=dict(l=60, r=20, t=30, b=55), legend=dict(orientation="h", y=-0.25),
                      hoverlabel=dict(bgcolor=FONDO))
    fig.update_xaxes(title_text=titulo_x, gridcolor=LINEA, zeroline=False)
    fig.update_yaxes(title_text=titulo_y, gridcolor=LINEA, zeroline=False)
    return fig


ESRI = "https://server.arcgisonline.com/ArcGIS/rest/services/{}/MapServer/tile/{{z}}/{{y}}/{{x}}"


def capas_base(mapa, primero):
    """Capas base que no requieren clave de API (CARTO ahora la exige)."""
    import folium
    capas = {
        "relieve": dict(tiles="OpenTopoMap", name="Relieve (cuencas y curvas de nivel)"),
        "satelite": dict(tiles="Esri.WorldImagery", name="Satélite"),
        "oscuro": dict(tiles=ESRI.format("Canvas/World_Dark_Gray_Base"), attr="Tiles © Esri — Esri, HERE, Garmin, © OpenStreetMap", name="Gris oscuro"),
    }
    for k in [primero] + [c for c in capas if c != primero]:
        folium.TileLayer(**capas[k]).add_to(mapa)


def bandas(fig, tramos, eje_x0, eje_x1):
    for y0, y1, color, texto in tramos:
        fig.add_shape(type="rect", x0=eje_x0, x1=eje_x1, y0=y0, y1=y1, fillcolor=color, line_width=0, layer="below")
        fig.add_annotation(x=eje_x1, y=(y0 + y1) / 2, text=texto, showarrow=False, xanchor="right", font=dict(size=10, color=GRIS))


NOMBRES = ["icen", "roni", "nino_semanal", "tsm_costera", "lluvia_zonas", "lluvia_puntos", "lluvia_umbrales", "lluvia_satelite", "eventos_satelite"]
import hashlib
FIRMA = hashlib.sha256((os.getenv("GCP_SA_KEY") or "").encode()).hexdigest()[:12]
LEIDAS = {n: tabla(n, os.getenv("BQ_PROYECTO"), os.getenv("BQ_DATASET"), FIRMA) for n in NOMBRES}
icen, roni, sem = (LEIDAS[n][0] for n in ("icen", "roni", "nino_semanal"))
tsm, zonas_df, puntos = (LEIDAS[n][0] for n in ("tsm_costera", "lluvia_zonas", "lluvia_puntos"))
umbr, sat, ev = (LEIDAS[n][0] for n in ("lluvia_umbrales", "lluvia_satelite", "eventos_satelite"))
esenciales = ["icen", "roni", "tsm_costera", "lluvia_zonas", "lluvia_puntos"]
if any(LEIDAS[n][0].empty for n in esenciales):
    diagnostico({n: LEIDAS[n][1] or "OK" for n in NOMBRES})
    st.stop()

# BigQuery no garantiza el orden de las filas (y los días rellenados del histórico se agregaron al final).
# Se ordena cada serie por fecha y se descartan días repetidos: así las líneas no "saltan" hacia atrás.
if not tsm.empty:
    tsm = tsm.sort_values(["punto", "fecha"]).drop_duplicates(["punto", "fecha"], keep="last").reset_index(drop=True)
icen = icen.sort_values(["anio", "mes"]).drop_duplicates(["anio", "mes"], keep="last").reset_index(drop=True)
if not sem.empty and "fecha" in sem:
    sem = sem.sort_values("fecha").drop_duplicates("fecha", keep="last").reset_index(drop=True)
if not sat.empty and {"zona", "fecha"} <= set(sat.columns):
    sat = sat.sort_values(["zona", "fecha"]).reset_index(drop=True)

# Preparación
icen = icen.sort_values(["anio", "mes"])
icen["fecha"] = pd.to_datetime(dict(year=icen["anio"], month=icen["mes"], day=1))
roni = roni.sort_values(["anio", "mes_central"])
tsm["fecha"] = pd.to_datetime(tsm["fecha"])
ult_em = zonas_df["emitido"].max()
z_hoy = zonas_df[zonas_df["emitido"] == ult_em]
resumen_z = pd.DataFrame([{
    "zona": z["zona"], "tipo": z["tipo"],
    "pasado": z_hoy[(z_hoy.zona == z["zona"]) & (z_hoy.dias_adelante < 0)]["lluvia_media"].sum(),
    "futuro": z_hoy[(z_hoy.zona == z["zona"]) & (z_hoy.dias_adelante.between(0, 6))]["lluvia_media"].sum(),
    "prob": 100 * z_hoy[(z_hoy.zona == z["zona"]) & (z_hoy.dias_adelante.between(0, 6))]["prob_ens_5mm"].max(),
} for z in L.ZONAS]).set_index("zona")
ult_icen = icen.dropna(subset=["icen"]).iloc[-1]
ult_mes = icen.dropna(subset=["anom_nino12"]).iloc[-1]
ult_roni = roni.iloc[-1]
ult_dia = tsm["fecha"].max()
tsm_hoy = tsm[tsm["fecha"] == ult_dia].set_index("punto")

# ------------------------------------------------------------------ Encabezado
st.markdown(f"<span style='font-family:monospace;color:{AZUL};letter-spacing:.1em;font-size:12px'>SALA DE ANÁLISIS · EL NIÑO COSTERO</span>",
            unsafe_allow_html=True)
st.title("Océano, lluvia y territorio: Virú, Chao y costa norte")
st.caption(f"Mar al {ult_dia:%d-%b-%Y} · Lluvia emitida {ult_em} · ICEN {MESES[int(ult_icen.mes) - 1]}-{int(ult_icen.anio)} · "
           f"Semanal {sem['semana'].max() if not sem.empty else '—'}")

v1, v2, v3, v4, v5 = st.tabs(["Panorama e historia", "Territorio y lluvia", "Océano", "Eventos históricos", "Resumen automático"])

# ================================================================== VISTA 1: PANORAMA E HISTORIA
with v1:
    c1, c2, c3 = st.columns([3, 1.2, 1.4])
    atajos = {"1972-73": 1972, "1982-83": 1982, "1997-98": 1997, "2015-16": 2015, "2017 (costero)": 2017, "2023-24": 2023, "Otro año": None}
    eleccion = c1.radio("Comparar 2026 con", list(atajos), index=2, horizontal=True)
    anio = atajos[eleccion] or c2.number_input("Año", 1950, 2025, 1997, step=1)
    modo = c3.radio("Modo", ["Mismo periodo", "Pico del año"], horizontal=True)

    def valor_roni(a):
        s = roni[roni.anio == a].set_index("mes_central")["roni"]
        if s.empty:
            return None, ""
        if modo == "Pico del año":
            return s.max(), TEMP[int(s.idxmax()) - 1]
        m = int(ult_roni.mes_central)
        return (s.get(m), TEMP[m - 1]) if m in s.index else (None, "")

    def valor_icen(a):
        s = icen[icen.anio == a].dropna(subset=["icen"]).set_index("mes")["icen"]
        if s.empty:
            return None, ""
        if modo == "Pico del año":
            return s.max(), MESES[int(s.idxmax()) - 1]
        m = int(ult_icen.mes)
        return (s.get(m), MESES[m - 1]) if m in s.index else (None, "")

    def valor_mar(a, punto="Salaverry"):
        s = tsm[(tsm.punto == punto) & (tsm.fecha.dt.year == a)]
        if s.empty:
            return None, "sin dato: la serie empieza en 1991"
        if modo == "Pico del año":
            f = s.loc[s["anom_1991_2020"].idxmax()]
            return f["anom_1991_2020"], f"{f['fecha']:%d-%b}"
        d = ult_dia.replace(year=a)
        cerca = s[(s.fecha >= d - timedelta(days=3)) & (s.fecha <= d + timedelta(days=3))]
        return (cerca["anom_1991_2020"].mean(), f"{d:%d-%b}") if not cerca.empty else (None, "")

    def valor_sierra(a):
        if a < 2000:
            return None, "sin dato: satélite desde 2000"
        e = ev[(pd.to_datetime(ev["fecha"]).dt.year == a) & (ev["cuenca_alta"] == "Cuenca alta Virú")] if not ev.empty else ev
        if e is None or e.empty:
            return None, "sin evento registrado ese año"
        return e["acum_7d_cuenca"].max(), "previo a emergencia INDECI"

    k1, k2, k3, k4 = st.columns(4)
    for col, titulo, hoy, fn, unidad, nota in [
        (k1, "El Niño global · RONI", ult_roni.roni, valor_roni, "°C", f"{ult_roni.temporada} {int(ult_roni.anio)}"),
        (k2, "El Niño costero · ICEN", ult_icen.icen, valor_icen, "°C", f"{ult_icen.categoria} · {MESES[int(ult_icen.mes) - 1]}"),
        (k3, "Mar frente a Salaverry", tsm_hoy.loc["Salaverry", "anom_1991_2020"], valor_mar, "°C", f"anomalía al {ult_dia:%d-%b}"),
        (k4, "Sierra de Virú · próx. 7 días", resumen_z.loc["Cuenca alta Virú", "futuro"], valor_sierra, "mm", "pronóstico (comparación: satélite)"),
    ]:
        comp, cuando = fn(anio)
        delta = None if comp is None or pd.isna(comp) else f"{hoy - comp:+.2f} {unidad} vs {anio}"
        col.metric(titulo, f"{hoy:+.2f} {unidad}" if unidad == "°C" else f"{hoy:.1f} {unidad}", delta, delta_color="off")
        col.caption(f"{nota} · {anio}: " + (f"**{comp:+.2f} {unidad}** ({cuando})" if comp is not None and not pd.isna(comp) and unidad == "°C"
                                             else f"**{comp:.1f} {unidad}** ({cuando})" if comp is not None and not pd.isna(comp) else cuando or "sin dato"))

    g1, g2 = st.columns(2)
    with g1:
        fig = go.Figure()
        bandas(fig, [(-2.5, -0.5, "#132538", "La Niña"), (0.5, 1, "#1E2A2C", "Débil"), (1, 1.5, "#2A2E26", "Moderado"),
                     (1.5, 2, "#3A2C22", "Fuerte"), (2, 3, "#46241F", "Muy fuerte")], -0.5, 11.5)
        for a, col_, ancho, nombre in [(anio, AZUL, 3, str(anio)), (int(ult_roni.anio), ROJO, 4, str(int(ult_roni.anio)))]:
            s = roni[roni.anio == a].sort_values("mes_central")
            fig.add_scatter(x=[TEMP[m - 1] for m in s.mes_central], y=s.roni, mode="lines+markers", name=nombre,
                            line=dict(color=col_, width=ancho, dash="dash" if a == anio else "solid"))
        fig.update_xaxes(categoryorder="array", categoryarray=TEMP)
        st.markdown("**Años análogos · El Niño global (RONI)**")
        st.plotly_chart(width="stretch", figure_or_data=estilo(fig, "Trimestre móvil (DJF = dic-ene-feb)", "RONI (°C)"))
    with g2:
        fig = go.Figure()
        bandas(fig, [(-1.5, 0.5, "#16222D", "Neutra"), (0.5, 1.3, "#1E2A2C", "Débil"), (1.3, 2.1, "#2A2E26", "Moderada"),
                     (2.1, 3.0, "#3A2C22", "Fuerte"), (3.0, 4.5, "#46241F", "Extraordinaria")], -0.5, 11.5)
        for a, col_, nombre in [(anio, AZUL, str(anio)), (int(ult_icen.anio), ROJO, str(int(ult_icen.anio)))]:
            s = icen[icen.anio == a]
            fig.add_scatter(x=[MESES[m - 1] for m in s.mes], y=s.icen, mode="lines+markers", name=f"ICEN {nombre}",
                            line=dict(color=col_, width=4 if col_ == ROJO else 3, dash="dash" if col_ == AZUL else "solid"))
        fig.update_xaxes(categoryorder="array", categoryarray=MESES)
        st.markdown("**Años análogos · El Niño costero (ICEN)**")
        st.plotly_chart(width="stretch", figure_or_data=estilo(fig, "Mes", "ICEN (°C)"))

    if anio >= 1991:
        fig = go.Figure()
        for a, col_ in [(anio, AZUL), (ult_dia.year, ROJO)]:
            s = tsm[(tsm.punto == "Salaverry") & (tsm.fecha.dt.year == a)]
            fig.add_scatter(x=s.fecha.dt.dayofyear, y=s.anom_1991_2020, mode="lines", name=str(a), line=dict(color=col_, width=2.5 if col_ == ROJO else 1.8))
        fig.add_hline(y=0, line_color=GRIS)
        st.markdown(f"**Mar frente a Salaverry: {ult_dia.year} vs {anio}** (anomalía diaria)")
        st.plotly_chart(width="stretch", figure_or_data=estilo(fig, "Día del año", "Anomalía de temperatura (°C)", 320))

# ================================================================== VISTA 2: TERRITORIO Y LLUVIA
with v2:
    import folium
    from streamlit_folium import st_folium

    p_ult = puntos[puntos["emitido"] == puntos["emitido"].max()].drop_duplicates(["zona", "lat", "lon"])
    st.subheader("Aviso de lluvia SENAMHI en tiempo real")
    emision_nc, datos_nc, error_nc = nowcasting(os.getenv("NODERED_URL"), os.getenv("NODERED_TOKEN"))
    geo_nc = None
    if emision_nc is None:
        st.warning(f"Aviso de SENAMHI no disponible: {error_nc}.")
    else:
        from datetime import datetime as _dt
        atraso = (_dt.now(SN.LIMA).replace(tzinfo=None) - emision_nc).total_seconds() / 60
        if atraso > 45:
            st.warning(f"El último aviso guardado es de hace {atraso:.0f} minutos: revisar el flujo de Node-RED.")
        hz = st.radio("Horizonte del aviso", list(SN.HORIZONTES), horizontal=True)
        valido = emision_nc + timedelta(minutes=SN.HORIZONTES[hz])
        st.caption(f"Emisión {emision_nc:%d-%b %H:%M} · válido para las {valido:%H:%M} (hora de Perú) · "
                   "fuente: SENAMHI, nowcasting vía IDESEP · recolectado por Node-RED cada 5 min")
        geo_nc = datos_nc.get(hz, {"features": []})
        pts_zona = {z["zona"]: list(zip(p_ult[p_ult.zona == z["zona"]].lat, p_ult[p_ult.zona == z["zona"]].lon)) for z in L.ZONAS}
        filas_nc = SN.resumen_zonas(geo_nc, pts_zona)   # en el orden de las zonas: valles primero
        cols_nc = st.columns(len(filas_nc))
        for col, f in zip(cols_nc, filas_nc):
            fondo, texto = SN.COLORES.get(f["nivel_max"], "#FFFFFF"), "#0C141C"
            if f["nivel_max"] == 0:
                fondo, texto = PANEL, "#E6EDF3"
            col.markdown(f"<div style='background:{fondo};color:{texto};border:1px solid {LINEA};border-radius:10px;padding:10px'>"
                         f"<div style='font-size:12px'>{f['zona']}</div><div style='font-weight:700'>{f['aviso']}</div>"
                         f"<div style='font-size:12px'>{f['pct_area_con_aviso']:.0f}% del área</div></div>", unsafe_allow_html=True)
        if "error" in geo_nc:
            st.caption(f"Este horizonte no pudo descargarse ({geo_nc['error']}).")
    st.divider()

    var = st.radio("Variable del mapa", ["Próximos 7 días (mm)", "Últimos 7 días (mm)", "Probabilidad >5 mm (%)"], horizontal=True)
    campo = {"Próximos 7 días (mm)": "futuro", "Últimos 7 días (mm)": "pasado", "Probabilidad >5 mm (%)": "prob"}[var]
    cortes = [20, 50, 80] if campo == "prob" else [5, 15, 30]
    color_z = lambda v: ROJO if v >= cortes[2] else AMBAR if v >= cortes[1] else "#2F6E8F" if v >= cortes[0] else "#1F3B4D"

    mapa_col, panel = st.columns([1.5, 1])
    with mapa_col:
        m = folium.Map(location=[-8.7, -78.4], zoom_start=8, tiles=None, control_scale=True)
        capas_base(m, primero="relieve")
        g_z, g_p, g_c = folium.FeatureGroup("Zonas (coropleta)"), folium.FeatureGroup("Puntos del modelo"), folium.FeatureGroup("Celdas del satélite", show=False)
        for z in L.ZONAS:
            r = resumen_z.loc[z["zona"]]
            folium.Polygon([(a, b) for a, b in z["poligono"]], color="#FFFFFF", weight=1, fill=True, fill_color=color_z(r[campo]), fill_opacity=0.55,
                           tooltip=f"<b>{z['zona']}</b><br>Últimos 7 d: {r.pasado:.1f} mm<br>Próximos 7 d: {r.futuro:.1f} mm<br>Prob. &gt;5 mm: {r.prob:.0f}%").add_to(g_z)
        if geo_nc:
            g_nc = folium.FeatureGroup("Aviso SENAMHI (nowcasting)")
            for f in geo_nc.get("features", []):
                n = f["properties"].get("nivel") or 0
                if n >= 1:
                    folium.GeoJson(f, style_function=lambda _, c=SN.COLORES.get(n, "#FFFFFF"): {"fillColor": c, "color": c, "weight": 1, "fillOpacity": 0.55},
                                   tooltip=f"SENAMHI: {SN.NIVELES.get(n, n)} · pp {f['properties'].get('ppmin')}–{f['properties'].get('ppmax')}").add_to(g_nc)
            g_nc.add_to(m)
        celdas = set()
        for zona, gz in p_ult.groupby("zona"):
            for i, (_, p) in enumerate(gz.iterrows(), 1):
                folium.CircleMarker((p.lat, p.lon), radius=3, color="#FFFFFF", weight=1, fill=True, fill_opacity=0.9,
                                    tooltip=f"<b>{zona}</b> · punto {i}<br>Lat {p.lat:.4f} · Lon {p.lon:.4f}<br>Altitud {p.elevacion:.0f} m").add_to(g_p)
                celdas.add((round(np.floor(p.lat * 10) / 10 + 0.05, 2), round(np.floor(p.lon * 10) / 10 + 0.05, 2)))
        for la, lo in celdas:
            folium.Rectangle([(la - 0.05, lo - 0.05), (la + 0.05, lo + 0.05)], color=AZUL, weight=1, dash_array="4", fill=False,
                             tooltip=f"Celda IMERG (0.1°) centrada en {la:.2f}, {lo:.2f}").add_to(g_c)
        for g in (g_z, g_p, g_c):
            g.add_to(m)
        folium.LayerControl(collapsed=False).add_to(m)
        st_folium(m, height=620, use_container_width=True, returned_objects=[])
        st.caption(f"Puntos exactos usados por el modelo ({len(p_ult)}) · celdas satelitales derivadas de ellos ({len(celdas)}). "
                   "Mapas: CARTO, OpenTopoMap (CC BY-SA), Esri World Imagery.")

    with panel:
        zsel = st.selectbox("Zona", list(resumen_z.index), index=list(resumen_z.index).index("Cuenca alta Virú"))
        r = resumen_z.loc[zsel]
        a, b, c = st.columns(3)
        a.metric("Últimos 7 días", f"{r.pasado:.1f} mm")
        b.metric("Próximos 7 días", f"{r.futuro:.1f} mm")
        c.metric("Prob. >5 mm/día", f"{r.prob:.0f}%")
        fig = go.Figure()
        for x0, x1, colr in [(0, 15, "#1F3B4D"), (15, 30, "#25485E"), (30, 60, "#3A3A2A")]:
            fig.add_shape(type="rect", x0=x0, x1=x1, y0=-0.4, y1=0.4, fillcolor=colr, line_width=0, layer="below")
        fig.add_bar(x=[r.futuro], y=[""], orientation="h", width=0.25, marker_color="#E6EDF3", name="Próximos 7 días")
        fig.add_bar(x=[r.pasado], y=[""], orientation="h", width=0.08, marker_color=AZUL, name="Últimos 7 días")
        fig.add_vline(x=30, line_color=ROJO, line_width=3, annotation_text="30 mm (desbordes 2017)", annotation_font_color=ROJO)
        fig.update_layout(barmode="overlay", showlegend=True)
        fig.update_xaxes(range=[0, 60])
        st.plotly_chart(width="stretch", figure_or_data=estilo(fig, "Lluvia acumulada 7 días (mm)", "Zona", 200))

        if not sat.empty:
            s = sat[sat.zona == zsel].sort_values("fecha")
            fig = go.Figure()
            fig.add_bar(x=s.fecha, y=s.lluvia_media, name="Lluvia diaria (satélite)", marker_color=AZUL)
            fig.add_scatter(x=s.fecha, y=s.acum_7d, name="Acumulado 7 días", line=dict(color=AMBAR, width=3))
            fig.add_hline(y=30, line_color=ROJO, line_dash="dash", annotation_text="30 mm", annotation_font_color=ROJO)
            st.markdown("**¿Se está acumulando lluvia?** · satélite NASA IMERG")
            st.plotly_chart(width="stretch", figure_or_data=estilo(fig, "Fecha", "Lluvia (mm)", 280))

        fig = go.Figure()
        for zona, q in resumen_z.iterrows():
            on = zona == zsel
            fig.add_scatter(x=["Últimos 7 días", "Próximos 7 días"], y=[q.pasado, q.futuro], mode="lines+markers+text", name=zona,
                            text=["", zona.replace("Cuenca alta ", "C.A. ")], textposition="middle right",
                            line=dict(color="#FFFFFF" if on else (AMBAR if q.futuro >= 30 else "#4F7A96"), width=4 if on else 1.5))
        fig.add_hline(y=30, line_color=ROJO, line_dash="dash")
        fig.update_layout(showlegend=False)
        st.markdown("**Pendientes por zona**")
        st.plotly_chart(width="stretch", figure_or_data=estilo(fig, "Periodo", "Lluvia acumulada (mm)", 300))

    if not umbr.empty:
        with st.expander("Umbrales usados por zona (SENAMHI y modelo)"):
            st.dataframe(umbr[["zona", "p75", "p90", "p95", "p99", "fuente_umbral"]], hide_index=True, width="stretch")

# ================================================================== VISTA 3: OCÉANO
with v3:
    o1, o2 = st.columns(2)
    reciente = icen[icen.fecha >= icen.fecha.max() - pd.DateOffset(months=14)]
    with o1:
        fig = go.Figure()
        x0, x1 = reciente.fecha.min() - pd.Timedelta(days=15), reciente.fecha.max() + pd.Timedelta(days=15)
        bandas(fig, [(-1.5, 0.5, "#16222D", "Neutra"), (0.5, 1.3, "#1E2A2C", "Débil"), (1.3, 2.1, "#2A2E26", "Moderada"),
                     (2.1, 3.0, "#3A2C22", "Fuerte"), (3.0, 4.5, "#46241F", "Extraordinaria")], x0, x1)
        fig.add_scatter(x=reciente.fecha, y=reciente.anom_nino12, name="Anomalía mensual Niño 1+2", line=dict(color=AZUL, dash="dash", width=2))
        fig.add_scatter(x=reciente.fecha, y=reciente.icen, name="ICEN", mode="lines+markers+text", text=reciente.icen.round(2),
                        textposition="top center", line=dict(color=AMBAR, width=4))
        st.markdown("**Líneas con bandas de anomalía: Niño 1+2 e ICEN**")
        st.plotly_chart(width="stretch", figure_or_data=estilo(fig, "Mes", "Anomalía (°C)"))
    with o2:
        ult8 = icen.dropna(subset=["anom_nino12"]).tail(8)
        etiquetas = [f"{MESES[m - 1]}-{str(a)[2:]}" for a, m in zip(ult8.anio, ult8.mes)]
        fig = go.Figure()
        fig.add_bar(y=etiquetas, x=ult8.anom_nino12, orientation="h", name="Niño 1+2 (costa)",
                    marker_color=[ROJO if v >= 0 else "#3D7EA6" for v in ult8.anom_nino12], text=ult8.anom_nino12.round(2), textposition="outside")
        fig.add_bar(y=etiquetas, x=ult8.anom_nino34, orientation="h", name="Niño 3.4 (Pacífico central)",
                    marker_color=[AZUL if v >= 0 else "#2B5878" for v in ult8.anom_nino34])
        fig.add_vline(x=0, line_color=GRIS)
        fig.update_layout(barmode="group")
        st.markdown("**Barras divergentes: costa vs Pacífico central**")
        st.plotly_chart(width="stretch", figure_or_data=estilo(fig, "Anomalía de temperatura del mar (°C)", "Mes"))

    o3, o4, o5 = st.columns([1.1, 1, 1])
    puertos = {p["punto"]: p for p in T.PUNTOS}
    with o3:
        mm = folium.Map(location=[-7.8, -79.5], zoom_start=6, tiles=None, control_scale=True)
        capas_base(mm, primero="oscuro")
        for nombre, f in tsm_hoy.iterrows():
            p = puertos.get(nombre)
            col_ = ROJO if f.anom_1991_2020 >= 6.5 else "#F2845F" if f.anom_1991_2020 >= 6 else AMBAR
            folium.Rectangle([(f.lat_celda - 0.125, f.lon_celda - 0.125), (f.lat_celda + 0.125, f.lon_celda + 0.125)], color=col_, fill=True,
                             fill_opacity=0.4, tooltip=f"Celda satelital de {nombre}: {f.lat_celda:.3f}, {f.lon_celda:.3f}").add_to(mm)
            if p:
                folium.CircleMarker((p["lat"], p["lon"]), radius=f.anom_1991_2020 * 1.6, color="#FFFFFF", weight=1, fill=True, fill_color=col_, fill_opacity=0.9,
                                    tooltip=f"<b>{nombre}</b><br>Hoy {f.sst:.1f} °C · normal {f.sst_clima:.1f} °C<br>Anomalía +{f.anom_1991_2020:.2f} °C<br>Celda a {f.distancia_km} km").add_to(mm)
                folium.PolyLine([(p["lat"], p["lon"]), (f.lat_celda, f.lon_celda)], color=GRIS, weight=1).add_to(mm)
        folium.LayerControl().add_to(mm)
        st.markdown("**Mapa del mar: puerto y celda satelital**")
        st_folium(mm, height=480, use_container_width=True, returned_objects=[])
    orden = [p["punto"] for p in T.PUNTOS if p["punto"] in tsm_hoy.index]
    with o4:
        fig = go.Figure()
        for nombre in orden:
            f = tsm_hoy.loc[nombre]
            fig.add_scatter(x=[f.sst_clima, f.sst], y=[nombre, nombre], mode="lines", line=dict(color=AMBAR, width=4), showlegend=False)
        fig.add_scatter(x=tsm_hoy.loc[orden, "sst_clima"], y=orden, mode="markers", name="Normal 1991–2020", marker=dict(color=AZUL, size=12))
        fig.add_scatter(x=tsm_hoy.loc[orden, "sst"], y=orden, mode="markers+text", name="Hoy", marker=dict(color=ROJO, size=12),
                        text=[f"+{v:.1f}" for v in tsm_hoy.loc[orden, "anom_1991_2020"]], textposition="middle right")
        fig.update_yaxes(autorange="reversed")
        st.markdown("**Dumbbell: normal vs hoy**")
        st.plotly_chart(width="stretch", figure_or_data=estilo(fig, "Temperatura del mar (°C)", "Puerto (norte a sur)", 480))
    with o5:
        lat = [puertos[n]["lat"] for n in orden]
        fig = go.Figure()
        fig.add_scatter(x=lat, y=tsm_hoy.loc[orden, "anom_1991_2020"], mode="markers+text", text=orden, textposition="top center",
                        marker=dict(size=14, color=ROJO), showlegend=False)
        fig.update_xaxes(autorange="reversed")
        st.markdown("**Dispersión: anomalía vs latitud**")
        st.plotly_chart(width="stretch", figure_or_data=estilo(fig, "Latitud del puerto (°S), norte → sur", "Anomalía (°C)", 480))

    o6, o7 = st.columns(2)
    with o6:
        punto = st.selectbox("Serie diaria del puerto", orden, index=orden.index("Salaverry"))
        s = tsm[(tsm.punto == punto) & (tsm.fecha >= ult_dia - pd.Timedelta(days=365))].sort_values("fecha")
        fig = go.Figure()
        fig.add_scatter(x=s.fecha, y=s.sst_clima, name="Normal 1991–2020", line=dict(color=AZUL, width=2))
        fig.add_scatter(x=s.fecha, y=s.sst, name="Observada", line=dict(color=ROJO, width=2), fill="tonexty", fillcolor="rgba(240,107,79,0.25)")
        st.plotly_chart(width="stretch", figure_or_data=estilo(fig, "Fecha", "Temperatura del mar (°C)"))
    with o7:
        if not sem.empty:
            u = sem.sort_values("semana").tail(8)
            filas = {"Niño 1+2 · T": u.anom_nino12, "Niño 1+2 · R": u.get("rel_anom_nino12"), "Niño 3.4 · T": u.anom_nino34, "Niño 3.4 · R": u.get("rel_anom_nino34")}
            filas = {k: v for k, v in filas.items() if v is not None}
            z = [list(v) for v in filas.values()]
            fig = go.Figure(go.Heatmap(z=z, x=[str(d) for d in u.semana], y=list(filas), colorscale=[[0, "#6B2E22"], [0.5, ROJO], [1, "#F7C6B8"]],
                                       text=[[f"{x:+.1f}" for x in fila] for fila in z], texttemplate="%{text}", colorbar=dict(title="°C")))
            st.markdown("**Mapa de calor: regiones Niño por semana** (T: tradicional · R: relativa)")
            st.plotly_chart(width="stretch", figure_or_data=estilo(fig, "Semana (centrada en miércoles)", "Serie"))

# ================================================================== VISTA 4: EVENTOS HISTÓRICOS
with v4:
    if ev.empty:
        st.info("La tabla eventos_satelite aún no existe.")
    else:
        e = ev.copy()
        e["fecha"] = pd.to_datetime(e["fecha"])
        colores = {"Lluvia fuerte en valle y sierra": ROJO, "Lluvia local (valle)": AMBAR, "Lluvia en la sierra (posible crecida o huaico)": AZUL, "No concluyente": "#7C8B99"}
        fig = go.Figure()
        for ind, g in e.groupby("indicio_origen"):
            fig.add_scatter(x=g.acum_7d_cuenca, y=g.lluvia_max_dia, mode="markers", name=ind,
                            marker=dict(size=14, color=colores.get(ind, GRIS), symbol=["circle" if z == "Valle Virú" else "square" for z in g.zona],
                                        line=dict(color=FONDO, width=1)),
                            customdata=np.stack([g.fecha.dt.strftime("%d-%b-%Y"), g.zona, g.localidades], axis=-1),
                            hovertemplate="%{customdata[0]} · %{customdata[1]}<br>%{customdata[2]}<br>Sierra 7 d: %{x} mm · valle: %{y} mm<extra></extra>")
        fig.add_vline(x=30, line_color=AMBAR, line_dash="dash", annotation_text="30 mm sierra")
        fig.add_hline(y=6.02, line_color=ROJO, line_dash="dash", annotation_text="P99 SENAMHI 6.02 mm")
        st.markdown("**Emergencias INDECI: ¿dónde llovió antes del daño?** · satélite NASA IMERG, 7 días previos (círculo: Virú · cuadrado: Chao)")
        st.plotly_chart(width="stretch", figure_or_data=estilo(fig, "Lluvia acumulada 7 días en la cuenca alta (mm)", "Lluvia máxima diaria en el valle (mm)", 460))
        e["roni"] = [roni[(roni.anio == f.year) & (roni.mes_central == f.month)]["roni"].squeeze() if not roni[(roni.anio == f.year) & (roni.mes_central == f.month)].empty else None for f in e.fecha]
        st.dataframe(e[["fecha", "zona", "localidades", "lluvia_max_dia", "acum_3d_cuenca", "acum_7d_cuenca", "indicio_origen", "roni"]]
                     .rename(columns={"lluvia_max_dia": "valle máx. (mm)", "acum_3d_cuenca": "sierra 3 d (mm)", "acum_7d_cuenca": "sierra 7 d (mm)",
                                      "indicio_origen": "indicio", "roni": "RONI ese mes"}).sort_values("fecha"),
                     hide_index=True, width="stretch")

# ================================================================== VISTA 5: RESUMEN AUTOMÁTICO
with v5:
    st.caption("Resumen generado con reglas a partir de los datos del día (sin IA). Es la base sobre la que un modelo de lenguaje "
               "redactaría el resumen ejecutivo en una fase posterior.")
    vig = resumen_z[(resumen_z.index.str.startswith("Cuenca")) & (resumen_z.futuro >= 30)]
    r97 = roni[(roni.anio == 1997) & (roni.mes_central == ult_roni.mes_central)]
    v97 = None if r97.empty else float(r97['roni'].iloc[0])
    lineas = [
        f"**El Niño costero:** ICEN de {MESES[int(ult_icen.mes) - 1]} {ult_icen.icen:+.2f} ({ult_icen.categoria}); "
        f"anomalía mensual más reciente de Niño 1+2 {ult_mes.anom_nino12:+.2f} °C.",
        f"**Mar:** frente a Salaverry {tsm_hoy.loc['Salaverry', 'sst']:.1f} °C, {tsm_hoy.loc['Salaverry', 'anom_1991_2020']:+.1f} °C sobre lo normal; "
        f"rango costero de {tsm_hoy.anom_1991_2020.min():+.1f} a {tsm_hoy.anom_1991_2020.max():+.1f} °C.",
        f"**El Niño global:** RONI {ult_roni.temporada} {ult_roni.roni:+.2f}" + (f" (1997 en el mismo trimestre: {v97:+.2f})." if v97 is not None else "."),
        "**Lluvia:** " + ("; ".join(f"{z} con {q.futuro:.0f} mm previstos en 7 días (prob. {q.prob:.0f}%)" for z, q in vig.iterrows())
                          + " — sobre la referencia de 30 mm." if not vig.empty else "ninguna cuenca alta supera la referencia de 30 mm."),
    ]
    for l in lineas:
        st.markdown("- " + l)

st.divider()
st.caption("Fuentes: ENFEN/NOAA ERSSTv5 (ICEN) · NOAA CPC (regiones Niño, RONI) · NOAA OISST vía NCEI (mar) · Open-Meteo, CC BY 4.0 (pronóstico) · "
           "NASA GPM IMERG (satélite) · SENAMHI NT 001-2014 (umbrales) · INDECI vía CENEPRED 2017 (eventos)")
