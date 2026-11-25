"""
Pestaña "Avisos precipitación" del dashboard El Niño — avisos meteorológicos de SENAMHI cargados a mano.

Origen: el zip (shapefile) que se descarga desde la página del aviso en SENAMHI. Cada zip es UN mapa
(un día) del aviso:  shp_aviso_<aviso>_<mapa>_<año>.zip  →  se suben a datos/avisos_senamhi/
El número de aviso, el mapa, las fechas y los niveles se leen del propio shapefile (no del nombre del archivo).
Opcional: datos/avisos_senamhi/avisos.csv con el título y la descripción de cada aviso, y
          datos/avisos_senamhi/rangos.csv con los acumulados de lluvia previstos por día y zona.

Uso en streamlit_app.py:
    with v_avi:
        render_avisos_precipitacion(capas_base=capas_base, zonas=L.ZONAS)

Peso: el zip original tiene ~87 mil vértices; aquí se simplifica a ~550 m (TOLERANCIA) y solo se dibujan
los niveles 2 a 4 del día elegido. Todo queda en caché hasta que cambien los archivos de la carpeta.
"""
import json
import re
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import pandas as pd
import streamlit as st

# ------------------------------------------------------------------ configuración
RAIZ = Path(__file__).resolve().parents[1]
CARPETA = RAIZ / "datos" / "avisos_senamhi"
TITULOS = CARPETA / "avisos.csv"
RANGOS = CARPETA / "rangos.csv"
LIMITES = RAIZ / "datos" / "limites"
LIMA = ZoneInfo("America/Lima")

TOLERANCIA = 0.005          # grados (~550 m): simplificación de los polígonos del aviso
MAX_BOTONES = 6             # avisos visibles como botones; el resto va en "Anteriores"

# Niveles del aviso meteorológico de SENAMHI (mismos colores que el nowcasting de la pestaña Lluvia)
NIVELES = {1: ("Blanco", "#F2F2F2"), 2: ("Amarillo", "#FFE600"), 3: ("Naranja", "#FF9900"), 4: ("Rojo", "#E60000")}
CUADRO = {0: "⬜", 1: "⬜", 2: "🟨", 3: "🟧", 4: "🟥"}
# Significado de cada nivel (resumen de la escala oficial de SENAMHI, gob.pe)
SIGNIFICADO = {
    1: "Sin fenómenos peligrosos. No se requieren precauciones especiales.",
    2: "Fenómenos peligrosos pero habituales en la zona. Mantenerse informado y ser prudente.",
    3: "Se prevén fenómenos peligrosos. Seguir las recomendaciones de las autoridades.",
    4: "Fenómenos de gran magnitud. Extremar precauciones y seguir las indicaciones de las autoridades.",
}
AZUL = "#7CC6E8"
# Regiones (departamentos) de la tabla de niveles, de norte a sur: nombre en el archivo de límites -> nombre a mostrar
REGIONES = {"TUMBES": "Tumbes", "PIURA": "Piura", "LAMBAYEQUE": "Lambayeque", "LA LIBERTAD": "La Libertad",
            "CAJAMARCA": "Cajamarca", "ANCASH": "Áncash", "LIMA": "Lima"}
AREA_MINIMA = 0.002         # grados² (~25 km²): una franja menor no cuenta (evita bordes que apenas se tocan)
CENTRO, ZOOM = [-8.2, -78.3], 6     # vista inicial: de Tumbes a Lima
DIAS = ["Lun", "Mar", "Mié", "Jue", "Vie", "Sáb", "Dom"]
MESES = ["ene", "feb", "mar", "abr", "may", "jun", "jul", "ago", "set", "oct", "nov", "dic"]

# estilo del dashboard
FONDO, PANEL, LINEA, TEXTO, GRIS = "#0C141C", "#131E28", "#22313F", "#E6EDF3", "#9FB0C0"


def _fecha(d):
    return f"{DIAS[d.weekday()]} {d.day} {MESES[d.month - 1]}"


def _nivel(texto):
    """'Nivel 3' -> 3"""
    m = re.search(r"\d", str(texto))
    return int(m.group()) if m else 0


def _texto_color(n):
    return "#FFFFFF" if n == 4 else FONDO


# ------------------------------------------------------------------ lectura (con caché)
def _firma():
    """Lista de zips con tamaño y fecha: si se sube o cambia uno, la caché se renueva."""
    if not CARPETA.exists():
        return ()
    return tuple((p.name, p.stat().st_size, p.stat().st_mtime_ns) for p in sorted(CARPETA.glob("*.zip")))


