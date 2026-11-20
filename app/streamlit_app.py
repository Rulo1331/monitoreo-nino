"""
Dashboard de monitoreo de El Niño Costero – Virú, Chao y costa norte del Perú.
Basado en el Modelo C v2 (sala de análisis). Lee las tablas que llenan los recolectores.

Ejecución local:   streamlit run app/streamlit_app.py
Credenciales:      .streamlit/secrets.toml (GCP_SA_KEY, BQ_PROYECTO, BQ_DATASET).
                   Sin credenciales lee la base local de prueba 'datos_nino.db'.
"""
import os
import re
import sys
from datetime import timedelta
from pathlib import Path

import numpy as np
import pandas as pd
import plotly.graph_objects as go
import streamlit as st

from monitoreo_hidrologico import render_monitoreo_hidrologico
st.set_page_config(page_title="Monitoreo El Niño – Virú y Chao", page_icon="🌊", layout="wide")

# ------------------------------------------------------------------ Configuración y datos
RAIZ = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(RAIZ / "recolectores"))

# --- Credenciales: se leen de st.secrets y se registran los problemas en lugar de ocultarlos
DIAG = {"claves_secrets": [], "error_secrets": None}
try:
    DIAG["claves_secrets"] = list(st.secrets.keys())
    for clave in ("GCP_SA_KEY", "BQ_PROYECTO", "BQ_DATASET", "BQ_PREFIJO", "NODERED_URL", "NODERED_TOKEN"):
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


# Prefijo de las tablas de este proyecto en BigQuery (para distinguirlas de otras aplicaciones).
# Se puede cambiar con BQ_PREFIJO en los secrets; el modo local (SQLite) usa los nombres sin prefijo.
PREFIJO = os.getenv("BQ_PREFIJO", "fen_7_")


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
        ruta = L.ruta_bq(proyecto, PREFIJO + nombre)[1]
        df = cliente.list_rows(ruta).to_dataframe(create_bqstorage_client=False)
        for c in ("fecha", "emitido"):
            if c in df.columns:
                df[c] = pd.to_datetime(df[c]).dt.date
        return df, None
    except NotFound:
        return pd.DataFrame(), f"no existe {L.ruta_bq(proyecto, PREFIJO + nombre)[1]}"
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


NOMBRES = ["icen", "roni", "nino_semanal", "tsm_costera", "lluvia_zonas", "lluvia_puntos", "lluvia_umbrales", "lluvia_satelite",
           "eventos_satelite", "calor_subsuperficial", "enfen_comunicados", "enfen_probabilidades"]
import hashlib
FIRMA = hashlib.sha256((os.getenv("GCP_SA_KEY") or "").encode()).hexdigest()[:12]
LEIDAS = {n: tabla(n, os.getenv("BQ_PROYECTO"), os.getenv("BQ_DATASET"), FIRMA) for n in NOMBRES}
icen, roni, sem = (LEIDAS[n][0] for n in ("icen", "roni", "nino_semanal"))
tsm, zonas_df, puntos = (LEIDAS[n][0] for n in ("tsm_costera", "lluvia_zonas", "lluvia_puntos"))
umbr, sat, ev = (LEIDAS[n][0] for n in ("lluvia_umbrales", "lluvia_satelite", "eventos_satelite"))
cal, enf, enf_p = (LEIDAS[n][0] for n in ("calor_subsuperficial", "enfen_comunicados", "enfen_probabilidades"))
esenciales = ["icen", "roni", "tsm_costera", "lluvia_zonas", "lluvia_puntos"]
if any(LEIDAS[n][0].empty for n in esenciales):
    diagnostico({n: LEIDAS[n][1] or "OK" for n in NOMBRES})
    st.stop()

# BigQuery no garantiza el orden de las filas (y los días rellenados del histórico se agregaron al final).
# Se ordena cada serie por fecha y se descartan días repetidos: así las líneas no "saltan" hacia atrás.
if not tsm.empty:
    tsm = tsm.sort_values(["punto", "fecha"]).drop_duplicates(["punto", "fecha"], keep="last").reset_index(drop=True)
icen = icen.sort_values(["anio", "mes"]).drop_duplicates(["anio", "mes"], keep="last").reset_index(drop=True)
if not sem.empty and "semana" in sem:
    sem["semana"] = pd.to_datetime(sem["semana"])
    sem = sem.sort_values("semana").drop_duplicates("semana", keep="last").reset_index(drop=True)
if not sat.empty and {"zona", "fecha"} <= set(sat.columns):
    sat = sat.sort_values(["zona", "fecha"]).reset_index(drop=True)
if not cal.empty:
    cal = cal.sort_values(["anio", "mes"]).drop_duplicates(["anio", "mes"], keep="last").reset_index(drop=True)
    cal["fecha"] = pd.to_datetime(dict(year=cal["anio"], month=cal["mes"], day=1))
if not enf.empty:
    enf = enf.sort_values(["anio", "numero"]).reset_index(drop=True)
    enf["fecha"] = pd.to_datetime(enf["fecha"])

