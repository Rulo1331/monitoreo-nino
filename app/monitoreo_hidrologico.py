"""
Pestaña "Monitoreo Hidrológico" del dashboard El Niño — nivel y caudal de estaciones SENAMHI.

Origen: Node-RED (Raspberry) → BigQuery (tabla senamhi_hidrologia y sus vistas v_senamhi_hidrologia*).
Usa BQ_PROYECTO y BQ_DATASET del entorno (los coloca streamlit_app.py desde los secrets).

Uso en streamlit_app.py:
    with v4:
        render_monitoreo_hidrologico(cliente_fn=lambda: L.cliente_bigquery(os.getenv("BQ_PROYECTO")))

Lectura: consulta las vistas (rápido, filtra por fecha). Si la cuenta no puede ejecutar consultas
o las vistas no existen, lee la tabla directamente con list_rows (igual que el resto del dashboard)
y elimina duplicados en pandas.
"""
import os

import numpy as np
import pandas as pd
import plotly.graph_objects as go
import streamlit as st

# ------------------------------------------------------------------ estilo (mismo del dashboard)
AZUL, ROJO, AMBAR, GRIS, FONDO, PANEL, LINEA = "#7CC6E8", "#F06B4F", "#F2A541", "#9FB0C0", "#0C141C", "#131E28", "#22313F"
COLOR_UMBRAL = {"Amarillo": "#F2D35B", "Naranja": AMBAR, "Rojo": ROJO}
PALETA = [AZUL, AMBAR, "#B9E28C", "#C792EA", ROJO, "#5FD3BC", "#F7C6B8", GRIS]


def _estilo(fig, titulo_x, titulo_y, alto=360):
    fig.update_layout(height=alto, paper_bgcolor=PANEL, plot_bgcolor=FONDO, font=dict(color="#E6EDF3", size=12),
                      margin=dict(l=60, r=20, t=30, b=55), legend=dict(orientation="h", y=-0.25),
                      hoverlabel=dict(bgcolor=FONDO))
    fig.update_xaxes(title_text=titulo_x, gridcolor=LINEA, zeroline=False)
    fig.update_yaxes(title_text=titulo_y, gridcolor=LINEA, zeroline=False)
    return fig


# ------------------------------------------------------------------ configuración
TABLA = "senamhi_hidrologia"
VISTA, VISTA_ULTIMO = "v_senamhi_hidrologia", "v_senamhi_hidrologia_ultimo"
TZ = "America/Lima"
PERIODOS = {"Últimas 24 h": 1, "Últimos 3 días": 3, "Últimos 7 días": 7, "Últimos 30 días": 30, "Últimos 90 días": 90}
VARIABLES = {"CAUDAL": "Caudal", "NIVEL": "Nivel"}
ORDEN_ALERTA = {"ROJO": 0, "NARANJA": 1, "AMARILLO": 2, "NORMAL": 3, "SIN_UMBRAL": 4}
ETIQUETA_ALERTA = {"ROJO": "🔴 Rojo", "NARANJA": "🟠 Naranja", "AMARILLO": "🟡 Amarillo",
                   "NORMAL": "🟢 Normal", "SIN_UMBRAL": "⚪ Sin umbral"}
COLUMNAS = ["cod_estacion", "estacion", "rio", "departamento", "variable", "unidad", "fecha_hora", "valor",
            "umbral_amarillo", "umbral_naranja", "umbral_rojo"]


def _destino():
    return os.getenv("BQ_PROYECTO") or "appsheetgr", os.getenv("BQ_DATASET") or "BD_Procesamiento"


# ------------------------------------------------------------------ lectura
def _limpiar(df: pd.DataFrame) -> pd.DataFrame:
    df = df.reindex(columns=COLUMNAS + [c for c in df.columns if c not in COLUMNAS])
    df["fecha_hora"] = pd.to_datetime(df["fecha_hora"])
    for c in ["valor", "umbral_amarillo", "umbral_naranja", "umbral_rojo"]:
        df[c] = pd.to_numeric(df[c], errors="coerce")
    v, a, n, r = df["valor"], df["umbral_amarillo"], df["umbral_naranja"], df["umbral_rojo"]
    df["nivel_alerta"] = np.select(
        [a.isna() & n.isna() & r.isna(), r.notna() & (v >= r), n.notna() & (v >= n), a.notna() & (v >= a)],
        ["SIN_UMBRAL", "ROJO", "NARANJA", "AMARILLO"], "NORMAL")
    return df