def _leer_zip(ruta, regiones):
    """Un zip = un mapa (día). Devuelve sus datos, el GeoJSON simplificado (niveles 2-4) y el nivel por región.
    regiones: {nombre: polígono shapely del departamento}"""
    import shapefile                       # pyshp: lee el zip directamente, sin GDAL
    import shapely
    from shapely.geometry import mapping, shape

    r = shapefile.Reader(str(ruta), encoding="latin-1")
    filas = []
    for sr in r.iterShapeRecords():
        a = sr.record.as_dict()
        filas.append((a, shape(sr.shape.__geo_interface__)))
    if not filas:
        raise ValueError("el shapefile no tiene polígonos")
    a0 = filas[0][0]

    features, por_region = [], {}
    for a, geom in filas:
        n = _nivel(a.get("nivel"))
        if n >= 2:      # el nivel 1 cubre el resto del país: no se dibuja (como en la web de SENAMHI)
            g = shapely.set_precision(geom.simplify(TOLERANCIA, preserve_topology=True), 0.0001)
            features.append({"type": "Feature", "geometry": mapping(g),
                             "properties": {"nivel": n, "texto": f"Nivel {n} · {NIVELES[n][0]}"}})
        for nombre, dep in regiones.items():
            area = geom.intersection(dep).area if geom.intersects(dep) else 0.0
            if area < AREA_MINIMA:
                continue
            nivel_r, area_aviso = por_region.get(nombre, (0, 0.0))
            por_region[nombre] = (max(nivel_r, n), area_aviso + (area if n >= 2 else 0.0))
    # nivel más alto en la región y % de su área con aviso (nivel 2 o más)
    por_region = {k: (n, min(100.0, 100 * ar / regiones[k].area)) for k, (n, ar) in por_region.items()}

    return {
        "aviso": int(a0["nro_aviso"]), "mapa": int(a0["nro_mapa"]), "anio": a0["fecha_emi"].year,
        "emision": a0["fecha_emi"], "inicio": min(a["fech_ini"] for a, _ in filas),
        "fin": max(a["fech_fin"] for a, _ in filas), "nivel_max": max(_nivel(a.get("nivel")) for a, _ in filas),
        "geojson": json.dumps({"type": "FeatureCollection", "features": features}, separators=(",", ":")),
        "regiones": por_region, "archivo": ruta.name,
    }


@st.cache_data(show_spinner="Leyendo los avisos de SENAMHI...")
def cargar(firma, regiones):
    """Agrupa los mapas por aviso. Devuelve (avisos del más reciente al más antiguo, archivos con error)."""
    from shapely.geometry import shape
    geo = json.loads(limites("departamentos", regiones) or '{"features": []}')
    poligonos = {f["properties"]["departamento"]: shape(f["geometry"]) for f in geo["features"]}
    mapas, errores = {}, []
    for nombre, _, _ in firma:
        try:
            m = _leer_zip(CARPETA / nombre, poligonos)
        except Exception as e:      # zip dañado o que no es un aviso: se informa y se sigue con los demás
            print(f"[avisos] no se pudo leer {nombre}: {type(e).__name__}: {e}")
            errores.append(nombre)
            continue
        mapas[(m["anio"], m["aviso"], m["mapa"])] = m      # si un mapa se subió dos veces, queda uno

    avisos = {}
    for (anio, aviso, _), m in sorted(mapas.items()):
        avisos.setdefault((anio, aviso), []).append(m)
    salida = []
    for (anio, aviso), ms in sorted(avisos.items(), reverse=True):
        salida.append({"anio": anio, "aviso": aviso, "mapas": ms, "emision": max(m["emision"] for m in ms),
                       "inicio": min(m["inicio"] for m in ms), "fin": max(m["fin"] for m in ms),
                       "nivel_max": max(m["nivel_max"] for m in ms)})
    return salida, errores


@st.cache_data(show_spinner=False)
def titulos(firma_csv):
    """avisos.csv (opcional): aviso, anio, titulo, descripcion."""
    if not firma_csv:
        return {}
    try:
        t = pd.read_csv(TITULOS, dtype=str, keep_default_na=False)
        return {(int(r.anio), int(r.aviso)): (r.get("titulo", ""), r.get("descripcion", "")) for _, r in t.iterrows()}
    except Exception as e:
        print(f"[avisos] avisos.csv no se pudo leer: {type(e).__name__}: {e}")
        return {}