# Preparación
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
ult_roni = roni.iloc[-1]
ult_dia = tsm["fecha"].max()
tsm_hoy = tsm[tsm["fecha"] == ult_dia].set_index("punto")
BANDAS_ICEN = [(-1.5, 0.5, "#16222D", "Neutra"), (0.5, 1.3, "#1E2A2C", "Débil"), (1.3, 2.1, "#2A2E26", "Moderada"),
               (2.1, 3.5, "#3A2C22", "Fuerte"), (3.5, 5.0, "#46241F", "Extraordinaria")]


def tendencia(serie, umbral=0.2):
    """Último valor frente al promedio de los 4 anteriores: 'sube', 'baja' o 'estable'."""
    s = serie.dropna()
    if len(s) < 5:
        return None, ""
    dif = s.iloc[-1] - s.iloc[-5:-1].mean()
    return dif, "sube" if dif > umbral else "baja" if dif < -umbral else "estable"


def anomalia_mensual(icen, sem):
    """Anomalía mensual de Niño 1+2 en tres versiones, sin tocar la tabla del ICEN:
    medida (NOAA ERSSTv5, llega hasta jul-2026), estimada a partir del ICEN oficial
    (siguiente = 3·ICEN − anterior − actual, como la vista v_anomalia_nino12_mensual, máx. 6 meses)
    y el promedio mensual del Niño 1+2 semanal (OISST) para contrastar."""
    base = icen.set_index(icen.anio * 12 + icen.mes - 1)
    est = {}
    medidos = base.index[base.anom_nino12.notna()]
    if len(medidos) >= 2 and pd.notna(base.anom_nino12.get(medidos[-1] - 1)):
        i = medidos[-1]
        a_ant, a_act = base.anom_nino12[i - 1], base.anom_nino12[i]
        for paso in range(1, 7):
            ic = base.icen.get(i)
            if ic is None or pd.isna(ic):
                break
            sig = 3 * ic - a_ant - a_act
            est[i + 1] = (round(float(sig), 2), paso)
            a_ant, a_act, i = a_act, sig, i + 1
    d = pd.DataFrame({"i": sorted(set(base.index) | set(est))})
    d["anio"], d["mes"] = d.i // 12, d.i % 12 + 1
    d["fecha"] = pd.to_datetime(dict(year=d.anio, month=d.mes, day=1))
    d["icen"] = d.i.map(base.icen)
    d["medida"] = d.i.map(base.anom_nino12)
    d["estimada"] = d.i.map(lambda k: est[k][0] if k in est else np.nan)
    d["meses_estimados"] = d.i.map(lambda k: est[k][1] if k in est else np.nan)
    d["semanal"] = np.nan
    if not sem.empty:
        g = sem.assign(i=sem.semana.dt.year * 12 + sem.semana.dt.month - 1).groupby("i")["anom_nino12"].mean().round(2)
        d["semanal"] = d.i.map(g)
    return d


anom_mes = anomalia_mensual(icen, sem)
dif_sem, tend_sem = tendencia(sem["anom_nino12"]) if not sem.empty else (None, "")
ult_sem = sem.iloc[-1] if not sem.empty else None
ult_enf = enf.iloc[-1] if not enf.empty else None

# ------------------------------------------------------------------ Encabezado
st.markdown(f"<span style='font-family:monospace;color:{AZUL};letter-spacing:.1em;font-size:12px'>SALA DE ANÁLISIS · EL NIÑO COSTERO</span>",
            unsafe_allow_html=True)
st.title("Océano, lluvia y territorio: Virú, Chao y costa norte")
st.caption(" · ".join(x for x in [
    f"ENFEN N° {ult_enf.comunicado} ({ult_enf.fecha:%d-%b})" if ult_enf is not None else "",
    f"ICEN {MESES[int(ult_icen.mes) - 1]}-{int(ult_icen.anio)}",
    f"Semanal al {ult_sem.semana:%d-%b}" if ult_sem is not None else "",
    f"Mar al {ult_dia:%d-%b-%Y}",
    f"Lluvia emitida {ult_em}"] if x))

# "Monitoreo hidrológico" va primero: st.tabs abre siempre la primera pestaña (vista inicial del dashboard)
v_hid, v_pan, v_oce, v_llu = st.tabs(["Monitoreo hidrológico", "Panorama", "Océano", "Lluvia"])

# ================================================================== PANORAMA (situación actual + comparación con años anteriores)
@st.cache_data(ttl=86400, show_spinner="Descargando el PDF...")
def pdf_bytes(url):
    """Descarga el PDF desde el repositorio público (enlace 'blob' de GitHub -> archivo directo)."""
    import requests
    directo = re.sub(r"^https://github\.com/([^/]+/[^/]+)/blob/", r"https://raw.githubusercontent.com/\1/", url)
    r = requests.get(directo, timeout=60)
    r.raise_for_status()
    return r.content


def enlace_publico(url):
    """Enlace directo al PDF, sin la página de GitHub: el CDN público jsDelivr sirve el archivo tal cual
    (https://cdn.jsdelivr.net/gh/<usuario>/<repo>@<rama>/<ruta>), y el navegador lo abre con su visor de PDF."""
    return re.sub(r"^https://github\.com/([^/]+/[^/]+)/blob/([^/]+)/", r"https://cdn.jsdelivr.net/gh/\1@\2/", url)