@st.cache_data(ttl=600, show_spinner="Leyendo estaciones hidrológicas...")
def cargar(proyecto: str, dataset: str, dias: int, _cliente_fn=None):
    """Devuelve (ultimos, historial, modo, error). Nunca oculta el motivo de un fallo."""
    from google.cloud import bigquery
    try:
        cliente = _cliente_fn() if _cliente_fn else bigquery.Client(project=proyecto)
    except Exception as e:
        return pd.DataFrame(), pd.DataFrame(), None, f"no se pudo crear el cliente de BigQuery: {type(e).__name__}: {e}"

    # 1) Vistas con SQL (filtra por fecha en BigQuery)
    try:
        cols = ", ".join(COLUMNAS)
        ult = cliente.query(f"SELECT {cols} FROM `{proyecto}.{dataset}.{VISTA_ULTIMO}`").result()
        cfg = bigquery.QueryJobConfig(query_parameters=[bigquery.ScalarQueryParameter("dias", "INT64", dias)])
        hist = cliente.query(
            f"SELECT {cols} FROM `{proyecto}.{dataset}.{VISTA}` "
            f"WHERE fecha_hora >= DATETIME_SUB(CURRENT_DATETIME('{TZ}'), INTERVAL @dias DAY)", job_config=cfg).result()
        ult = _limpiar(pd.DataFrame([dict(f.items()) for f in ult]))
        hist = _limpiar(pd.DataFrame([dict(f.items()) for f in hist]))
        return ult, hist, "vistas", None
    except Exception as e_sql:
        motivo_sql = f"{type(e_sql).__name__}: {str(e_sql)[:160]}"

    # 2) Respaldo: tabla completa con list_rows (no requiere permiso para ejecutar consultas)
    try:
        tabla = cliente.get_table(f"{proyecto}.{dataset}.{TABLA}")
        campos = [c for c in tabla.schema if c.name in COLUMNAS + ["consultado_utc"]]
        df = pd.DataFrame([dict(f.items()) for f in cliente.list_rows(tabla, selected_fields=campos)])
        if df.empty:
            return df, df, "tabla", None
        df = (df.sort_values("consultado_utc")
                .drop_duplicates(["cod_estacion", "variable", "fecha_hora"], keep="last"))
        df = _limpiar(df).sort_values("fecha_hora")
        ult = df.groupby(["cod_estacion", "variable"]).tail(1)
        desde = pd.Timestamp.now(tz=TZ).tz_localize(None) - pd.Timedelta(days=dias)
        return ult, df[df["fecha_hora"] >= desde], f"tabla (las vistas no se pudieron usar: {motivo_sql})", None
    except Exception as e:
        return (pd.DataFrame(), pd.DataFrame(), None,
                f"vistas → {motivo_sql} · tabla {proyecto}.{dataset}.{TABLA} → {type(e).__name__}: {str(e)[:200]}")


# ------------------------------------------------------------------ utilidades
def _ahora():
    return pd.Timestamp.now(tz=TZ).tz_localize(None)


def _hace(h):
    if pd.isna(h):
        return "—"
    if h < 1:
        return f"{max(int(h * 60), 0)} min"
    if h < 48:
        return f"{int(h)} h {int(round((h % 1) * 60)):02d} min"
    return f"{h / 24:.1f} días"


def _etiqueta(f):
    return f"{f['estacion']} · {f['rio']} ({f['departamento']})"


def _tabla_estado(ult, hist):
    t = ult.copy()
    t["antiguedad_h"] = (_ahora() - t["fecha_hora"]).dt.total_seconds() / 3600
    # "Sin datos recientes" según el ritmo de cada estación: las automáticas reportan cada hora,
    # las convencionales 4 veces al día con un vacío nocturno de 12 h.
    huecos = (hist.sort_values("fecha_hora").groupby(["cod_estacion", "variable"])["fecha_hora"]
                  .apply(lambda s: s.diff().dt.total_seconds().div(3600).quantile(0.9)))
    t = t.merge(huecos.rename("hueco_h").reset_index(), on=["cod_estacion", "variable"], how="left")
    t["limite_h"] = (t["hueco_h"].fillna(2) * 1.5).clip(lower=3)
    t["al_dia"] = t["antiguedad_h"] <= t["limite_h"]
    t["pct_amarillo"] = (t["valor"] / t["umbral_amarillo"] * 100).where(t["umbral_amarillo"] > 0)
    t["orden"] = t["nivel_alerta"].map(ORDEN_ALERTA).fillna(9)
    return t.sort_values(["orden", "pct_amarillo", "estacion"], ascending=[True, False, True])


