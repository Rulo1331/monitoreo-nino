"""
Pestaña "Monitoreo Hidrológico" — nivel y caudal de estaciones SENAMHI.

Origen de los datos: Node-RED (Raspberry) → BigQuery → vistas
    appsheetgr.BD_Procesamiento.v_senamhi_hidrologia
    appsheetgr.BD_Procesamiento.v_senamhi_hidrologia_ultimo

Uso en la app principal:

    from monitoreo_hidrologico import render_monitoreo_hidrologico

    tab_..., tab_hidro = st.tabs([..., "Monitoreo Hidrológico"])
    with tab_hidro:
        render_monitoreo_hidrologico()             # crea su propio cliente desde st.secrets
        # o bien: render_monitoreo_hidrologico(client=cliente_bq_existente)
"""

import pandas as pd
import altair as alt
import streamlit as st
from google.cloud import bigquery

# ---------------------------------------------------------------- configuración
PROYECTO = "appsheetgr"
DATASET = "BD_Procesamiento"
VISTA = f"`{PROYECTO}.{DATASET}.v_senamhi_hidrologia`"
VISTA_ULTIMO = f"`{PROYECTO}.{DATASET}.v_senamhi_hidrologia_ultimo`"
TZ = "America/Lima"
CACHE_SEG = 600          # el flujo carga cada hora: 10 min de caché es suficiente

PERIODOS = {"Últimas 24 h": 1, "Últimos 3 días": 3, "Últimos 7 días": 7,
            "Últimos 30 días": 30, "Últimos 90 días": 90}
VARIABLES = {"CAUDAL": "Caudal", "NIVEL": "Nivel"}

# Colores y orden de alerta según la escala de SENAMHI
ORDEN_ALERTA = {"ROJO": 0, "NARANJA": 1, "AMARILLO": 2, "NORMAL": 3, "SIN_UMBRAL": 4}
ETIQUETA_ALERTA = {"ROJO": "🔴 Rojo", "NARANJA": "🟠 Naranja", "AMARILLO": "🟡 Amarillo",
                   "NORMAL": "🟢 Normal", "SIN_UMBRAL": "⚪ Sin umbral"}
COLOR_UMBRAL = {"Amarillo": "#E9B824", "Naranja": "#EE7B1E", "Rojo": "#D62828"}
COLOR_SERIE = "#1D4E89"

COLUMNAS = ["cod_estacion", "estacion", "rio", "departamento", "variable", "unidad",
            "fecha_hora", "valor", "umbral_amarillo", "umbral_naranja", "umbral_rojo", "nivel_alerta"]


# ---------------------------------------------------------------- acceso a BigQuery
@st.cache_resource(show_spinner=False)
def _cliente_bq():
    """Crea el cliente con la cuenta de servicio guardada en st.secrets."""
    from google.oauth2 import service_account
    try:
        for clave in ("gcp_service_account", "gcp", "bigquery", "service_account"):
            if clave in st.secrets:
                info = dict(st.secrets[clave])
                cred = service_account.Credentials.from_service_account_info(info)
                return bigquery.Client(credentials=cred, project=info.get("project_id", PROYECTO))
    except FileNotFoundError:
        pass
    return bigquery.Client(project=PROYECTO)   # credenciales por defecto del entorno


def _consultar(client, sql, params=None) -> pd.DataFrame:
    cfg = bigquery.QueryJobConfig(query_parameters=params or [])
    filas = [dict(r.items()) for r in (client or _cliente_bq()).query(sql, job_config=cfg).result()]
    df = pd.DataFrame(filas, columns=COLUMNAS)
    df["fecha_hora"] = pd.to_datetime(df["fecha_hora"])
    for c in ["valor", "umbral_amarillo", "umbral_naranja", "umbral_rojo"]:
        df[c] = pd.to_numeric(df[c], errors="coerce")
    return df


@st.cache_data(ttl=CACHE_SEG, show_spinner="Consultando estaciones…")
def cargar_ultimos(_client=None) -> pd.DataFrame:
    return _consultar(_client, f"SELECT {', '.join(COLUMNAS)} FROM {VISTA_ULTIMO}")


