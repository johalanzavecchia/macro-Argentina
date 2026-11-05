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
import datos
from datos import (FECHA_INICIO_DEFECTO, NOM_BM, NOM_DES, NOM_EMAE_D, NOM_EXP, NOM_IMP, NOM_IPC, NOM_PA, NOM_PBI,
                   NOM_RES, NOM_SAL_R, NOM_TC, DatosNoDisponibles, buscar_series, cargar, catalogo_total,
                   chequeos_inflacion, inflacion_dic_dic, transformar, verificar_fuentes)

# ----------------------------------------------------------------------------
# Paleta: fondo azul-magenta muy oscuro, magenta, grises y blanco; colores vivos solo para las líneas
# ----------------------------------------------------------------------------
FONDO, PANEL, GRIS, GRIS_CLARO, BLANCO = "#140A2A", "#1F1340", "#3B2A6B", "#A9A3C4", "#F5F5F7"
MAGENTA, MAGENTA_CLARO = "#A8208F", "#D14BBE"
LINEAS = ["#FF4D4D", "#4DA3FF", "#FFD23F", "#3DDC84", "#FF9F43", "#26D0CE", "#F78FB3", "#B8E986"]

st.set_page_config(page_title="Macro Argentina", page_icon="📊", layout="wide")

st.markdown(f"""
<style>
.stApp {{ background: {FONDO}; }}
h1, h2, h3 {{ color: {BLANCO}; }}
h1 span.acento {{ color: {MAGENTA_CLARO}; }}
.kpi {{ background: {PANEL}; border: 1px solid {GRIS}; border-left: 4px solid {MAGENTA};
        border-radius: 8px; padding: 12px 16px; margin-bottom: 10px; }}
.kpi .t {{ color: {GRIS_CLARO}; font-size: 0.78rem; text-transform: uppercase; letter-spacing: .04em; }}
.kpi .v {{ color: {BLANCO}; font-size: 1.6rem; font-weight: 700; }}
.kpi .s {{ color: {GRIS_CLARO}; font-size: 0.75rem; }}
.kpi .pos {{ color: #3DDC84; }} .kpi .neg {{ color: #FF4D4D; }} .kpi .alerta {{ color: #FFD23F; }}
button[kind="primary"] {{ background: {MAGENTA}; border-color: {MAGENTA}; }}
</style>
""", unsafe_allow_html=True)

TRANS_OPC = {"Nivel (valor original)": "nivel",
             "Variación % vs. período anterior": "var_periodo",
             "Variación % interanual": "var_interanual"}
MESES = ["ene", "feb", "mar", "abr", "may", "jun", "jul", "ago", "sep", "oct", "nov", "dic"]


# ----------------------------------------------------------------------------
# Utilidades
# ----------------------------------------------------------------------------
def fmt(x: float, dec: int = 2) -> str:
    return f"{x:,.{dec}f}".replace(",", "X").replace(".", ",").replace("X", ".")


def fecha_corta(ts: pd.Timestamp, frecuencia: str) -> str:
    if frecuencia == "diaria":
        return f"{ts:%d/%m/%Y}"
    if frecuencia == "trimestral":
        return f"T{(ts.month - 1) // 3 + 1} {ts.year}"
    return f"{MESES[ts.month - 1]} {ts.year}"


def avisos(info: dict):
    """Muestra advertencias si la serie está desactualizada o hay que revisarla."""
    if info.get("desactualizada"):
        st.warning(f"Atención: el último dato de esta serie es de {info.get('ultimo')} "
                   f"({info.get('atraso_dias')} días de atraso). Puede estar discontinuada: revisá la pestaña "
                   "'Explorador y fuentes'.")
    elif info.get("revisar"):
        st.info("Esta serie se eligió automáticamente y su título no coincide del todo con lo esperado: "
                "verificala en la pestaña 'Explorador y fuentes'.")


def cargar_seguro(nombre: str, desde: str, mostrar_avisos: bool = True):
    try:
        df, info = cargar(nombre, desde)
        if mostrar_avisos:
            avisos(info)
        return df, info
    except DatosNoDisponibles as e:
        st.warning(f"No se pudo obtener '{nombre}': {e}")
    except Exception as e:
        st.warning(f"Error al cargar '{nombre}': {e}")
    return None, None


