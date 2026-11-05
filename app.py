"""Tablero de indicadores macroeconómicos de Argentina con agente de IA.

Ejecutar:  streamlit run app.py
"""
from __future__ import annotations

import os
from datetime import date

import pandas as pd
import plotly.graph_objects as go
import streamlit as st
from plotly.subplots import make_subplots

import agente
from datos import (CATALOGO, FECHA_INICIO_DEFECTO, DatosNoDisponibles, buscar_series,
                   cargar, catalogo_total, transformar)

# ----------------------------------------------------------------------------
# Paleta
# ----------------------------------------------------------------------------
NEGRO, GRIS_OSC, GRIS, GRIS_CLARO, BLANCO = "#0B0B10", "#1B1B22", "#2C2C36", "#9A9AA8", "#F5F5F7"
MAGENTA = "#9C1B86"          # morado magenta oscuro (acento de marca)
MAGENTA_CLARO = "#C23AAE"
LINEAS = ["#FF4D4D", "#4DA3FF", "#FFD23F", "#3DDC84", "#FF9F43", "#26D0CE", "#F78FB3", "#B8E986"]

st.set_page_config(page_title="Macro Argentina", page_icon="📊", layout="wide")

st.markdown(f"""
<style>
.stApp {{ background: {NEGRO}; }}
h1, h2, h3 {{ color: {BLANCO}; }}
h1 span.acento {{ color: {MAGENTA_CLARO}; }}
.kpi {{ background: {GRIS_OSC}; border: 1px solid {GRIS}; border-left: 4px solid {MAGENTA};
        border-radius: 8px; padding: 12px 16px; margin-bottom: 10px; }}
.kpi .t {{ color: {GRIS_CLARO}; font-size: 0.78rem; text-transform: uppercase; letter-spacing: .04em; }}
.kpi .v {{ color: {BLANCO}; font-size: 1.6rem; font-weight: 700; }}
.kpi .s {{ color: {GRIS_CLARO}; font-size: 0.75rem; }}
.kpi .pos {{ color: #3DDC84; }} .kpi .neg {{ color: #FF4D4D; }}
button[kind="primary"] {{ background: {MAGENTA}; border-color: {MAGENTA}; }}
</style>
""", unsafe_allow_html=True)

TRANS_OPC = {"Nivel (valor original)": "nivel",
             "Variación % vs. período anterior": "var_periodo",
             "Variación % interanual": "var_interanual"}


# ----------------------------------------------------------------------------
# Utilidades
# ----------------------------------------------------------------------------
def cargar_seguro(nombre: str, desde: str):
    try:
        return cargar(nombre, desde)
    except DatosNoDisponibles as e:
        st.warning(f"⚠️ {e}")
    except Exception as e:
        st.warning(f"⚠️ Error al cargar '{nombre}': {e}")
    return None, None


def estilo(fig: go.Figure, titulo: str = "", alto: int = 480) -> go.Figure:
    fig.update_layout(
        title=dict(text=titulo, font=dict(color=BLANCO, size=16)),
        paper_bgcolor=NEGRO, plot_bgcolor=NEGRO, height=alto,
        font=dict(color=BLANCO), hovermode="x unified",
        hoverlabel=dict(bgcolor=MAGENTA, font_color=BLANCO),
        legend=dict(orientation="h", y=-0.25, font=dict(color=BLANCO)),
        margin=dict(l=10, r=10, t=50, b=10),
    )
    fig.update_xaxes(gridcolor=GRIS, linecolor=GRIS_CLARO, zeroline=False,
                     rangeselector=dict(bgcolor=MAGENTA, activecolor=MAGENTA_CLARO, font=dict(color=BLANCO),
                                        buttons=[dict(count=1, label="1A", step="year", stepmode="backward"),
                                                 dict(count=5, label="5A", step="year", stepmode="backward"),
                                                 dict(count=10, label="10A", step="year", stepmode="backward"),
                                                 dict(step="all", label="Todo")]))
    fig.update_yaxes(gridcolor=GRIS, linecolor=GRIS_CLARO, zeroline=False)
    return fig


def kpi(titulo: str, valor: str, sub: str = "", signo: int = 0):
    clase = "pos" if signo > 0 else "neg" if signo < 0 else ""
    st.markdown(f'<div class="kpi"><div class="t">{titulo}</div><div class="v">{valor}</div>'
                f'<div class="s {clase}">{sub}</div></div>', unsafe_allow_html=True)