def ver_pdf(comunicado, url):
    """Abre el comunicado en una ventana emergente dentro del dashboard."""
    @st.dialog(f"Comunicado oficial ENFEN N° {comunicado}", width="large")
    def _ventana():
        try:
            st.pdf(pdf_bytes(url), height=720)
        except Exception as e:      # sin conexión a GitHub o sin el visor instalado: queda el enlace
            st.warning(f"No se pudo mostrar el PDF aquí: {str(e)[:160] or type(e).__name__}")
        st.link_button("Abrir en una pestaña nueva", enlace_publico(url))
    _ventana()


with v_pan:
    # --- 1. Postura oficial del ENFEN
    if ult_enf is None:
        st.info("Aún no hay comunicados del ENFEN cargados (tabla enfen_comunicados).")
    else:
        estado = ult_enf.estado_alerta or "Sin dato"
        col_est = ROJO if estado.startswith("Alerta") else AMBAR if estado.startswith("Vigilancia") else GRIS
        e1, e2 = st.columns([1.6, 1])
        with e1:
            titular = ult_enf.titular if isinstance(ult_enf.titular, str) and ult_enf.titular else ""
            titular = re.sub(r"\b(el|la) ni(ñ)(o|a)\b", lambda m: f"{m.group(1).capitalize()} Ni{m.group(2)}{m.group(3)}",
                             titular.capitalize(), flags=re.I)
            h12 = f" · {ult_enf.horizonte_nino12}" if isinstance(ult_enf.horizonte_nino12, str) else ""
            h34 = f" · {ult_enf.horizonte_nino34}" if isinstance(ult_enf.horizonte_nino34, str) else ""
            prox = (f"<div style='font-size:12px;color:{GRIS};margin-top:8px'>Próximo comunicado: "
                    f"{pd.to_datetime(ult_enf.proximo_comunicado):%d-%m-%Y}</div>") if pd.notna(ult_enf.proximo_comunicado) else ""
            st.markdown(
                f"<div style='background:{PANEL};color:#E6EDF3;border:1px solid {LINEA};border-radius:12px;padding:16px 18px'>"
                f"<div style='font-size:12px;color:{GRIS};letter-spacing:.06em'>POSTURA OFICIAL · COMUNICADO ENFEN N° {ult_enf.comunicado} · "
                f"{ult_enf.fecha:%d-%m-%Y}</div>"
                f"<div style='margin:8px 0'><span style='background:{col_est};color:{FONDO};font-weight:700;border-radius:999px;"
                f"padding:3px 12px'>{estado}</span></div>"
                + (f"<div style='font-size:18px;font-weight:700;margin:6px 0 10px'>{titular}</div>" if titular else "")
                + f"<div>🌊 <b>El Niño costero (Niño 1+2):</b> {ult_enf.magnitud_nino12 or '—'}{h12}</div>"
                + f"<div>🌐 <b>Pacífico central (Niño 3.4):</b> {ult_enf.magnitud_nino34 or '—'}{h34}</div>"
                + prox + "</div>",
                unsafe_allow_html=True)
            if isinstance(ult_enf.get("url"), str) and ult_enf.url:
                if st.button("Ver el PDF del comunicado", icon=":material/description:"):
                    ver_pdf(ult_enf.comunicado, ult_enf.url)
        with e2:
            st.markdown("**Probabilidades del ENFEN**")
            if enf_p.empty:
                st.caption("Sin probabilidades cargadas (datos/enfen_probabilidades.csv).")
            else:
                pp = enf_p.merge(enf[["comunicado", "anio", "numero"]], on="comunicado", how="left").sort_values(["anio", "numero"])
                ult_c = pp["comunicado"].iloc[-1]
                pr = pp[pp.comunicado == ult_c]
                for col, (_, f) in zip(st.columns(len(pr)), pr.iterrows()):
                    nota = f.get("nota") if isinstance(f.get("nota"), str) and f.nota else None
                    col.metric(f"{f.region} · {f.magnitud}", f"{f.probabilidad:.0f} %", help=nota)
                    col.caption(str(f.periodo))
                if ult_c != ult_enf.comunicado:
                    st.caption(f"Del comunicado N° {ult_c}: el N° {ult_enf.comunicado} aún no tiene probabilidades cargadas.")
    st.divider()

    # --- 2. Selector de comparación
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

    def valor_semanal(a):
        if sem.empty or ult_sem is None:
            return None, ""
        s = sem[sem.semana.dt.year == a]
        if s.empty:
            return None, "sin dato: la serie semanal empieza en 1990" if a < 1990 else "sin dato"
        if modo == "Pico del año":
            f = s.loc[s["anom_nino12"].idxmax()]
            return f["anom_nino12"], f"sem. {f['semana']:%d-%b}"
        d = ult_sem.semana.replace(year=a)
        cerca = s[(s.semana - d).abs() <= pd.Timedelta(days=4)]
        return (cerca["anom_nino12"].iloc[0], f"sem. {cerca['semana'].iloc[0]:%d-%b}") if not cerca.empty else (None, "")

    def valor_mar(a, punto="Salaverry"):
        s = tsm[(tsm.punto == punto) & (tsm.fecha.dt.year == a)]
        if s.empty:
            return None, "sin dato: la serie empieza en 1991" if a < 1991 else "sin dato"
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
            return None, "sin emergencia registrada ese año"
        return e["acum_7d_cuenca"].max(), "7 días previos a una emergencia INDECI"

    # --- 3. Indicadores: valor actual y comparación con el año elegido
    # Lluvia: observada por satélite (NASA IMERG) en la sierra de Virú; el pronóstico queda como dato secundario.
    s_vir = sat[sat.zona == "Cuenca alta Virú"].dropna(subset=["acum_7d"]).sort_values("fecha") if not sat.empty else sat
    lluvia_obs = float(s_vir["acum_7d"].iloc[-1]) if s_vir is not None and not s_vir.empty else None
    f_sat = pd.to_datetime(s_vir["fecha"].iloc[-1]) if lluvia_obs is not None else None
    pron = resumen_z.loc["Cuenca alta Virú"]

    indicadores = [
        ("El Niño costero · ICEN", ult_icen.icen, valor_icen, "°C",
         f"{ult_icen.categoria} · {MESES[int(ult_icen.mes) - 1]}-{int(ult_icen.anio)}"),
        ("Mar Niño 1+2 · esta semana", None if ult_sem is None else ult_sem.anom_nino12, valor_semanal, "°C",
         "" if ult_sem is None else f"Semana del {ult_sem.semana:%d-%b} · {tend_sem} vs 4 semanas"),
        ("Mar frente a Salaverry", tsm_hoy.loc["Salaverry", "anom_1991_2020"], valor_mar, "°C",
         f"Anomalía al {ult_dia:%d-%b} · {tsm_hoy.loc['Salaverry', 'sst']:.1f} °C"),
        ("El Niño global · RONI", ult_roni.roni, valor_roni, "°C",
         f"{ult_roni.temporada} {int(ult_roni.anio)} · {ult_roni.get('intensidad') or ''}"),
        ("Sierra de Virú · lluvia 7 días", lluvia_obs, valor_sierra, "mm",
         ("Satélite " + (f"al {f_sat:%d-%b}" if f_sat is not None else "sin dato")
          + f" · pronóstico próx. 7 días: {pron.futuro:.0f} mm (prob. {pron.prob:.0f}%)")),
    ]
    for col, (titulo, hoy, fn, unidad, nota) in zip(st.columns(5), indicadores):
        comp, cuando = fn(anio)
        hay_comp = comp is not None and not pd.isna(comp)
        if hoy is None or pd.isna(hoy):
            col.metric(titulo, "—")
        else:
            col.metric(titulo, f"{hoy:+.2f} {unidad}" if unidad == "°C" else f"{hoy:.1f} {unidad}",
                       f"{hoy - comp:+.2f} {unidad} vs {anio}" if hay_comp else None, delta_color="off")
        col.caption(nota)
        valor_c = (f"{comp:+.2f} {unidad}" if unidad == "°C" else f"{comp:.1f} {unidad}") if hay_comp else None
        col.caption(f"**{anio}:** " + (f"{valor_c} ({cuando})" if valor_c else cuando or "sin dato"))

    # --- 4. Años análogos
    g1, g2 = st.columns(2)
    with g1:
        fig = go.Figure()
        bandas(fig, BANDAS_ICEN, -0.5, 11.5)
        for a, col_, nombre in [(anio, AZUL, str(anio)), (int(ult_icen.anio), ROJO, str(int(ult_icen.anio)))]:
            s = icen[icen.anio == a]
            fig.add_scatter(x=[MESES[m - 1] for m in s.mes], y=s.icen, mode="lines+markers", name=f"ICEN {nombre}",
                            line=dict(color=col_, width=4 if col_ == ROJO else 3, dash="dash" if col_ == AZUL else "solid"))
        fig.update_xaxes(categoryorder="array", categoryarray=MESES)
        st.markdown("**Años análogos · El Niño costero (ICEN)**")
        st.plotly_chart(width="stretch", figure_or_data=estilo(fig, "Mes", "ICEN (°C)"))
    with g2:
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

    if anio >= 1991:
        fig = go.Figure()
        for a, col_ in [(anio, AZUL), (ult_dia.year, ROJO)]:
            s = tsm[(tsm.punto == "Salaverry") & (tsm.fecha.dt.year == a)]
            # Todas las series se llevan a un mismo año de referencia (2000, bisiesto) para comparar por mes y día;
            # la fecha real se muestra al pasar el cursor.
            x_ref = pd.to_datetime({"year": 2000, "month": s.fecha.dt.month, "day": s.fecha.dt.day})
            fig.add_scatter(x=x_ref, y=s.anom_1991_2020, mode="lines", name=str(a), customdata=s.fecha.dt.strftime("%d/%m/%Y"),
                            hovertemplate="%{customdata}<br>Anomalía: %{y:+.2f} °C<extra>" + str(a) + "</extra>",
                            line=dict(color=col_, width=2.5 if col_ == ROJO else 1.8))
        fig.add_hline(y=0, line_color=GRIS)
        fig.update_xaxes(tickvals=pd.date_range("2000-01-01", periods=12, freq="MS"), ticktext=MESES, range=["2000-01-01", "2000-12-31"])
        st.markdown(f"**Mar frente a Salaverry: {ult_dia.year} vs {anio}** (anomalía diaria)")
        st.plotly_chart(width="stretch", figure_or_data=estilo(fig, "Mes", "Anomalía de temperatura (°C)", 320))
    else:
        st.caption(f"La serie diaria del mar frente a Salaverry empieza en 1991: no hay comparación diaria con {anio}.")

    # --- 5. Historial de comunicados
    if not enf.empty:
        with st.expander(f"Historial de comunicados del ENFEN ({len(enf)})"):
            ESCALA = {"débil": 1, "moderada": 2, "fuerte": 3, "muy fuerte": 4, "extraordinaria": 4}

            def nivel(t):
                """Magnitud en número (1 débil … 4 extraordinaria / muy fuerte); un rango toma el promedio."""
                if not isinstance(t, str):
                    return np.nan
                t, v = t.lower(), []
                for k in ("muy fuerte", "extraordinaria", "moderada", "débil", "fuerte"):
                    if k in t:
                        v.append(ESCALA[k])
                        t = t.replace(k, "")
                return float(np.mean(v)) if v else np.nan

            fig = go.Figure()
            # Niño 3.4 punteado y debajo, para que no tape a Niño 1+2 cuando coinciden
            for col_m, nombre, color, ancho, trazo in [("magnitud_nino34", "Pacífico central (Niño 3.4)", AZUL, 2, "dash"),
                                                       ("magnitud_nino12", "El Niño costero (Niño 1+2)", ROJO, 4, "solid")]:
                fig.add_scatter(x=enf.fecha, y=enf[col_m].map(nivel), mode="lines+markers", name=nombre, line_shape="hv",
                                line=dict(color=color, width=ancho, dash=trazo), customdata=np.stack([enf.comunicado, enf[col_m].fillna("—")], axis=-1),
                                hovertemplate="N° %{customdata[0]} · %{x|%d-%b}<br>%{customdata[1]}<extra>" + nombre + "</extra>")
            fig.update_yaxes(tickvals=[1, 2, 3, 4], ticktext=["Débil", "Moderada", "Fuerte", "Extraord. / Muy fuerte"], range=[0.5, 4.5])
            st.markdown("**Magnitud esperada según cada comunicado**")
            st.plotly_chart(width="stretch", figure_or_data=estilo(fig, "Fecha del comunicado", "Magnitud", 300))
            vista = enf.sort_values(["anio", "numero"], ascending=False)[
                ["comunicado", "fecha", "estado_alerta", "magnitud_nino12", "horizonte_nino12", "magnitud_nino34", "horizonte_nino34"]].copy()
            for c_txt in ("estado_alerta", "magnitud_nino12", "horizonte_nino12", "magnitud_nino34", "horizonte_nino34"):
                vista[c_txt] = vista[c_txt].fillna("—")
            st.dataframe(vista, hide_index=True, width="stretch", column_config={
                "comunicado": "N°", "fecha": st.column_config.DateColumn("Fecha", format="DD-MM-YYYY"), "estado_alerta": "Estado",
                "magnitud_nino12": "Niño 1+2", "horizonte_nino12": "Horizonte 1+2", "magnitud_nino34": "Niño 3.4",
                "horizonte_nino34": "Horizonte 3.4"})
            con_pdf = enf[enf["url"].apply(lambda u: isinstance(u, str) and bool(u))].sort_values(["anio", "numero"], ascending=False)
            if not con_pdf.empty:
                h1, h2 = st.columns([1, 3], vertical_alignment="bottom")
                elegido = h1.selectbox("Comunicado", list(con_pdf.comunicado), key="pdf_historial")
                if h2.button("Ver el PDF", key="ver_pdf_historial", icon=":material/description:"):
                    ver_pdf(elegido, con_pdf.set_index("comunicado").loc[elegido, "url"])