def estilo(fig: go.Figure, titulo: str = "", alto: int = 480, selector: bool = True) -> go.Figure:
    fig.update_layout(
        title=dict(text=titulo, font=dict(color=BLANCO, size=16)),
        paper_bgcolor=FONDO, plot_bgcolor=FONDO, height=alto,
        font=dict(color=BLANCO), hovermode="x unified",
        hoverlabel=dict(bgcolor=MAGENTA, font_color=BLANCO),
        legend=dict(orientation="h", y=-0.25, font=dict(color=BLANCO)),
        margin=dict(l=10, r=10, t=50, b=10),
    )
    rs = dict(bgcolor=MAGENTA, activecolor=MAGENTA_CLARO, font=dict(color=BLANCO),
              buttons=[dict(count=1, label="1A", step="year", stepmode="backward"),
                       dict(count=5, label="5A", step="year", stepmode="backward"),
                       dict(step="all", label="Todo")]) if selector else None
    fig.update_xaxes(gridcolor=GRIS, linecolor=GRIS_CLARO, zeroline=False, rangeselector=rs)
    fig.update_yaxes(gridcolor=GRIS, linecolor=GRIS_CLARO, zeroline=False)
    return fig


def kpi(titulo: str, valor: str, sub: str = "", signo: int = 0, alerta: bool = False):
    clase = "alerta" if alerta else "pos" if signo > 0 else "neg" if signo < 0 else ""
    st.markdown(f'<div class="kpi"><div class="t">{titulo}</div><div class="v">{valor}</div>'
                f'<div class="s {clase}">{sub}</div></div>', unsafe_allow_html=True)


def recortar(s: pd.Series, d0: date, d1: date) -> pd.Series:
    return s[(s.index >= pd.Timestamp(d0)) & (s.index <= pd.Timestamp(d1))]


# ----------------------------------------------------------------------------
# Encabezado y barra lateral
# ----------------------------------------------------------------------------
st.markdown('<h1>Macro <span class="acento">Argentina</span></h1>', unsafe_allow_html=True)
st.caption("Datos oficiales obtenidos en vivo: BCRA (API de Principales Variables y Estadísticas Cambiarias) e INDEC "
           "(series publicadas por la API de Series de Tiempo de datos.gob.ar). Sin carga manual.")

with st.sidebar:
    st.header("Configuración")
    inicio = st.date_input("Histórico desde", value=date.fromisoformat(FECHA_INICIO_DEFECTO),
                           min_value=date(1990, 1, 1), max_value=date.today())
    DESDE = inicio.isoformat()
    st.caption("Nota: el IPC nacional del INDEC (base dic 2016) existe desde 2016; el poder adquisitivo y los "
               "salarios reales se calculan desde esa fecha.")
    st.divider()
    st.subheader("Agente de IA")
    try:
        api_key = st.secrets.get("GEMINI_API_KEY", "")
    except Exception:  # no existe secrets.toml: es normal en una instalación local
        api_key = ""
    api_key = api_key or os.environ.get("GEMINI_API_KEY", "")
    if not api_key:
        api_key = st.text_input("Clave de API de Google (Gemini, gratis)", type="password",
                                help="Se saca gratis en aistudio.google.com → Get API key. También podés definir "
                                     "GEMINI_API_KEY como variable de entorno.")
    if st.button("Actualizar datos"):
        st.cache_data.clear()
        st.rerun()

# series agregadas por el usuario desde el explorador
datos.EXTRA.clear()
datos.EXTRA.update(st.session_state.get("series_extra", {}))
NOMBRES = list(catalogo_total().keys())

tab_pan, tab_inf, tab_evo, tab_ia, tab_exp = st.tabs(
    ["Panorama", "Inflación", "Evolución", "Agente IA", "Explorador y fuentes"])

# ----------------------------------------------------------------------------
# Panorama
# ----------------------------------------------------------------------------
with tab_pan:
    st.subheader("Últimos datos disponibles")
    specs = [
        (NOM_IPC, "var_periodo", "Inflación mensual", "%"),
        (NOM_IPC, "var_interanual", "Inflación interanual", "%"),
        (NOM_PA, "nivel", "Poder adquisitivo del peso", "idx"),
        (NOM_TC, "nivel", "Dólar (BCRA)", "$"),
        (NOM_DES, "nivel", "Desocupación", "%"),
        (NOM_EMAE_D, "var_interanual", "EMAE interanual", "%"),
        (NOM_PBI, "var_interanual", "PBI interanual", "%"),
        (NOM_SAL_R, "var_interanual", "Salario real interanual", "%"),
        (NOM_RES, "nivel", "Reservas BCRA", "USD M"),
        (NOM_EXP, "var_interanual", "Exportaciones interanual", "%"),
        (NOM_IMP, "var_interanual", "Importaciones interanual", "%"),
        (NOM_BM, "nivel", "Base monetaria", "$ M"),
    ]
    barra = st.progress(0.0, text="Consultando APIs oficiales…")
    cols = st.columns(4)
    for i, (nom, tr, etiqueta, u) in enumerate(specs):
        barra.progress((i + 1) / len(specs), text=f"Consultando: {etiqueta}")
        with cols[i % 4]:
            try:
                df, info = cargar(nom, DESDE)
                s = transformar(df["valor"], tr).dropna()
                ultimo, fecha = float(s.iloc[-1]), s.index[-1]
                prev = float(s.iloc[-2]) if len(s) > 1 else None
                texto = {"%": f"{fmt(ultimo)}%", "$": f"$ {fmt(ultimo)}", "USD M": f"USD {fmt(ultimo, 0)} M",
                         "$ M": f"$ {fmt(ultimo, 0)} M"}.get(u, fmt(ultimo))
                sub = fecha_corta(fecha, info["frecuencia"])
                vieja = info.get("desactualizada") or info.get("revisar")
                if vieja:
                    sub += " (revisar serie)"
                signo = 0 if (prev is None or u == "%") else (1 if ultimo > prev else -1 if ultimo < prev else 0)
                kpi(etiqueta, texto, sub, signo, alerta=bool(vieja))
            except Exception:
                kpi(etiqueta, "s/d", "no disponible")
    barra.empty()
    st.caption("Los indicadores en variación interanual comparan con el mismo período del año anterior. "
               "El detalle de cada serie usada está en 'Explorador y fuentes'.")