def fmt(x: float, dec: int = 2) -> str:
    return f"{x:,.{dec}f}".replace(",", "X").replace(".", ",").replace("X", ".")


def recortar(s: pd.Series, d0: date, d1: date) -> pd.Series:
    return s[(s.index >= pd.Timestamp(d0)) & (s.index <= pd.Timestamp(d1))]


# ----------------------------------------------------------------------------
# Barra lateral
# ----------------------------------------------------------------------------
st.markdown('<h1>📊 Macro <span class="acento">Argentina</span></h1>', unsafe_allow_html=True)
st.caption("Datos oficiales (INDEC, BCRA, Ministerio de Economía) obtenidos en vivo desde la API de Series de "
           "Tiempo de datos.gob.ar. Sin carga manual.")

with st.sidebar:
    st.header("Configuración")
    inicio = st.date_input("Histórico desde", value=date.fromisoformat(FECHA_INICIO_DEFECTO),
                           min_value=date(1990, 1, 1), max_value=date.today())
    DESDE = inicio.isoformat()
    st.divider()
    st.subheader("Agente de IA")
    try:
        api_key = st.secrets.get("GEMINI_API_KEY", "")
    except Exception:  # no existe secrets.toml: es normal en una instalación local
        api_key = ""
    api_key = api_key or os.environ.get("GEMINI_API_KEY", "")
    if not api_key:
        api_key = st.text_input("Clave de API de Google (Gemini, gratis)", type="password",
                                help="Se saca gratis en aistudio.google.com → Get API key. También podés definir GEMINI_API_KEY como variable de entorno.")
    if st.button("🔄 Actualizar datos"):
        st.cache_data.clear()
        st.rerun()

NOMBRES = list(catalogo_total().keys())

tab_pan, tab_evo, tab_cmp, tab_ia, tab_exp = st.tabs(
    ["Panorama", "Evolución", "Comparar indicadores", "🤖 Agente IA", "Explorador de series"])

# ----------------------------------------------------------------------------
# Panorama
# ----------------------------------------------------------------------------
with tab_pan:
    st.subheader("Últimos datos disponibles")
    specs = [
        ("Inflación - IPC nivel general (índice)", "var_periodo", "Inflación mensual", "%"),
        ("Inflación - IPC nivel general (índice)", "var_interanual", "Inflación interanual", "%"),
        ("Tipo de cambio oficial USD (Com. A 3500)", "nivel", "Dólar oficial", "$"),
        ("Desempleo - Tasa de desocupación", "nivel", "Desocupación", "%"),
        ("Actividad - EMAE (desestacionalizado)", "var_interanual", "EMAE interanual", "%"),
        ("Reservas internacionales del BCRA", "nivel", "Reservas BCRA", "USD M"),
        ("Comercio - Exportaciones (USD)", "var_interanual", "Exportaciones interanual", "%"),
        ("Comercio - Importaciones (USD)", "var_interanual", "Importaciones interanual", "%"),
    ]
    barra = st.progress(0.0, text="Consultando APIs oficiales…")
    cols = st.columns(4)
    for i, (nom, tr, etiqueta, u) in enumerate(specs):
        barra.progress((i + 1) / len(specs), text=f"Consultando: {etiqueta}")
        try:
            df, info = cargar(nom, DESDE)
            s = transformar(df["valor"], tr).dropna()
            ultimo, fecha = float(s.iloc[-1]), s.index[-1]
            prev = float(s.iloc[-2]) if len(s) > 1 else None
            texto = f"{fmt(ultimo)}{'%' if u == '%' else ''}" if u in ("%",) else f"{u} {fmt(ultimo)}"
            sub = f"{fecha:%b %Y}" if info["frecuencia"] != "diaria" else f"{fecha:%d/%m/%Y}"
            signo = 0
            if prev is not None and u != "%":
                signo = 1 if ultimo > prev else -1 if ultimo < prev else 0
            with cols[i % 4]:
                kpi(etiqueta, texto, sub, signo)
        except Exception:
            with cols[i % 4]:
                kpi(etiqueta, "s/d", "no disponible")
    barra.empty()