# ================================================================== LLUVIA
with v_llu:
    import folium
    from streamlit_folium import st_folium

    p_ult = puntos[puntos["emitido"] == puntos["emitido"].max()].drop_duplicates(["zona", "lat", "lon"])
    st.subheader("Aviso de lluvia SENAMHI en tiempo real")
    emision_nc, datos_nc, error_nc = nowcasting(os.getenv("NODERED_URL"), os.getenv("NODERED_TOKEN"))
    geo_nc = None
    if emision_nc is None:
        # Mientras el aviso no llegue al dashboard se muestra un mensaje amable (el detalle técnico queda en el log).
        print(f"[nowcasting] no disponible: {error_nc}")
        st.info("🛠️ Estamos preparando este espacio: pronto verás aquí el aviso de lluvia de SENAMHI en tiempo real.")
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

# ================================================================== OCÉANO
with v_oce:
    st.subheader("El mar ahora")
    o1, o2 = st.columns(2)
    with o1:
        if sem.empty:
            st.info("Sin datos semanales (tabla nino_semanal).")
        else:
            PERIODOS = {"6 meses": 26, "1 año": 52, "3 años": 156, "Desde 1990": None}
            periodo = st.session_state.get("periodo_semanal") or "6 meses"
            semanas = PERIODOS[periodo]
            u = sem if semanas is None else sem[sem.semana >= sem.semana.max() - pd.Timedelta(weeks=semanas)]
            modo_l = "lines+markers" if len(u) <= 60 else "lines"
            fig = go.Figure()
            fig.add_scatter(x=u.semana, y=u.anom_nino12, name="Niño 1+2 (costa)", mode=modo_l, line=dict(color=ROJO, width=3 if len(u) <= 60 else 1.5))
            fig.add_scatter(x=u.semana, y=u.anom_nino34, name="Niño 3.4 (Pacífico central)", mode=modo_l, line=dict(color=AZUL, width=2 if len(u) <= 60 else 1.2))
            fig.add_hline(y=0, line_color=GRIS)
            for col_s, color in (("anom_nino12", ROJO), ("anom_nino34", AZUL)):
                fig.add_annotation(x=u.semana.iloc[-1], y=u[col_s].iloc[-1], text=f"{u[col_s].iloc[-1]:+.1f}", showarrow=False,
                                   xanchor="left", xshift=8, font=dict(color=color, size=13))
            st.markdown(f"**Anomalía semanal del mar · {periodo.lower() if semanas else 'desde 1990'}**")
            st.plotly_chart(width="stretch", figure_or_data=estilo(fig, "Semana", "Anomalía (°C)"))
            st.segmented_control("Periodo", list(PERIODOS), default="6 meses", key="periodo_semanal", label_visibility="collapsed")
            st.caption(f"Niño 1+2 {tend_sem} en la última semana"
                       + (f" ({dif_sem:+.2f} °C frente al promedio de las 4 semanas previas)." if dif_sem is not None else "."))
    with o2:
        rec = anom_mes[anom_mes.fecha >= anom_mes.fecha.max() - pd.DateOffset(months=15)]
        fig = go.Figure()
        x0, x1 = rec.fecha.min() - pd.Timedelta(days=15), rec.fecha.max() + pd.Timedelta(days=15)
        bandas(fig, BANDAS_ICEN, x0, x1)
        fig.add_scatter(x=rec.fecha, y=rec.icen, name="ICEN oficial (IGP)", mode="lines+markers+text", text=rec.icen.round(2),
                        textposition="top center", line=dict(color=AMBAR, width=4))
        fig.add_scatter(x=rec.fecha, y=rec.medida, name="Anomalía mensual medida", mode="lines+markers", line=dict(color=AZUL, width=2))
        unidas = rec[rec.estimada.notna() | rec.fecha.eq(rec.loc[rec.medida.notna(), "fecha"].max())]
        fig.add_scatter(x=unidas.fecha, y=unidas.estimada.fillna(unidas.medida), name="Anomalía mensual estimada desde el ICEN",
                        mode="lines+markers", line=dict(color=AZUL, width=2, dash="dash"), marker=dict(symbol="circle-open", size=9))
        fig.add_scatter(x=rec.fecha, y=rec.semanal, name="Promedio del Niño 1+2 semanal", mode="markers",
                        marker=dict(color="#E6EDF3", symbol="diamond", size=8))
        st.markdown("**ICEN y anomalía mensual de Niño 1+2**")
        st.plotly_chart(width="stretch", figure_or_data=estilo(fig, "Mes", "Anomalía (°C)"))
        if rec.estimada.notna().any():
            st.caption("La anomalía medida (NOAA ERSSTv5) llega hasta julio 2026. Los meses siguientes se estiman despejando la media "
                       "móvil del ICEN oficial; cada mes estimado acumula más error, por eso se contrasta con el promedio semanal.")

    st.subheader("Calor bajo la superficie del Pacífico ecuatorial")
    if cal.empty:
        st.info("Aún no hay datos de calor subsuperficial (tabla calor_subsuperficial).")
    else:
        c1, c2 = st.columns(2)
        with c1:
            u = cal[cal.fecha >= cal.fecha.max() - pd.DateOffset(months=36)]
            fig = go.Figure()
            for col_c, nombre, color, ancho in [("t300_180w_100w", "Centro-oriente (180°-100°O)", ROJO, 3),
                                                ("t300_160e_80w", "Centro y oriente (160°E-80°O)", AMBAR, 2),
                                                ("t300_130e_80w", "Todo el Pacífico ecuatorial (130°E-80°O)", AZUL, 2)]:
                fig.add_scatter(x=u.fecha, y=u[col_c], name=nombre, mode="lines", line=dict(color=color, width=ancho))
            fig.add_hline(y=0, line_color=GRIS)
            st.markdown("**Anomalía de temperatura de los primeros 300 m · últimos 3 años**")
            st.plotly_chart(width="stretch", figure_or_data=estilo(fig, "Mes", "Anomalía 0-300 m (°C)"))
        with c2:
            opciones = [a for a in (1982, 1997, 2015, 2017, 2023) if a in set(cal.anio)]
            anios = st.session_state.get("anios_calor", [a for a in (1997, 2015, 2023) if a in opciones])
            fig = go.Figure()
            act = int(cal.anio.max())
            for a in anios + [act]:
                s = cal[cal.anio == a]
                fig.add_scatter(x=[MESES[m - 1] for m in s.mes], y=s.t300_180w_100w, mode="lines+markers", name=str(a),
                                line=dict(width=4 if a == act else 2, color=ROJO if a == act else None, dash="solid" if a == act else "dash"))
            fig.update_xaxes(categoryorder="array", categoryarray=MESES)
            fig.add_hline(y=0, line_color=GRIS)
            st.markdown(f"**{act} frente a otros años · franja 180°-100°O**")
            st.plotly_chart(width="stretch", figure_or_data=estilo(fig, "Mes", "Anomalía 0-300 m (°C)"))
            st.multiselect("Comparar con", opciones, default=[a for a in (1997, 2015, 2023) if a in opciones], key="anios_calor")
        st.caption("Es la reserva de calor que las ondas Kelvin llevan hacia la costa: suele adelantarse 1 a 3 meses a lo que luego "
                   "aparece en la superficie. Fuente: NOAA CPC, análisis GODAS, climatología 1981-2010.")

    st.subheader("Costa peruana, de norte a sur")
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
                folium.CircleMarker((p["lat"], p["lon"]), radius=max(f.anom_1991_2020, 0.5) * 1.6, color="#FFFFFF", weight=1, fill=True, fill_color=col_, fill_opacity=0.9,
                                    tooltip=f"<b>{nombre}</b><br>Hoy {f.sst:.1f} °C · normal {f.sst_clima:.1f} °C<br>Anomalía {f.anom_1991_2020:+.2f} °C<br>Celda a {f.distancia_km} km").add_to(mm)
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
                        text=[f"{v:+.1f}" for v in tsm_hoy.loc[orden, "anom_1991_2020"]], textposition="middle right")
        fig.update_yaxes(autorange="reversed")
        st.markdown("**Normal frente a hoy**")
        st.plotly_chart(width="stretch", figure_or_data=estilo(fig, "Temperatura del mar (°C)", "Puerto (norte a sur)", 480))
    with o5:
        lat = [puertos[n]["lat"] for n in orden]
        fig = go.Figure()
        fig.add_scatter(x=lat, y=tsm_hoy.loc[orden, "anom_1991_2020"], mode="markers+text", text=orden, textposition="top center",
                        marker=dict(size=14, color=ROJO), showlegend=False)
        fig.update_xaxes(autorange="reversed")
        st.markdown("**Anomalía según la latitud**")
        st.plotly_chart(width="stretch", figure_or_data=estilo(fig, "Latitud del puerto (°S), norte → sur", "Anomalía (°C)", 480))

    o6, o7 = st.columns(2)
    with o6:
        punto = st.session_state.get("puerto_serie") or "Salaverry"
        st.markdown(f"**Temperatura del mar frente a {punto} · último año**")
        s = tsm[(tsm.punto == punto) & (tsm.fecha >= ult_dia - pd.Timedelta(days=365))].sort_values("fecha")
        fig = go.Figure()
        fig.add_scatter(x=s.fecha, y=s.sst_clima, name="Normal 1991–2020", line=dict(color=AZUL, width=2))
        fig.add_scatter(x=s.fecha, y=s.sst, name="Observada", line=dict(color=ROJO, width=2), fill="tonexty", fillcolor="rgba(240,107,79,0.25)")
        st.plotly_chart(width="stretch", figure_or_data=estilo(fig, "Fecha", "Temperatura del mar (°C)"))
        st.selectbox("Puerto", orden, index=orden.index("Salaverry"), key="puerto_serie")
    with o7:
        if not sem.empty:
            u = sem.tail(8)
            filas = {"Niño 1+2 · T": u.anom_nino12, "Niño 1+2 · R": u.get("rel_anom_nino12"), "Niño 3.4 · T": u.anom_nino34, "Niño 3.4 · R": u.get("rel_anom_nino34")}
            filas = {k: v for k, v in filas.items() if v is not None}
            z = [list(v) for v in filas.values()]
            fig = go.Figure(go.Heatmap(z=z, x=[f"{d:%d-%b}" for d in u.semana], y=list(filas), colorscale=[[0, "#6B2E22"], [0.5, ROJO], [1, "#F7C6B8"]],
                                       text=[[f"{x:+.1f}" for x in fila] for fila in z], texttemplate="%{text}", colorbar=dict(title="°C")))
            st.markdown("**Regiones Niño por semana** (T: tradicional · R: relativa)")
            st.plotly_chart(width="stretch", figure_or_data=estilo(fig, "Semana (centrada en miércoles)", "Serie"))