@st.cache_data(ttl=CACHE_SEG, show_spinner="Consultando historial…")
def cargar_historial(dias: int, _client=None) -> pd.DataFrame:
    sql = f"""
        SELECT {', '.join(COLUMNAS)}
        FROM {VISTA}
        WHERE fecha_hora >= DATETIME_SUB(CURRENT_DATETIME('{TZ}'), INTERVAL @dias DAY)
        ORDER BY cod_estacion, variable, fecha_hora"""
    return _consultar(_client, sql, [bigquery.ScalarQueryParameter("dias", "INT64", dias)])


# ---------------------------------------------------------------- utilidades
def _ahora() -> pd.Timestamp:
    return pd.Timestamp.now(tz=TZ).tz_localize(None)


def _hace(horas: float) -> str:
    if pd.isna(horas):
        return "—"
    if horas < 1:
        return f"{max(int(horas * 60), 0)} min"
    if horas < 48:
        return f"{int(horas)} h {int(round((horas % 1) * 60)):02d} min"
    return f"{horas / 24:.1f} días".replace(".", ",")


def _etiqueta(fila) -> str:
    return f"{fila['estacion']} · {fila['rio']} ({fila['departamento']})"


def _num(v, dec=2) -> str:
    return "—" if pd.isna(v) else f"{v:,.{dec}f}".replace(",", "X").replace(".", ",").replace("X", ".")


def _tabla_estado(ult: pd.DataFrame, hist: pd.DataFrame) -> pd.DataFrame:
    """Último dato por estación y variable, con antigüedad y estado de transmisión."""
    t = ult.copy()
    t["antiguedad_h"] = (_ahora() - t["fecha_hora"]).dt.total_seconds() / 3600

    # Límite de "sin datos recientes" adaptado a cada estación: las automáticas reportan cada hora,
    # las convencionales 4 veces al día con un vacío nocturno de 12 h.
    huecos = (hist.sort_values("fecha_hora")
                  .groupby(["cod_estacion", "variable"])["fecha_hora"]
                  .apply(lambda s: s.diff().dt.total_seconds().div(3600).quantile(0.9)))
    t = t.merge(huecos.rename("hueco_tipico_h").reset_index(), on=["cod_estacion", "variable"], how="left")
    t["limite_h"] = (t["hueco_tipico_h"].fillna(2) * 1.5).clip(lower=3)
    t["transmision"] = (t["antiguedad_h"] <= t["limite_h"]).map({True: "Al día", False: "Sin datos recientes"})

    t["pct_amarillo"] = (t["valor"] / t["umbral_amarillo"] * 100).where(t["umbral_amarillo"] > 0)
    t["orden"] = t["nivel_alerta"].map(ORDEN_ALERTA).fillna(9)
    return t.sort_values(["orden", "pct_amarillo", "estacion"], ascending=[True, False, True])


# ---------------------------------------------------------------- secciones
def _seccion_estado(tabla: pd.DataFrame):
    alertas = tabla[tabla["nivel_alerta"].isin(["AMARILLO", "NARANJA", "ROJO"])]
    sin_umbral = (tabla["nivel_alerta"] == "SIN_UMBRAL").sum()
    caidas = tabla[tabla["transmision"] == "Sin datos recientes"]

    if len(alertas):
        lineas = [f"- **{r.estacion}** ({r.rio}): {VARIABLES.get(r.variable, r.variable).lower()} "
                  f"{_num(r.valor)} {r.unidad} → {ETIQUETA_ALERTA[r.nivel_alerta]}"
                  for r in alertas.itertuples()]
        fn = st.error if alertas["nivel_alerta"].isin(["NARANJA", "ROJO"]).any() else st.warning
        fn("Estaciones que superan un umbral de alerta:\n" + "\n".join(lineas))
    else:
        texto = "Ninguna estación supera los umbrales de alerta de SENAMHI."
        if sin_umbral:
            texto += f" {sin_umbral} serie(s) no tienen umbral publicado."
        st.success(texto)

    if len(caidas):
        st.caption("Sin datos recientes: " + ", ".join(
            f"{r.estacion} ({VARIABLES.get(r.variable, r.variable).lower()}, último hace {_hace(r.antiguedad_h)})"
            for r in caidas.itertuples()))

    vista = pd.DataFrame({
        "Alerta": tabla["nivel_alerta"].map(ETIQUETA_ALERTA),
        "Estación": tabla["estacion"],
        "Río": tabla["rio"],
        "Departamento": tabla["departamento"],
        "Variable": tabla["variable"].map(VARIABLES),
        "Último valor": tabla["valor"],
        "Unidad": tabla["unidad"],
        "% del umbral amarillo": tabla["pct_amarillo"].clip(upper=100),
        "Hora del dato": tabla["fecha_hora"],
        "Hace": tabla["antiguedad_h"].map(_hace),
        "Transmisión": tabla["transmision"],
    })
    st.dataframe(
        vista, hide_index=True,
        column_config={
            "Último valor": st.column_config.NumberColumn(format="%.2f"),
            "% del umbral amarillo": st.column_config.ProgressColumn(
                min_value=0, max_value=100, format="%.0f%%",
                help="Qué tan cerca está el valor actual del primer umbral de alerta (amarillo)."),
            "Hora del dato": st.column_config.DatetimeColumn(format="DD/MM/YYYY HH:mm"),
        },
    )


