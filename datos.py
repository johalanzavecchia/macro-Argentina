"""Acceso a datos oficiales (API de Series de Tiempo de Argentina, datos.gob.ar).

Esta API publica series de INDEC, BCRA, Ministerio de Economía, etc.
No requiere clave. Documentación: https://datosgobar.github.io/series-tiempo-ar-api/

Cada indicador del catálogo tiene un `id` de serie y un texto de búsqueda `q`.
Si el id falla o devuelve vacío, se busca automáticamente por texto en el
catálogo oficial de la API, de modo que la app sea robusta ante cambios de ids.
"""
from __future__ import annotations

import pandas as pd
import requests
import streamlit as st

BASE = "https://apis.datos.gob.ar/series/api"
FECHA_INICIO_DEFECTO = "2000-01-01"


class DatosNoDisponibles(Exception):
    pass


# ----------------------------------------------------------------------------
# Catálogo de indicadores
#   tipo: "indice" (nivel acumulado, tiene sentido la variación %),
#         "flujo" (monto por período), "tasa" (ya es un %), "stock"
# ----------------------------------------------------------------------------
CATALOGO: dict[str, dict] = {
    # Precios
    "Inflación - IPC nivel general (índice)": {
        "id": "148.3_INIVELNAL_DICI_M_26", "cat": "Precios", "fuente": "INDEC", "tipo": "indice",
        "q": "IPC nivel general nacional índice dic 2016"},
    "Inflación - IPC núcleo (índice)": {
        "id": "148.3_INUCLEONAL_DICI_M_19", "cat": "Precios", "fuente": "INDEC", "tipo": "indice",
        "q": "IPC núcleo nacional índice dic 2016"},
    # Tipo de cambio
    "Tipo de cambio oficial USD (Com. A 3500)": {
        "id": "168.1_T_CAMBIOR_D_0_0_26", "cat": "Tipo de cambio", "fuente": "BCRA", "tipo": "indice",
        "q": "tipo de cambio de referencia peso dólar comunicación A 3500"},
    # Actividad
    "Actividad - EMAE (desestacionalizado)": {
        "id": "143.3_NO_PR_2004_A_31", "cat": "Actividad", "fuente": "INDEC", "tipo": "indice",
        "q": "EMAE desestacionalizado base 2004"},
    "Actividad - EMAE (serie original)": {
        "id": "143.3_NO_PR_2004_A_21", "cat": "Actividad", "fuente": "INDEC", "tipo": "indice",
        "q": "EMAE serie original base 2004"},
    "PBI - a precios constantes (trimestral)": {
        "id": None, "cat": "Actividad", "fuente": "INDEC", "tipo": "flujo",
        "q": "producto interno bruto a precios de mercado precios constantes 2004 trimestral"},
    # Empleo y salarios
    "Desempleo - Tasa de desocupación": {
        "id": "45.2_ECTDT_0_T_33", "cat": "Empleo", "fuente": "INDEC", "tipo": "tasa",
        "q": "tasa de desocupación total EPH"},
    "Salarios - Índice de salarios": {
        "id": "149.1_TL_INDIIOS_OCTU_0_21", "cat": "Empleo", "fuente": "INDEC", "tipo": "indice",
        "q": "índice de salarios nivel general"},
    # Comercio exterior
    "Comercio - Exportaciones (USD)": {
        "id": "74.3_IET_0_M_16", "cat": "Comercio", "fuente": "INDEC", "tipo": "flujo",
        "q": "exportaciones totales intercambio comercial argentino millones de dólares"},
    "Comercio - Importaciones (USD)": {
        "id": "74.3_IIT_0_M_25", "cat": "Comercio", "fuente": "INDEC", "tipo": "flujo",
        "q": "importaciones totales intercambio comercial argentino millones de dólares"},
    # Sector externo / monetario
    "Reservas internacionales del BCRA": {
        "id": "174.1_RRVAS_IDOS_0_0_36", "cat": "Monetario", "fuente": "BCRA", "tipo": "stock",
        "q": "reservas internacionales BCRA millones de dólares"},
    "Base monetaria": {
        "id": None, "cat": "Monetario", "fuente": "BCRA", "tipo": "stock",
        "q": "base monetaria total millones de pesos"},
    # Inversión y deuda
    "Inversión - Formación bruta de capital fijo": {
        "id": None, "cat": "Inversión", "fuente": "INDEC", "tipo": "flujo",
        "q": "formación bruta de capital fijo precios constantes"},
    "Deuda - Deuda bruta de la Administración Central": {
        "id": None, "cat": "Deuda", "fuente": "Min. de Economía", "tipo": "stock",
        "q": "deuda bruta administración central millones de dólares"},
}


def catalogo_total() -> dict[str, dict]:
    """Catálogo base + series agregadas por el usuario desde el explorador."""
    extra = st.session_state.get("series_extra", {})
    return {**CATALOGO, **extra}


# ----------------------------------------------------------------------------
# Llamadas a la API
# ----------------------------------------------------------------------------
def _get(path: str, params: dict) -> dict:
    r = requests.get(f"{BASE}/{path}", params=params, timeout=30)
    r.raise_for_status()
    return r.json()