# ================================================================== MONITOREO HIDROLÓGICO
with v_hid:
    render_monitoreo_hidrologico(cliente_fn=lambda: L.cliente_bigquery(os.getenv("BQ_PROYECTO")))


# ================================================================== PENDIENTES (código guardado, no se muestra)
# Se decidirá al final si vuelven al dashboard. Para mostrarlos, llamar a la función dentro de una pestaña.
def lectura_del_dia():
    """Lectura del día generada con reglas (antes en Panorama)."""
    # --- Lectura del día (reglas, sin IA)
    st.markdown("**Lectura del día**")
    vig = resumen_z[(resumen_z.index.str.startswith("Cuenca")) & (resumen_z.futuro >= 30)]
    r97 = roni[(roni.anio == 1997) & (roni.mes_central == ult_roni.mes_central)]
    v97 = None if r97.empty else float(r97["roni"].iloc[0])
    lineas = []
    if ult_enf is not None:
        lineas.append(f"**Postura oficial:** comunicado ENFEN N° {ult_enf.comunicado} ({ult_enf.fecha:%d-%m-%Y}), {ult_enf.estado_alerta}. "
                      f"El Niño costero {str(ult_enf.magnitud_nino12 or '—').lower()}"
                      f"{' ' + ult_enf.horizonte_nino12 if isinstance(ult_enf.horizonte_nino12, str) else ''}; "
                      f"Pacífico central {str(ult_enf.magnitud_nino34 or '—').lower()}"
                      f"{' ' + ult_enf.horizonte_nino34 if isinstance(ult_enf.horizonte_nino34, str) else ''}.")
    lineas.append(f"**El Niño costero:** ICEN de {MESES[int(ult_icen.mes) - 1]} {ult_icen.icen:+.2f} ({ult_icen.categoria})"
                  + (f"; Niño 1+2 semanal {ult_sem.anom_nino12:+.2f} °C ({tend_sem} frente a las 4 semanas previas)." if ult_sem is not None else "."))
    if not cal.empty:
        uc = cal.dropna(subset=["t300_180w_100w"])
        d_cal = uc.t300_180w_100w.iloc[-1] - uc.t300_180w_100w.iloc[-2]
        lineas.append(f"**Calor bajo la superficie (0-300 m, 180°-100°O):** {uc.t300_180w_100w.iloc[-1]:+.2f} °C en "
                      f"{MESES[int(uc.mes.iloc[-1]) - 1]}-{int(uc.anio.iloc[-1])}, {d_cal:+.2f} °C respecto al mes anterior "
                      f"({'la reserva de calor sigue creciendo' if d_cal > 0.05 else 'la reserva de calor dejó de crecer' if d_cal < -0.05 else 'estable'}).")
    lineas += [
        f"**Mar:** frente a Salaverry {tsm_hoy.loc['Salaverry', 'sst']:.1f} °C, {tsm_hoy.loc['Salaverry', 'anom_1991_2020']:+.1f} °C sobre lo normal; "
        f"rango costero de {tsm_hoy.anom_1991_2020.min():+.1f} a {tsm_hoy.anom_1991_2020.max():+.1f} °C.",
        f"**El Niño global:** RONI {ult_roni.temporada} {ult_roni.roni:+.2f}" + (f" (1997 en el mismo trimestre: {v97:+.2f})." if v97 is not None else "."),
        "**Lluvia:** " + ("; ".join(f"{z} con {q.futuro:.0f} mm previstos en 7 días (prob. {q.prob:.0f}%)" for z, q in vig.iterrows())
                          + ", sobre la referencia de 30 mm." if not vig.empty else "ninguna cuenca alta supera la referencia de 30 mm."),
    ]
    for l in lineas:
        st.markdown("- " + l)
    st.caption("Lectura generada con reglas a partir de los datos del día (sin IA).")