# ----------------------------------------------------------------------------
# Evolución de un indicador
# ----------------------------------------------------------------------------
with tab_evo:
    c1, c2 = st.columns([2, 1])
    nombre = c1.selectbox("Indicador", NOMBRES, key="evo_ind")
    trans_lbl = c2.radio("Mostrar como", list(TRANS_OPC), key="evo_tr")
    df, info = cargar_seguro(nombre, DESDE)
    if df is not None:
        s_full = transformar(df["valor"], TRANS_OPC[trans_lbl]).dropna()
        rango = st.slider("Rango de fechas", min_value=s_full.index.min().date(), max_value=s_full.index.max().date(),
                          value=(s_full.index.min().date(), s_full.index.max().date()), key="evo_rng")
        s = recortar(s_full, *rango)
        fig = go.Figure(go.Scatter(x=s.index, y=s.values, mode="lines", line=dict(color=LINEAS[0], width=2.2),
                                   name=nombre, hovertemplate="%{y:,.2f}"))
        if not s.empty:
            fig.add_trace(go.Scatter(x=[s.idxmax()], y=[s.max()], mode="markers", name="Máximo",
                                     marker=dict(color=LINEAS[2], size=10, symbol="triangle-up")))
            fig.add_trace(go.Scatter(x=[s.idxmin()], y=[s.min()], mode="markers", name="Mínimo",
                                     marker=dict(color=LINEAS[1], size=10, symbol="triangle-down")))
        st.plotly_chart(estilo(fig, f"{nombre} — {trans_lbl}"), use_container_width=True)
        st.caption(f"Unidad: {info.get('unidad') or 's/d'} · Frecuencia: {info['frecuencia']} · "
                   f"Serie: {info['id']} ({info.get('via', 'id')}) · {info['titulo']}")
        if not s.empty:
            a, b, c, d = st.columns(4)
            with a: kpi("Último valor", fmt(s.iloc[-1]), f"{s.index[-1]:%d/%m/%Y}")
            with b: kpi("Máximo", fmt(s.max()), f"{s.idxmax():%d/%m/%Y}")
            with c: kpi("Mínimo", fmt(s.min()), f"{s.idxmin():%d/%m/%Y}")
            with d: kpi("Promedio", fmt(s.mean()), f"{len(s)} observaciones")
        with st.expander("Ver tabla y descargar"):
            st.dataframe(s.rename("valor").sort_index(ascending=False), use_container_width=True)
            st.download_button("Descargar CSV", s.rename("valor").to_csv().encode(), f"{info['id']}.csv", "text/csv")

# ----------------------------------------------------------------------------
# Comparar indicadores
# ----------------------------------------------------------------------------
with tab_cmp:
    elegidos = st.multiselect("Indicadores a comparar", NOMBRES, key="cmp_sel",
                              default=[n for n in NOMBRES[:3] if n in NOMBRES][:2])
    modo = st.radio("Escala", ["Base 100 (compara evolución)", "Valores originales (eje doble si son 2)",
                               "Variación % interanual"], horizontal=True, key="cmp_modo")
    series: dict[str, pd.Series] = {}
    for n in elegidos:
        d, inf = cargar_seguro(n, DESDE)
        if d is not None:
            tr = "var_interanual" if modo.startswith("Variación") else "nivel"
            series[n] = transformar(d["valor"], tr).dropna()

    if series:
        minimo = min(x.index.min() for x in series.values()).date()
        maximo = max(x.index.max() for x in series.values()).date()
        rango = st.slider("Rango de fechas", min_value=minimo, max_value=maximo, value=(minimo, maximo), key="cmp_rng")
        recort = {k: recortar(v, *rango) for k, v in series.items()}
        recort = {k: v for k, v in recort.items() if not v.empty}

        if recort:
            if modo.startswith("Base 100"):
                base = max(v.index.min() for v in recort.values())
                recort = {k: v[v.index >= base] / v[v.index >= base].iloc[0] * 100 for k, v in recort.items()}
                st.caption(f"Base 100 = {base:%d/%m/%Y} (primera fecha en común).")

            dual = modo.startswith("Valores") and len(recort) == 2
            fig = make_subplots(specs=[[{"secondary_y": True}]]) if dual else go.Figure()
            for i, (k, v) in enumerate(recort.items()):
                tr = go.Scatter(x=v.index, y=v.values, mode="lines", name=k, connectgaps=True,
                                line=dict(color=LINEAS[i % len(LINEAS)], width=2.2), hovertemplate="%{y:,.2f}")
                fig.add_trace(tr, secondary_y=(i == 1)) if dual else fig.add_trace(tr)
            if dual:
                ks = list(recort)
                fig.update_yaxes(title_text=ks[0], secondary_y=False, title_font=dict(color=LINEAS[0]))
                fig.update_yaxes(title_text=ks[1], secondary_y=True, title_font=dict(color=LINEAS[1]), showgrid=False)
            st.plotly_chart(estilo(fig, "Comparación de indicadores", 520), use_container_width=True)
            if modo.startswith("Valores") and len(recort) > 2:
                st.info("Con más de 2 indicadores en valores originales las escalas pueden no ser comparables; "
                        "probá 'Base 100'.")
    else:
        st.info("Elegí al menos un indicador.")