@st.cache_data(show_spinner=False)
def limites(nivel, departamentos=None):
    """GeoJSON de límites (INEI) filtrado por departamento, para no enviar al navegador todo el país."""
    ruta = LIMITES / f"{nivel}.geojson"
    if not ruta.exists():
        return None
    d = json.loads(ruta.read_text(encoding="utf-8"))
    if departamentos is not None:
        d["features"] = [f for f in d["features"] if f["properties"].get("departamento") in departamentos]
    return json.dumps(d, separators=(",", ":"))


@st.cache_data(show_spinner=False)
def rangos(firma_csv):
    """rangos.csv (opcional): aviso, anio, fecha (AAAA-MM-DD), zona, min_mm, max_mm.
    Si min_mm va vacío se muestra como "≈ max_mm" (p. ej. "valores cercanos a 15 mm/día")."""
    if not firma_csv:
        return pd.DataFrame()
    try:
        r = pd.read_csv(RANGOS, dtype={"zona": str}, keep_default_na=True)
        r["fecha"] = pd.to_datetime(r["fecha"]).dt.date
        return r
    except Exception as e:
        print(f"[avisos] rangos.csv no se pudo leer: {type(e).__name__}: {e}")
        return pd.DataFrame()


# ------------------------------------------------------------------ piezas de la vista
def _cabecera(a, titulo, hoy):
    n = a["nivel_max"]
    nombre, color = NIVELES.get(n, ("—", GRIS))
    dias = (a["fin"] - a["inicio"]).days + 1
    if hoy < a["inicio"]:
        estado = f"Próximo · empieza en {(a['inicio'] - hoy).days} día(s)"
    elif hoy > a["fin"]:
        estado = "Finalizado"
    else:
        estado = f"Vigente · día {(hoy - a['inicio']).days + 1} de {dias}"
    titulo_html = f"<div style='font-size:20px;font-weight:700;color:{color if n >= 2 else TEXTO};margin-top:6px'>{titulo}</div>" if titulo else ""
    st.markdown(
        f"<div style='background:{PANEL};border:1px solid {LINEA};border-left:6px solid {color};border-radius:10px;padding:12px 16px'>"
        f"<div style='display:flex;justify-content:space-between;align-items:center;gap:12px;flex-wrap:wrap'>"
        f"<div style='font-size:22px;font-weight:700'>Aviso N° {a['aviso']}-{a['anio']}</div>"
        f"<div style='background:{color};color:{_texto_color(n)};font-weight:700;padding:4px 14px;border-radius:6px'>{nombre.upper()}</div></div>"
        f"{titulo_html}"
        f"<div style='display:flex;gap:28px;flex-wrap:wrap;margin-top:8px;font-size:13px;color:{GRIS}'>"
        f"<span>Inicio: <b style='color:{TEXTO}'>{_fecha(a['inicio'])} 00:00</b></span>"
        f"<span>Fin: <b style='color:{TEXTO}'>{_fecha(a['fin'])} 23:59</b></span>"
        f"<span>Vigencia: <b style='color:{TEXTO}'>{dias * 24 - 1} horas</b></span>"
        f"<span>Emisión: <b style='color:{TEXTO}'>{_fecha(a['emision'])}</b></span>"
        f"<span>Estado: <b style='color:{TEXTO}'>{estado}</b></span></div></div>",
        unsafe_allow_html=True)


def _tabla_regiones(a, dia_sel):
    """Nivel por región (filas) y día (columnas), con el % del área de la región con aviso."""
    th = f"padding:6px 8px;font-size:12px;color:{GRIS};text-align:center;border-bottom:1px solid {LINEA}"
    cab = "".join(f"<th style='{th}{';color:' + TEXTO if m['mapa'] == dia_sel else ''}'>{_fecha(m['inicio'])}</th>" for m in a["mapas"])
    filas = ""
    for clave, nombre in REGIONES.items():
        celdas = ""
        for m in a["mapas"]:
            n, pct = m["regiones"].get(clave, (0, 0.0))
            fondo = NIVELES[n][1] if n >= 2 else "transparent"
            txt = _texto_color(n) if n >= 2 else GRIS
            borde = f"outline:2px solid {TEXTO};" if m["mapa"] == dia_sel else ""
            detalle = f"<div style='font-size:10px;opacity:.85'>{pct:.0f}% del área</div>" if n >= 2 else ""
            celdas += (f"<td style='padding:4px'><div style='background:{fondo};color:{txt};{borde}border:1px solid {LINEA};"
                       f"border-radius:6px;text-align:center;font-size:12px;padding:3px 2px;line-height:1.25'>"
                       f"{'Nivel ' + str(n) if n else '—'}{detalle}</div></td>")
        filas += f"<tr><td style='padding:4px 8px;font-size:13px;white-space:nowrap'>{nombre}</td>{celdas}</tr>"
    st.markdown(f"<table style='width:100%;border-collapse:collapse'><tr><th style='{th};text-align:left'>Región</th>{cab}</tr>{filas}</table>",
                unsafe_allow_html=True)
    st.caption("Nivel más alto del aviso dentro de la región y porcentaje de su área con aviso (nivel 2 o más). "
               "Nivel 1 (blanco) = sin aviso.")