def _seccion_hidrograma(ult: pd.DataFrame, hist: pd.DataFrame, dias: int):
    # Primero las estaciones más comprometidas (mayor alerta y más cerca del umbral)
    estaciones = (ult.assign(orden=ult["nivel_alerta"].map(ORDEN_ALERTA).fillna(9),
                             pct=(ult["valor"] / ult["umbral_amarillo"]).where(ult["umbral_amarillo"] > 0).fillna(-1))
                     .sort_values(["orden", "pct"], ascending=[True, False])
                     .drop_duplicates("cod_estacion")
                     .assign(etq=lambda d: d.apply(_etiqueta, axis=1)))
    etiquetas = dict(zip(estaciones["cod_estacion"], estaciones["etq"]))

    c1, c2 = st.columns([3, 1])
    cod = c1.selectbox("Estación", list(etiquetas), format_func=etiquetas.get, key="hidro_estacion")
    vars_disp = [v for v in VARIABLES if v in set(ult.loc[ult["cod_estacion"] == cod, "variable"])]
    variable = c2.radio("Variable", vars_disp, format_func=VARIABLES.get, horizontal=True,
                        key=f"hidro_var_{cod}") if len(vars_disp) > 1 else vars_disp[0]

    desde = _ahora() - pd.Timedelta(days=dias)
    serie = hist[(hist["cod_estacion"] == cod) & (hist["variable"] == variable)
                 & (hist["fecha_hora"] >= desde)].sort_values("fecha_hora")
    if serie.empty:
        st.info("Esta estación no tiene datos en el período elegido. Prueba con un período más largo.")
        return
    unidad = serie["unidad"].dropna().iloc[-1] if serie["unidad"].notna().any() else ""
    nombre = VARIABLES[variable]

    # Métricas: último valor y cambio en 24 h
    ultimo = serie.iloc[-1]
    previo = serie[serie["fecha_hora"] <= ultimo["fecha_hora"] - pd.Timedelta(hours=24)]
    delta = None if previo.empty else ultimo["valor"] - previo.iloc[-1]["valor"]
    imax, imin = serie["valor"].idxmax(), serie["valor"].idxmin()
    m1, m2, m3 = st.columns(3)
    m1.metric(f"{nombre} actual ({unidad})", _num(ultimo["valor"]),
              None if delta is None else f"{_num(delta)} en 24 h", delta_color="off",
              help=f"Dato de las {ultimo['fecha_hora']:%H:%M} del {ultimo['fecha_hora']:%d/%m}")
    m2.metric(f"Máximo del período ({unidad})", _num(serie.loc[imax, "valor"]),
              f"{serie.loc[imax, 'fecha_hora']:%d/%m %H:%M}", delta_color="off")
    m3.metric(f"Mínimo del período ({unidad})", _num(serie.loc[imin, "valor"]),
              f"{serie.loc[imin, 'fecha_hora']:%d/%m %H:%M}", delta_color="off")

    # Umbrales: se muestran por defecto solo si el río está a ≥30 % del amarillo,
    # para que la serie no quede aplastada contra el eje (como pasa en la web de SENAMHI).
    umbrales = {n: serie[c].dropna().iloc[-1] for n, c in
                [("Amarillo", "umbral_amarillo"), ("Naranja", "umbral_naranja"), ("Rojo", "umbral_rojo")]
                if serie[c].notna().any()}
    cerca = bool(umbrales) and serie["valor"].max() >= 0.3 * umbrales.get("Amarillo", min(umbrales.values()))
    if umbrales:
        mostrar = st.toggle("Mostrar umbrales de alerta", value=cerca, key=f"hidro_umb_{cod}_{variable}")
        if not mostrar:
            st.caption("Umbrales SENAMHI: " + " · ".join(f"{n} {_num(v, 0)} {unidad}" for n, v in umbrales.items())
                       + f". El valor actual es el {ultimo['valor'] / umbrales.get('Amarillo', 1) * 100:.0f} % del amarillo.")
    else:
        mostrar = False
        st.caption("SENAMHI no publica umbrales de alerta para esta estación.")

    eje_x = alt.X("fecha_hora:T", title=None, axis=alt.Axis(format="%d/%m %H:%M", labelAngle=0, tickCount=8))
    base = alt.Chart(serie).encode(
        x=eje_x, y=alt.Y("valor:Q", title=f"{nombre} ({unidad})", scale=alt.Scale(zero=False)))
    capas = [
        base.mark_line(color=COLOR_SERIE, strokeWidth=2),
        base.mark_circle(size=30, color=COLOR_SERIE).encode(tooltip=[
            alt.Tooltip("fecha_hora:T", title="Hora", format="%d/%m/%Y %H:%M"),
            alt.Tooltip("valor:Q", title=f"{nombre} ({unidad})", format=",.2f")]),
    ]
    if mostrar:
        df_u = pd.DataFrame({"umbral": list(umbrales), "valor": list(umbrales.values())})
        capas.append(alt.Chart(df_u).mark_rule(strokeDash=[6, 4], strokeWidth=1.5).encode(
            y="valor:Q",
            color=alt.Color("umbral:N", title="Umbral",
                            scale=alt.Scale(domain=list(COLOR_UMBRAL), range=list(COLOR_UMBRAL.values())),
                            legend=alt.Legend(orient="bottom")),
            tooltip=[alt.Tooltip("umbral:N", title="Umbral"), alt.Tooltip("valor:Q", title=unidad, format=",.0f")]))
    st.altair_chart(alt.layer(*capas).properties(height=360))

    with st.expander("Resumen diario y descarga"):
        diario = (serie.assign(Día=serie["fecha_hora"].dt.date)
                       .groupby("Día")["valor"]
                       .agg(Promedio="mean", Mínimo="min", Máximo="max", Lecturas="count")
                       .reset_index().sort_values("Día", ascending=False))
        st.dataframe(diario, hide_index=True, column_config={
            c: st.column_config.NumberColumn(format="%.2f") for c in ["Promedio", "Mínimo", "Máximo"]})
        st.download_button(
            "Descargar datos (CSV)",
            serie[["cod_estacion", "estacion", "rio", "variable", "unidad", "fecha_hora", "valor"]]
                .to_csv(index=False).encode("utf-8-sig"),
            file_name=f"senamhi_{cod}_{variable.lower()}.csv", mime="text/csv", key=f"hidro_csv_{cod}_{variable}")

    return cod, variable