@st.cache_data(ttl=3600, show_spinner=False)
def buscar_series(texto: str, limite: int = 15) -> list[dict]:
    """Busca series por texto en el catálogo oficial."""
    j = _get("search/", {"q": texto, "limit": limite})
    salida = []
    for it in j.get("data", []):
        f = it.get("field", {}) or {}
        d = it.get("dataset", {}) or {}
        if not f.get("id"):
            continue
        salida.append({
            "id": f.get("id"),
            "titulo": f.get("description") or f.get("title") or f.get("id"),
            "unidad": f.get("units"),
            "desde": f.get("time_index_start"),
            "hasta": f.get("time_index_end"),
            "fuente": d.get("source") or "",
        })
    return salida


def _frecuencia(idx: pd.DatetimeIndex) -> str:
    if len(idx) < 3:
        return "s/d"
    dias = pd.Series(idx).diff().dt.days.median()
    if dias <= 3:
        return "diaria"
    if dias <= 40:
        return "mensual"
    if dias <= 100:
        return "trimestral"
    return "anual"


@st.cache_data(ttl=3600, show_spinner=False)
def descargar(serie_id: str, desde: str = FECHA_INICIO_DEFECTO) -> tuple[pd.DataFrame, dict]:
    """Descarga una serie completa (paginando de a 1000 filas)."""
    filas: list = []
    inicio = 0
    meta: list = []
    while True:
        j = _get("series", {"ids": serie_id, "start_date": desde, "limit": 1000,
                            "start": inicio, "format": "json", "metadata": "full"})
        if inicio == 0:
            meta = j.get("meta", [])
        datos = j.get("data", [])
        filas += datos
        if len(datos) < 1000:
            break
        inicio += 1000

    df = pd.DataFrame(filas, columns=["fecha", "valor"]) if filas else pd.DataFrame(columns=["fecha", "valor"])
    if not df.empty:
        df["fecha"] = pd.to_datetime(df["fecha"])
        df["valor"] = pd.to_numeric(df["valor"], errors="coerce")
        df = df.dropna().drop_duplicates("fecha").set_index("fecha").sort_index()

    info = {"id": serie_id, "titulo": serie_id, "unidad": "", "frecuencia": _frecuencia(df.index) if not df.empty else "s/d"}
    try:
        campo = meta[1].get("field", {})
        info["titulo"] = campo.get("description") or campo.get("title") or serie_id
        info["unidad"] = campo.get("units", "") or ""
    except Exception:
        pass
    return df, info


def cargar(nombre: str, desde: str = FECHA_INICIO_DEFECTO) -> tuple[pd.DataFrame, dict]:
    """Devuelve (DataFrame con índice fecha y columna 'valor', info) del indicador.

    Prueba primero el id del catálogo y, si falla, busca por texto en la API.
    """
    ent = catalogo_total()[nombre]
    candidatos: list[tuple[str, str]] = []
    if ent.get("id"):
        candidatos.append((ent["id"], "id"))

    ultimo_error = ""
    for sid, via in candidatos:
        try:
            df, info = descargar(sid, desde)
            if not df.empty:
                info["via"] = via
                return df, info
        except Exception as e:  # red, 400, etc.
            ultimo_error = str(e)

    if ent.get("q"):
        try:
            for r in buscar_series(ent["q"], 8):
                if r["id"] in [c[0] for c in candidatos]:
                    continue
                try:
                    df, info = descargar(r["id"], desde)
                except Exception as e:
                    ultimo_error = str(e)
                    continue
                if not df.empty:
                    info["via"] = "búsqueda"
                    return df, info
        except Exception as e:
            ultimo_error = str(e)

    raise DatosNoDisponibles(f"No se pudo obtener '{nombre}'. {ultimo_error}")


# ----------------------------------------------------------------------------
# Transformaciones
# ----------------------------------------------------------------------------
def transformar(s: pd.Series, trans: str) -> pd.Series:
    """trans: 'nivel' | 'var_periodo' | 'var_interanual'."""
    s = s.dropna().sort_index()
    if trans == "nivel":
        return s
    if trans == "var_periodo":
        return s.pct_change() * 100
    if trans == "var_interanual":
        previo = s.reindex(s.index - pd.DateOffset(years=1), method="ffill")
        previo.index = s.index
        return (s / previo - 1) * 100
    raise ValueError(f"Transformación desconocida: {trans}")


def agregar_anual(s: pd.Series, agregacion: str) -> pd.Series:
    """agregacion: 'ninguna' | 'promedio_anual' | 'fin_anual' | 'suma_anual' | 'max_anual'."""
    if agregacion == "ninguna":
        return s
    g = s.dropna().groupby(s.dropna().index.year)
    mapa = {"promedio_anual": g.mean, "fin_anual": g.last, "suma_anual": g.sum, "max_anual": g.max}
    if agregacion not in mapa:
        raise ValueError(f"Agregación desconocida: {agregacion}")
    return mapa[agregacion]()