def _leyenda():
    """Qué significa cada nivel del aviso."""
    items = "".join(
        f"<div style='flex:1;min-width:160px;display:flex;gap:8px;align-items:flex-start'>"
        f"<div style='flex:none;width:14px;height:14px;margin-top:2px;border-radius:3px;background:{color};border:1px solid {LINEA}'></div>"
        f"<div style='font-size:12px;line-height:1.3'><b>Nivel {n} · {nombre}</b><br><span style='color:{GRIS}'>{SIGNIFICADO[n]}</span></div></div>"
        for n, (nombre, color) in NIVELES.items())
    st.markdown(f"<div style='display:flex;flex-wrap:wrap;gap:10px 18px;background:{PANEL};border:1px solid {LINEA};"
                f"border-radius:10px;padding:10px 12px;margin-top:4px'>{items}</div>", unsafe_allow_html=True)


def _lluvia_prevista(a, m_dia, tabla):
    """Acumulados de lluvia previstos (mm/día) por zona para el día elegido, cargados en rangos.csv."""
    st.markdown(f"**Lluvia prevista · {_fecha(m_dia['inicio'])}**")
    if tabla.empty:
        st.caption("Sin rangos cargados. Se agregan en `datos/avisos_senamhi/rangos.csv`.")
        return
    del_aviso = tabla[(tabla["aviso"] == a["aviso"]) & (tabla["anio"] == a["anio"])]
    del_dia = del_aviso[del_aviso["fecha"] == m_dia["inicio"]]
    if del_dia.empty:
        st.caption("Sin rangos cargados para este día (`datos/avisos_senamhi/rangos.csv`).")
        return
    escala = max(float(del_aviso["max_mm"].max()), 1.0)       # misma escala para todos los días del aviso
    filas = ""
    for _, r in del_dia.iterrows():
        mx = float(r["max_mm"])
        mn = None if pd.isna(r["min_mm"]) else float(r["min_mm"])
        texto = f"≈ {mx:g}" if mn is None else f"{mn:g} – {mx:g}"
        x0 = (mx if mn is None else mn) / escala * 100
        ancho = max(mx / escala * 100 - x0, 1.5)
        barra = (f"<div style='position:relative;height:8px;background:{LINEA};border-radius:4px'>"
                 f"<div style='position:absolute;left:{x0:.1f}%;width:{ancho:.1f}%;height:8px;background:{AZUL};border-radius:4px'></div></div>")
        filas += (f"<tr><td style='padding:4px 8px 4px 0;font-size:13px;white-space:nowrap'>{r['zona']}</td>"
                  f"<td style='padding:4px 8px;font-size:13px;white-space:nowrap;text-align:right'><b>{texto}</b></td>"
                  f"<td style='padding:4px 0;width:45%'>{barra}</td></tr>")
    st.markdown(f"<table style='width:100%;border-collapse:collapse'>{filas}</table>", unsafe_allow_html=True)
    st.caption(f"Acumulado de lluvia en mm/día según el aviso · barras en escala de 0 a {escala:g} mm.")