# ------------------------------------------------------------------ secciones
def _estado(t):
    alertas = t[t["nivel_alerta"].isin(["AMARILLO", "NARANJA", "ROJO"])]
    caidas = t[~t["al_dia"]]
    con_umbral = t[t["nivel_alerta"] != "SIN_UMBRAL"]

    # Tarjetas de resumen, con el mismo lenguaje visual que el aviso de lluvia
    peor = alertas.iloc[0] if len(alertas) else None
    mas_cerca = con_umbral.dropna(subset=["pct_amarillo"]).sort_values("pct_amarillo", ascending=False)
    tarjetas = [
        ("Estaciones en alerta", f"{len(alertas)} de {t['cod_estacion'].nunique()}",
         f"{peor['estacion']}: {ETIQUETA_ALERTA[peor['nivel_alerta']]}" if peor is not None else "ninguna supera umbral",
         COLOR_UMBRAL.get(str(peor["nivel_alerta"]).capitalize()) if peor is not None else None),
        ("Más cerca del umbral amarillo",
         f"{mas_cerca.iloc[0]['pct_amarillo']:.0f} %" if len(mas_cerca) else "—",
         f"{mas_cerca.iloc[0]['estacion']} ({mas_cerca.iloc[0]['rio'].title()})" if len(mas_cerca) else "sin umbrales publicados", None),
        ("Sin datos recientes", str(len(caidas)),
         ", ".join(caidas["estacion"].head(3)) if len(caidas) else "todas transmiten al día",
         AMBAR if len(caidas) else None),
    ]
    for col, (titulo, valor, detalle, color) in zip(st.columns(3), tarjetas):
        fondo, texto = (color, FONDO) if color else (PANEL, "#E6EDF3")
        col.markdown(f"<div style='background:{fondo};color:{texto};border:1px solid {LINEA};border-radius:10px;padding:10px'>"
                     f"<div style='font-size:12px'>{titulo}</div><div style='font-weight:700;font-size:20px'>{valor}</div>"
                     f"<div style='font-size:12px'>{detalle}</div></div>", unsafe_allow_html=True)

    if len(alertas):
        st.warning("Superan un umbral de alerta de SENAMHI: " + "; ".join(
            f"**{r.estacion}** ({r.rio.title()}) {VARIABLES.get(r.variable, r.variable).lower()} {r.valor:,.2f} {r.unidad} "
            f"→ {ETIQUETA_ALERTA[r.nivel_alerta]}" for r in alertas.itertuples()))

    st.dataframe(pd.DataFrame({
        "Alerta": t["nivel_alerta"].map(ETIQUETA_ALERTA),
        "Estación": t["estacion"], "Río": t["rio"], "Departamento": t["departamento"],
        "Variable": t["variable"].map(VARIABLES), "Último valor": t["valor"], "Unidad": t["unidad"],
        "% del umbral amarillo": t["pct_amarillo"].clip(upper=100),
        "Hora del dato": t["fecha_hora"], "Hace": t["antiguedad_h"].map(_hace),
        "Transmisión": t["al_dia"].map({True: "Al día", False: "⚠️ Sin datos recientes"}),
    }), hide_index=True, width="stretch", column_config={
        "Último valor": st.column_config.NumberColumn(format="%.2f"),
        "% del umbral amarillo": st.column_config.ProgressColumn(
            min_value=0, max_value=100, format="%.0f%%",
            help="Qué tan cerca está el valor actual del primer umbral de alerta (amarillo) de SENAMHI."),
        "Hora del dato": st.column_config.DatetimeColumn(format="DD/MM/YYYY HH:mm"),
    })