def _seccion_comparacion(ult: pd.DataFrame, hist: pd.DataFrame, dias: int, cod_sel: str, variable: str):
    candidatos = ult[ult["variable"] == variable].drop_duplicates("cod_estacion")
    if len(candidatos) < 2:
        return
    etiquetas = dict(zip(candidatos["cod_estacion"], candidatos.apply(_etiqueta, axis=1)))
    rio_sel = ult.loc[ult["cod_estacion"] == cod_sel, "rio"].iloc[0]
    defecto = list(candidatos.loc[candidatos["rio"] == rio_sel, "cod_estacion"])
    if len(defecto) < 2:
        defecto = list(etiquetas)[:4]

    c1, c2 = st.columns([4, 1])
    elegidas = c1.multiselect("Estaciones", list(etiquetas), default=defecto, format_func=etiquetas.get,
                              key=f"hidro_comp_{variable}")
    log = c2.toggle("Escala logarítmica", value=False, key=f"hidro_log_{variable}",
                    help="Útil cuando los caudales de las estaciones son muy distintos.")
    if not elegidas:
        st.caption("Elige al menos una estación para comparar.")
        return

    desde = _ahora() - pd.Timedelta(days=dias)
    datos = hist[hist["cod_estacion"].isin(elegidas) & (hist["variable"] == variable)
                 & (hist["fecha_hora"] >= desde)].copy()
    if log:
        datos = datos[datos["valor"] > 0]
    datos["Estación"] = datos["cod_estacion"].map(etiquetas)
    unidad = datos["unidad"].dropna().iloc[0] if datos["unidad"].notna().any() else ""
    graf = alt.Chart(datos).mark_line(strokeWidth=2).encode(
        x=alt.X("fecha_hora:T", title=None, axis=alt.Axis(format="%d/%m %H:%M", labelAngle=0, tickCount=8)),
        y=alt.Y("valor:Q", title=f"{VARIABLES[variable]} ({unidad})",
                scale=alt.Scale(type="log" if log else "linear", zero=False)),
        color=alt.Color("Estación:N", legend=alt.Legend(orient="bottom", columns=2, labelLimit=320)),
        tooltip=[alt.Tooltip("Estación:N"), alt.Tooltip("fecha_hora:T", title="Hora", format="%d/%m/%Y %H:%M"),
                 alt.Tooltip("valor:Q", title=unidad, format=",.2f")],
    ).properties(height=320)
    st.altair_chart(graf)