# ----------------------------------------------------------------------------
# Agente de IA
# ----------------------------------------------------------------------------
with tab_ia:
    st.subheader("Preguntale a los datos")
    st.caption("El agente consulta las series oficiales con herramientas de cálculo; no responde de memoria.")
    ejemplos = ["¿Cuál fue el año con mayor inflación desde 2000?",
                "Compará exportaciones e importaciones de 2018, 2020 y 2023.",
                "¿Cuál fue la variación promedio diaria del tipo de cambio en el último mes?"]
    cols_ej = st.columns(len(ejemplos))
    for c, ej in zip(cols_ej, ejemplos):
        if c.button(ej, use_container_width=True):
            st.session_state["pregunta_pendiente"] = ej

    st.session_state.setdefault("chat", [])
    for m in st.session_state["chat"]:
        with st.chat_message(m["role"]):
            st.markdown(m["content"])
            if m.get("llamadas"):
                with st.expander("Cálculos realizados"):
                    st.json(m["llamadas"])

    pregunta = st.chat_input("Escribí tu pregunta sobre los indicadores…") or st.session_state.pop("pregunta_pendiente", None)
    if pregunta:
        st.session_state["chat"].append({"role": "user", "content": pregunta})
        with st.chat_message("user"):
            st.markdown(pregunta)
        with st.chat_message("assistant"):
            if not api_key:
                resp, llamadas = "Falta la clave de API de Gemini (barra lateral). Se saca gratis en aistudio.google.com.", []
                st.warning(resp)
            else:
                with st.spinner("Consultando los datos…"):
                    try:
                        hist = [{"role": m["role"], "content": m["content"]} for m in st.session_state["chat"][:-1]]
                        resp, llamadas = agente.preguntar(
                            api_key, pregunta, hist, NOMBRES, lambda n: cargar(n, DESDE))
                    except Exception as e:
                        resp, llamadas = f"No pude completar la consulta: {e}", []
                st.markdown(resp)
                if llamadas:
                    with st.expander("Cálculos realizados"):
                        st.json(llamadas)
        st.session_state["chat"].append({"role": "assistant", "content": resp, "llamadas": llamadas})

# ----------------------------------------------------------------------------
# Explorador de series
# ----------------------------------------------------------------------------
with tab_exp:
    st.subheader("Buscar otras series en el catálogo oficial")
    st.caption("Buscá por texto (ej.: 'deuda', 'inversión', 'tasa de interés', 'pobreza') y sumá la serie al tablero; "
               "luego aparece en Evolución, Comparar y el agente.")
    texto = st.text_input("Buscar", key="exp_q")
    if texto:
        try:
            res = buscar_series(texto, 15)
        except Exception as e:
            res = []
            st.warning(f"No se pudo consultar la API: {e}")
        for r in res:
            c1, c2 = st.columns([5, 1])
            c1.markdown(f"**{r['titulo']}**  \n`{r['id']}` · {r.get('unidad') or ''} · "
                        f"{r.get('desde') or ''} → {r.get('hasta') or ''}")
            if c2.button("Agregar", key=f"add_{r['id']}"):
                st.session_state.setdefault("series_extra", {})[r["titulo"][:70]] = {
                    "id": r["id"], "cat": "Personalizada", "fuente": r.get("fuente") or "", "tipo": "indice",
                    "q": r["titulo"]}
                st.rerun()
    with st.expander("Series incluidas y estado de la fuente"):
        filas = []
        for n, e in catalogo_total().items():
            filas.append({"Indicador": n, "Categoría": e["cat"], "Fuente": e["fuente"], "Id": e.get("id") or "(búsqueda)"})
        st.dataframe(pd.DataFrame(filas), use_container_width=True, hide_index=True)