def _hidrograma(ult, hist, dias):
    est = (ult.assign(o=ult["nivel_alerta"].map(ORDEN_ALERTA).fillna(9),
                      p=(ult["valor"] / ult["umbral_amarillo"]).where(ult["umbral_amarillo"] > 0).fillna(-1))
              .sort_values(["o", "p"], ascending=[True, False]).drop_duplicates("cod_estacion"))
    etq = dict(zip(est["cod_estacion"], est.apply(_etiqueta, axis=1)))

    c1, c2 = st.columns([3, 1])
    cod = c1.selectbox("Estación", list(etq), format_func=etq.get, key="hidro_estacion",
                       help="Ordenadas de la más comprometida a la menos.")
    disp = [v for v in VARIABLES if v in set(ult.loc[ult["cod_estacion"] == cod, "variable"])]
    var = (c2.radio("Variable", disp, format_func=VARIABLES.get, horizontal=True, key=f"hidro_var_{cod}")
           if len(disp) > 1 else disp[0])

    s = hist[(hist["cod_estacion"] == cod) & (hist["variable"] == var)
             & (hist["fecha_hora"] >= _ahora() - pd.Timedelta(days=dias))].sort_values("fecha_hora")
    if s.empty:
        st.info("Esta estación no tiene datos en el período elegido. Prueba con un período más largo.")
        return None
    unidad, nombre = s["unidad"].dropna().iloc[-1], VARIABLES[var]
    ult_f = s.iloc[-1]
    prev = s[s["fecha_hora"] <= ult_f["fecha_hora"] - pd.Timedelta(hours=24)]
    imax, imin = s["valor"].idxmax(), s["valor"].idxmin()

    m1, m2, m3 = st.columns(3)
    m1.metric(f"{nombre} actual", f"{ult_f['valor']:,.2f} {unidad}",
              None if prev.empty else f"{ult_f['valor'] - prev.iloc[-1]['valor']:+,.2f} {unidad} en 24 h", delta_color="off")
    m1.caption(f"Dato de las {ult_f['fecha_hora']:%H:%M} del {ult_f['fecha_hora']:%d-%b} (hora de Perú)")
    m2.metric("Máximo del período", f"{s.loc[imax, 'valor']:,.2f} {unidad}", f"{s.loc[imax, 'fecha_hora']:%d-%b %H:%M}", delta_color="off")
    m3.metric("Mínimo del período", f"{s.loc[imin, 'valor']:,.2f} {unidad}", f"{s.loc[imin, 'fecha_hora']:%d-%b %H:%M}", delta_color="off")

    umbrales = {n: s[c].dropna().iloc[-1] for n, c in
                [("Amarillo", "umbral_amarillo"), ("Naranja", "umbral_naranja"), ("Rojo", "umbral_rojo")] if s[c].notna().any()}
    # Las líneas de umbral se muestran por defecto solo si el río está a ≥30 % del amarillo:
    # así la serie no queda aplastada contra el eje (como pasa en la web de SENAMHI).
    if umbrales:
        ref = umbrales.get("Amarillo", min(umbrales.values()))
        mostrar = st.toggle("Mostrar umbrales de alerta", value=s["valor"].max() >= 0.3 * ref, key=f"hidro_umb_{cod}_{var}")
        if not mostrar:
            st.caption("Umbrales SENAMHI: " + " · ".join(f"{n} {v:,.0f} {unidad}" for n, v in umbrales.items())
                       + f". El valor actual es el {ult_f['valor'] / ref * 100:.0f} % del amarillo.")
    else:
        mostrar = False
        st.caption("SENAMHI no publica umbrales de alerta para esta estación.")

    fig = go.Figure()
    fig.add_scatter(x=s["fecha_hora"], y=s["valor"], mode="lines+markers", name=nombre,
                    line=dict(color=AZUL, width=2.5), marker=dict(size=4),
                    hovertemplate="%{x|%d/%m/%Y %H:%M}<br>%{y:,.2f} " + unidad + "<extra></extra>")
    if mostrar:
        for n, v in umbrales.items():
            fig.add_hline(y=v, line_color=COLOR_UMBRAL[n], line_dash="dash", annotation_text=f"{n} {v:,.0f}",
                          annotation_font_color=COLOR_UMBRAL[n], annotation_position="top left")
    fig.update_layout(showlegend=False)
    st.markdown(f"**Hidrograma de {nombre.lower()}: {ult_f['estacion'].title()}, {ult_f['rio'].title()}**")
    st.plotly_chart(width="stretch", figure_or_data=_estilo(fig, "Fecha y hora (Perú)", f"{nombre} ({unidad})", 380))

    with st.expander("Resumen diario y descarga"):
        d = (s.assign(Día=s["fecha_hora"].dt.date).groupby("Día")["valor"]
              .agg(Promedio="mean", Mínimo="min", Máximo="max", Lecturas="count").reset_index()
              .sort_values("Día", ascending=False))
        st.dataframe(d, hide_index=True, width="stretch",
                     column_config={c: st.column_config.NumberColumn(format="%.2f") for c in ["Promedio", "Mínimo", "Máximo"]})
        st.download_button("Descargar datos (CSV)",
                           s[["cod_estacion", "estacion", "rio", "variable", "unidad", "fecha_hora", "valor"]]
                           .to_csv(index=False).encode("utf-8-sig"),
                           file_name=f"senamhi_{cod}_{var.lower()}.csv", mime="text/csv", key=f"hidro_csv_{cod}_{var}")
    return cod, var