# ---------------------------------------------------------------- pestaña
def render_monitoreo_hidrologico(client=None):
    st.subheader("Monitoreo hidrológico")
    st.caption("Nivel y caudal de los ríos de la costa norte, según las estaciones de SENAMHI. "
               "Los datos se actualizan cada hora.")

    c1, c2, c3 = st.columns([2, 3, 1])
    periodo = c1.selectbox("Período", list(PERIODOS), index=2, key="hidro_periodo")
    dias = PERIODOS[periodo]
    if c3.button("Actualizar", key="hidro_actualizar", help="Vuelve a consultar BigQuery sin esperar la caché."):
        cargar_ultimos.clear()
        cargar_historial.clear()

    try:
        ult = cargar_ultimos(_client=client)
        hist = cargar_historial(max(dias, 7), _client=client)   # mínimo 7 días para medir la transmisión
    except Exception as e:
        st.error(f"No se pudo leer BigQuery: {e}\n\n"
                 f"Revisa que existan las vistas {VISTA} y {VISTA_ULTIMO}, y que la cuenta de servicio "
                 "de Streamlit tenga permiso de lectura sobre el dataset.")
        return
    if ult.empty:
        st.info("Todavía no hay datos de estaciones. Revisa que el flujo de Node-RED esté cargando "
                "la tabla senamhi_hidrologia.")
        return

    deptos = sorted(ult["departamento"].dropna().unique())
    sel = c2.multiselect("Departamento", deptos, default=deptos, key="hidro_deptos")
    if sel:
        ult = ult[ult["departamento"].isin(sel)]
        hist = hist[hist["cod_estacion"].isin(ult["cod_estacion"])]
    if ult.empty:
        st.info("Elige al menos un departamento.")
        return

    st.markdown("#### Estado actual")
    _seccion_estado(_tabla_estado(ult, hist))

    st.markdown("#### Hidrograma por estación")
    elegido = _seccion_hidrograma(ult, hist, dias)

    if elegido:
        st.markdown(f"#### Comparar estaciones · {VARIABLES[elegido[1]].lower()}")
        _seccion_comparacion(ult, hist, dias, *elegido)

    st.caption("Fuente: SENAMHI, monitoreo hidrológico. Información en tiempo casi real, sin control de calidad "
               "y sujeta a revisión. Las horas corresponden a hora de Perú.")