# ----------------------------------------------------------------------------
# Inflación: nominal vs. real
# ----------------------------------------------------------------------------
with tab_inf:
    st.subheader("Inflación: precios (nominal) y poder adquisitivo (real)")
    ipc_df, ipc_info = cargar_seguro(NOM_IPC, DESDE)
    pa_df, _ = cargar_seguro(NOM_PA, DESDE, mostrar_avisos=False)
    if ipc_df is not None:
        ipc = ipc_df["valor"]
        mensual = transformar(ipc, "var_periodo").dropna()
        interanual = transformar(ipc, "var_interanual").dropna()

        a, b, c, d = st.columns(4)
        with a: kpi("Inflación mensual", f"{fmt(mensual.iloc[-1])}%", fecha_corta(mensual.index[-1], "mensual"))
        with b:
            if len(interanual):
                kpi("Inflación interanual", f"{fmt(interanual.iloc[-1])}%", fecha_corta(interanual.index[-1], "mensual"))
        with c: kpi("Inflación acumulada", f"{fmt((ipc.iloc[-1] / ipc.iloc[0] - 1) * 100, 0)}%",
                    f"desde {fecha_corta(ipc.index[0], 'mensual')}")
        with d:
            if pa_df is not None:
                kpi("Poder adquisitivo del peso", fmt(pa_df['valor'].iloc[-1]),
                    f"base 100 = {fecha_corta(pa_df.index[0], 'mensual')}")

        log = st.checkbox("Escala logarítmica (recomendada: el IPC crece muy rápido)", value=True, key="inf_log")
        fig = make_subplots(specs=[[{"secondary_y": True}]])
        fig.add_trace(go.Scatter(x=ipc.index, y=ipc.values, name="IPC (precios, nominal)",
                                 line=dict(color=LINEAS[0], width=2.6)), secondary_y=False)
        if pa_df is not None:
            fig.add_trace(go.Scatter(x=pa_df.index, y=pa_df["valor"].values, name="Poder adquisitivo del peso (real)",
                                     line=dict(color=LINEAS[3], width=2.6)), secondary_y=True)
        if log:
            fig.update_yaxes(type="log", secondary_y=False)
            fig.update_yaxes(type="log", secondary_y=True)
        fig.update_yaxes(title_text="IPC nominal (dic 2016 = 100)", secondary_y=False, title_font=dict(color=LINEAS[0]))
        fig.update_yaxes(title_text="Poder adquisitivo (base 100)", secondary_y=True, showgrid=False,
                         title_font=dict(color=LINEAS[3]))
        st.plotly_chart(estilo(fig, "Precios (nominal) vs. poder adquisitivo del peso (real)", 480),
                        use_container_width=True)
        st.caption("Los precios suben (línea roja) y lo que se puede comprar con un peso baja (línea verde): "
                   "son dos formas de ver el mismo fenómeno.")

        fig2 = make_subplots(specs=[[{"secondary_y": True}]])
        fig2.add_trace(go.Bar(x=mensual.index, y=mensual.values, name="Inflación mensual (%)",
                              marker_color=LINEAS[1], opacity=0.85), secondary_y=False)
        fig2.add_trace(go.Scatter(x=interanual.index, y=interanual.values, name="Inflación interanual (%)",
                                  line=dict(color=LINEAS[2], width=2.6)), secondary_y=True)
        fig2.update_yaxes(title_text="mensual %", secondary_y=False)
        fig2.update_yaxes(title_text="interanual %", secondary_y=True, showgrid=False)
        st.plotly_chart(estilo(fig2, "Inflación mensual e interanual (IPC nacional, INDEC)", 420),
                        use_container_width=True)

        anual = inflacion_dic_dic(ipc)
        if len(anual):
            st.markdown("**Inflación anual, diciembre a diciembre** (calculada con el IPC del INDEC)")
            tabla = pd.DataFrame({"Año": anual.index, "Inflación (%)": anual.round(1).values}).set_index("Año")
            st.dataframe(tabla.T, use_container_width=True)
        st.caption(f"Fuente: INDEC, serie {ipc_info['id']} ({ipc_info['api']}). Último dato: {ipc_info['ultimo']}.")

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
        if s_full.empty:
            st.info("No hay datos para esta combinación.")
        else:
            rango = st.slider("Rango de fechas", min_value=s_full.index.min().date(),
                              max_value=s_full.index.max().date(),
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
            st.caption(f"Fuente: {info.get('fuente')} · {info.get('api')} · Serie: {info['id']} · "
                       f"Unidad: {info.get('unidad') or 's/d'} · Frecuencia: {info['frecuencia']} · "
                       f"Último dato: {info.get('ultimo')}")
            if not s.empty:
                a, b, c, d = st.columns(4)
                with a: kpi("Último valor", fmt(s.iloc[-1]), f"{s.index[-1]:%d/%m/%Y}")
                with b: kpi("Máximo", fmt(s.max()), f"{s.idxmax():%d/%m/%Y}")
                with c: kpi("Mínimo", fmt(s.min()), f"{s.idxmin():%d/%m/%Y}")
                with d: kpi("Promedio", fmt(s.mean()), f"{len(s)} observaciones")
            with st.expander("Ver tabla y descargar"):
                st.dataframe(s.rename("valor").sort_index(ascending=False), use_container_width=True)
                st.download_button("Descargar CSV", s.rename("valor").to_csv().encode(),
                                   f"{info['id'].replace(' ', '_')}.csv", "text/csv")

# ----------------------------------------------------------------------------
# Agente de IA
# ----------------------------------------------------------------------------
with tab_ia:
    st.subheader("Preguntale a los datos")
    st.caption("El agente consulta las series oficiales con herramientas de cálculo; no responde de memoria.")
    ejemplos = ["¿Cuál fue el año con mayor inflación desde 2017?",
                "Compará exportaciones e importaciones de 2022, 2023 y 2024.",
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
                        resp, llamadas = agente.preguntar(api_key, pregunta, hist, NOMBRES, lambda n: cargar(n, DESDE))
                    except Exception as e:
                        resp, llamadas = f"No pude completar la consulta: {e}", []
                st.markdown(resp)
                if llamadas:
                    with st.expander("Cálculos realizados"):
                        st.json(llamadas)
        st.session_state["chat"].append({"role": "assistant", "content": resp, "llamadas": llamadas})

# ----------------------------------------------------------------------------
# Explorador de series y estado de las fuentes
# ----------------------------------------------------------------------------
with tab_exp:
    st.subheader("Estado de las fuentes")
    st.caption("Muestra qué serie oficial se está usando para cada indicador, su último dato y si hay algo para revisar.")
    if st.button("Verificar todas las fuentes"):
        with st.spinner("Consultando las APIs…"):
            st.session_state["verif"] = verificar_fuentes(DESDE)
            try:
                st.session_state["chequeo"] = chequeos_inflacion()
            except Exception as e:
                st.session_state["chequeo"] = f"No se pudo calcular el chequeo: {e}"
    if "verif" in st.session_state:
        st.dataframe(st.session_state["verif"], use_container_width=True, hide_index=True)
    if "chequeo" in st.session_state:
        st.markdown("**Chequeo de la inflación contra cifras publicadas por el INDEC (diciembre a diciembre)**")
        if isinstance(st.session_state["chequeo"], str):
            st.warning(st.session_state["chequeo"])
        else:
            st.dataframe(st.session_state["chequeo"], use_container_width=True, hide_index=True)

    st.divider()
    st.subheader("Buscar otras series en el catálogo oficial")
    st.caption("Buscá por texto (ej.: 'pobreza', 'tasa de interés', 'canasta básica') y sumá la serie al tablero.")
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
                        f"{r.get('desde') or ''} → {r.get('hasta') or ''} · {r.get('fuente') or ''}")
            if c2.button("Agregar", key=f"add_{r['id']}"):
                st.session_state.setdefault("series_extra", {})[str(r["titulo"])[:70]] = {
                    "cat": "Personalizada", "fuente": r.get("fuente") or "API de series", "unidad": r.get("unidad") or "",
                    "indec": {"ids": (r["id"],), "forzar": True}}
                st.rerun()