def _mapa(m_dia, zonas, capas_base):
    import folium
    from streamlit_folium import st_folium

    mapa = folium.Map(location=CENTRO, zoom_start=ZOOM, tiles=None, control_scale=True)
    if capas_base:
        capas_base(mapa, primero="oscuro")
    else:
        folium.TileLayer("OpenStreetMap").add_to(mapa)

    g_av = folium.FeatureGroup("Aviso SENAMHI")
    folium.GeoJson(m_dia["geojson"], style_function=lambda f: {
        "fillColor": NIVELES[f["properties"]["nivel"]][1], "color": NIVELES[f["properties"]["nivel"]][1],
        "weight": 1, "fillOpacity": 0.55}, tooltip=folium.GeoJsonTooltip(["texto"], labels=False)).add_to(g_av)
    g_av.add_to(mapa)

    g_z = folium.FeatureGroup("Viru y Chao")
    for nombre, poligono in zonas:
        folium.Polygon(list(poligono), color="#FFFFFF", weight=1.5, dash_array="5", fill=False, tooltip=nombre).add_to(g_z)
    g_z.add_to(mapa)

    # Departamentos en un panel propio, siempre por encima del aviso:
    # línea oscura con un borde claro alrededor, para que se distinga sobre cualquier mapa base.
    geo = limites("departamentos")
    if geo:
        folium.map.CustomPane("limite_dep", z_index=450, pointer_events=True).add_to(mapa)
        g_dep = folium.FeatureGroup("Departamentos")
        folium.GeoJson(geo, pane="limite_dep", interactive=False,
                       style_function=lambda _: {"fill": False, "color": "#E6EDF3", "weight": 4.5, "opacity": 0.55}).add_to(g_dep)
        folium.GeoJson(geo, pane="limite_dep",
                       style_function=lambda _: {"fill": False, "color": "#0B0F14", "weight": 2.2, "opacity": 1},
                       tooltip=folium.GeoJsonTooltip(["departamento"], labels=False)).add_to(g_dep)
        g_dep.add_to(mapa)

    folium.LayerControl(collapsed=False).add_to(mapa)
    st_folium(mapa, height=560, use_container_width=True, returned_objects=[])


# ------------------------------------------------------------------ pestaña
def render_avisos_precipitacion(capas_base=None, zonas=()):
    zonas = tuple((z["zona"], tuple(map(tuple, z["poligono"]))) for z in zonas)
    st.markdown("**Avisos meteorológicos de precipitación** · SENAMHI")

    avisos, errores = cargar(_firma(), tuple(REGIONES))
    if errores:
        st.caption(f"⚠️ No se pudieron leer {len(errores)} archivo(s): {', '.join(errores)}. Revisa que sean los zip de SENAMHI.")
    if not avisos:
        st.info("Aún no hay avisos cargados. Sube los zip descargados de SENAMHI a `datos/avisos_senamhi` "
                "(Add file → Upload files) y el dashboard los mostrará solos.")
        return
    hoy = datetime.now(LIMA).date()
    textos = titulos(TITULOS.stat().st_mtime_ns if TITULOS.exists() else None)

    # --- selector de aviso: los más recientes como botones, el resto en "Anteriores"
    etiqueta = {f"{CUADRO.get(a['nivel_max'], '')} N° {a['aviso']}": a for a in avisos}
    opciones = list(etiqueta)[:MAX_BOTONES] + (["Anteriores"] if len(avisos) > MAX_BOTONES else [])
    elegido = st.segmented_control("Aviso", opciones, default=opciones[0], key="avisos_sel", label_visibility="collapsed")
    if elegido == "Anteriores":
        previos = list(etiqueta)[MAX_BOTONES:]
        elegido = st.selectbox("Avisos anteriores", previos,
                               format_func=lambda k: f"{k} · {_fecha(etiqueta[k]['inicio'])} a {_fecha(etiqueta[k]['fin'])}",
                               key="avisos_previos")
    a = etiqueta.get(elegido) or avisos[0]     # sin selección: el más reciente

    titulo, descripcion = textos.get((a["anio"], a["aviso"]), ("", ""))
    _cabecera(a, titulo, hoy)
    if descripcion:
        with st.expander("Descripción del aviso"):
            st.write(descripcion)

    # --- día (mapa) del aviso: por defecto hoy si está dentro del aviso
    dia = {f"{CUADRO.get(m['nivel_max'], '')} {_fecha(m['inicio'])}": m for m in a["mapas"]}
    por_defecto = next((k for k, m in dia.items() if m["inicio"] <= hoy <= m["fin"]), list(dia)[0])
    k_dia = st.segmented_control("Día", list(dia), default=por_defecto, key=f"avisos_dia_{a['anio']}_{a['aviso']}")
    m_dia = dia.get(k_dia) or dia[por_defecto]

    col_mapa, col_panel = st.columns([1.7, 1])
    with col_panel:
        st.markdown("**Nivel por región**")
        _tabla_regiones(a, m_dia["mapa"])
        st.divider()
        _lluvia_prevista(a, m_dia, rangos(RANGOS.stat().st_mtime_ns if RANGOS.exists() else None))
        st.caption(f"Numero total de mapas cargados de este aviso: {len(a['mapas'])}" )
    with col_mapa:
        _mapa(m_dia, zonas, capas_base)
        _leyenda()
        st.caption("Mapa de Aviso según SENAMHI (simplificado a ~550 m) || Límites según INEI")