def _comparacion(ult, hist, dias, cod_sel, var):
    cand = ult[ult["variable"] == var].drop_duplicates("cod_estacion")
    if len(cand) < 2:
        return
    etq = dict(zip(cand["cod_estacion"], cand.apply(_etiqueta, axis=1)))
    rio = ult.loc[ult["cod_estacion"] == cod_sel, "rio"].iloc[0]
    defecto = list(cand.loc[cand["rio"] == rio, "cod_estacion"])
    if len(defecto) < 2:
        defecto = list(etq)[:4]

    st.markdown(f"**Comparar estaciones · {VARIABLES[var].lower()}**")
    c1, c2 = st.columns([4, 1])
    elegidas = c1.multiselect("Estaciones a comparar", list(etq), default=defecto, format_func=etq.get, key=f"hidro_comp_{var}")
    log = c2.toggle("Escala logarítmica", key=f"hidro_log_{var}", help="Útil cuando los caudales son de órdenes muy distintos.")
    if not elegidas:
        st.caption("Elige al menos una estación.")
        return
    d = hist[hist["cod_estacion"].isin(elegidas) & (hist["variable"] == var)
             & (hist["fecha_hora"] >= _ahora() - pd.Timedelta(days=dias))]
    unidad = d["unidad"].dropna().iloc[0] if d["unidad"].notna().any() else ""
    fig = go.Figure()
    for i, c in enumerate(elegidas):
        g = d[d["cod_estacion"] == c].sort_values("fecha_hora")
        if log:
            g = g[g["valor"] > 0]
        fig.add_scatter(x=g["fecha_hora"], y=g["valor"], mode="lines", name=etq[c],
                        line=dict(color=PALETA[i % len(PALETA)], width=2),
                        hovertemplate="%{x|%d/%m %H:%M}<br>%{y:,.2f} " + unidad + "<extra>" + etq[c] + "</extra>")
    if log:
        fig.update_yaxes(type="log")
    st.plotly_chart(width="stretch", figure_or_data=_estilo(fig, "Fecha y hora (Perú)", f"{VARIABLES[var]} ({unidad})", 340))


# ------------------------------------------------------------------ pestaña
def render_monitoreo_hidrologico(cliente_fn=None):
    proyecto, dataset = _destino()
    st.markdown("**Nivel y caudal de los ríos de la costa norte** · estaciones SENAMHI, recolectadas por Node-RED cada hora")

    c1, c2, c3 = st.columns([1.3, 3, 0.8])
    periodo = c1.selectbox("Período", list(PERIODOS), index=2, key="hidro_periodo")
    dias = PERIODOS[periodo]
    if c3.button("Actualizar", key="hidro_actualizar", help="Vuelve a leer BigQuery sin esperar los 10 min de caché."):
        cargar.clear()

    ult, hist, modo, error = cargar(proyecto, dataset, max(dias, 7), _cliente_fn=cliente_fn)
    if error:
        st.error(f"No se pudieron leer los datos hidrológicos: {error}")
        st.caption("Revisa que el flujo de Node-RED esté cargando la tabla y que la cuenta de servicio de Streamlit "
                   f"pueda leer `{proyecto}.{dataset}`. Para usar las vistas necesita además el rol BigQuery Job User.")
        return
    if ult.empty:
        st.info(f"La tabla `{proyecto}.{dataset}.{TABLA}` aún no tiene datos. Revisa el flujo de Node-RED.")
        return

    deptos = sorted(ult["departamento"].dropna().unique())
    sel = c2.multiselect("Departamento", deptos, default=deptos, key="hidro_deptos")
    if not sel:
        st.info("Elige al menos un departamento.")
        return
    ult = ult[ult["departamento"].isin(sel)]
    hist = hist[hist["cod_estacion"].isin(ult["cod_estacion"])]

    _estado(_tabla_estado(ult, hist))
    st.divider()
    elegido = _hidrograma(ult, hist, dias)
    if elegido:
        st.divider()
        _comparacion(ult, hist, dias, *elegido)

    st.caption("Fuente: SENAMHI, monitoreo hidrológico en tiempo casi real, sin control de calidad y sujeto a revisión. "
               "Horas en hora de Perú." + ("" if modo == "vistas" else f" Lectura directa de la {modo}."))