def emergencias_indeci():
    """Emergencias INDECI en Virú y Chao con la lluvia satelital previa (antes en Eventos históricos)."""
    st.subheader("Emergencias INDECI en Virú y Chao")
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
        st.markdown("**¿Dónde llovió antes del daño?** · satélite NASA IMERG, 7 días previos (círculo: Virú · cuadrado: Chao)")
        st.plotly_chart(width="stretch", figure_or_data=estilo(fig, "Lluvia acumulada 7 días en la cuenca alta (mm)", "Lluvia máxima diaria en el valle (mm)", 460))
        e["roni"] = [roni[(roni.anio == f.year) & (roni.mes_central == f.month)]["roni"].squeeze() if not roni[(roni.anio == f.year) & (roni.mes_central == f.month)].empty else None for f in e.fecha]
        st.dataframe(e[["fecha", "zona", "localidades", "lluvia_max_dia", "acum_3d_cuenca", "acum_7d_cuenca", "indicio_origen", "roni"]]
                     .rename(columns={"lluvia_max_dia": "valle máx. (mm)", "acum_3d_cuenca": "sierra 3 d (mm)", "acum_7d_cuenca": "sierra 7 d (mm)",
                                      "indicio_origen": "indicio", "roni": "RONI ese mes"}).sort_values("fecha"),
                     hide_index=True, width="stretch")

st.divider()
st.caption("Fuentes: IGP/ENFEN (ICEN oficial) · ENFEN (comunicados oficiales) · NOAA CPC (regiones Niño, RONI; calor 0-300 m, GODAS) · "
           "NOAA ERSSTv5 (anomalía mensual Niño 1+2 hasta jul-2026) · NOAA OISST vía NCEI (mar) · Open-Meteo, CC BY 4.0 (pronóstico) · "
           "NASA GPM IMERG (satélite) · SENAMHI NT 001-2014 (umbrales) · SENAMHI (nowcasting, hidrología) · INDECI vía CENEPRED 2017 (eventos)")
