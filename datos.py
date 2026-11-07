"""Capa de datos del tablero. Todo sale de APIs oficiales, sin carga manual.

Fuentes
-------
* BCRA - API "Principales Variables" v4.0 (con v3.0 de respaldo)   https://api.bcra.gob.ar/estadisticas/v4.0/monetarias
        (tipo de cambio de referencia, reservas internacionales, base monetaria)
* BCRA - API "Estadísticas Cambiarias" v1.0 https://api.bcra.gob.ar/estadisticascambiarias/v1.0
        (cotización del dólar; respaldo del anterior)
* INDEC - sus series (IPC, EMAE, PBI, inversión, salarios, desocupación, comercio exterior)
        se distribuyen por la API de Series de Tiempo de la República Argentina
        https://apis.datos.gob.ar/series/api , donde INDEC figura como fuente.
        No encontré una API REST propia del INDEC; esta es la vía oficial de publicación.

Control de calidad: cada serie elegida se valida (el título tiene que corresponder a lo buscado y
el último dato tiene que ser reciente). Si no, se descarta, se prueba otra y se informa.
Si una serie no es la correcta, se puede forzar su id en OVERRIDE_IDS.
"""
import os
import re
import time
import unicodedata
from datetime import date

import pandas as pd
import requests

try:  # dentro de Streamlit se cachea por 1 hora; en Colab/consola se usa un caché simple en memoria
    import streamlit as st

    _cache = st.cache_data(ttl=3600, show_spinner=False)
except Exception:  # pragma: no cover
    import functools

    def _cache(f):
        return functools.lru_cache(maxsize=None)(f)


SERIES_API = "https://apis.datos.gob.ar/series/api"
# Principales Variables: se prueba primero la v4.0 y, si no responde, la v3.0
BCRA_VAR_VERSIONES = ("https://api.bcra.gob.ar/estadisticas/v4.0/monetarias",
                      "https://api.bcra.gob.ar/estadisticas/v3.0/monetarias")
BCRA_COT = "https://api.bcra.gob.ar/estadisticascambiarias/v1.0/Cotizaciones"
FECHA_INICIO_DEFECTO = "2000-01-01"

# Si la verificación muestra que una serie no es la correcta, pegá acá el id correcto:
#   OVERRIDE_IDS = {"PBI - precios constantes (trimestral)": "9.2_xxxxxxxx"}
OVERRIDE_IDS: dict = {}
# Series agregadas desde el explorador (las completa la app)
EXTRA: dict = {}

# Días máximos de atraso aceptables según la frecuencia de la serie
MAX_ATRASO = {"diaria": 20, "mensual": 130, "trimestral": 300, "anual": 560}

MSG_SSL = ("Error de certificado SSL al conectar con api.bcra.gob.ar (la cadena de certificados de esa API suele dar "
           "este problema). Si te pasa, definí la variable de entorno BCRA_SSL_VERIFY=0 (solo afecta a esa API "
           "pública, que no maneja datos personales) y volvé a ejecutar.")


class DatosNoDisponibles(Exception):
    pass


# ----------------------------------------------------------------------------
# Catálogo de indicadores
# ----------------------------------------------------------------------------
NOM_IPC = "IPC nacional - nivel general (índice)"
NOM_PA = "Poder adquisitivo del peso (base 100)"
NOM_TC = "Dólar - tipo de cambio de referencia (BCRA)"
NOM_EMAE_D = "EMAE - desestacionalizado"
NOM_EMAE_O = "EMAE - serie original"
NOM_PBI = "PBI - precios constantes (trimestral)"
NOM_INV = "Inversión - formación bruta de capital fijo"
NOM_DES = "Desocupación - tasa (EPH)"
NOM_SAL_N = "Salarios - índice nominal (base 100)"
NOM_SAL_R = "Salarios - índice real (base 100)"
NOM_SAL_RAW = "Salarios - índice de salarios INDEC (original)"
NOM_EXP = "Comercio - exportaciones"
NOM_IMP = "Comercio - importaciones"
NOM_RES = "Reservas internacionales (BCRA)"
NOM_BM = "Base monetaria (BCRA)"

_EXCL_IPC = ("region", "gba", "cuyo", "noa", "nea", "patagonia", "pampeana", "nucleo", "regulados",
             "estacional", "variacion", "incidencia")
_EXCL_COMUN = ("variacion", "participacion", "per capita", "deflactor", "implicito", "incidencia")

CATALOGO: dict = {
    NOM_IPC: {
        "cat": "Precios", "fuente": "INDEC", "unidad": "Índice (dic 2016 = 100)",
        "indec": {"ids": ("148.3_INIVELNAL_DICI_M_26",),
                  "busquedas": ("IPC nacional nivel general índice diciembre 2016",
                                "índice de precios al consumidor nivel general nacional"),
                  "incluir": ("nivel general",), "excluir": _EXCL_IPC, "frecuencia": "mensual"}},
    NOM_PA: {
        "cat": "Precios", "fuente": "INDEC (cálculo propio)", "unidad": "Base 100",
        "derivada": {"tipo": "poder_adquisitivo", "ipc": NOM_IPC}},
    NOM_TC: {
        "cat": "Tipo de cambio", "fuente": "BCRA", "unidad": "$ por USD",
        "bcra": {"variable": {"incluir": ("tipo de cambio",), "incluir_alguno": ("3500", "referencia", "mayorista"),
                              "excluir": ("minorista", "implicito", "bna", "banco de la nacion"),
                              "preferir": ("3500", "referencia", "mayorista")},
                 "variable_id_respaldo": 5, "cotizacion": "USD"}},
    NOM_EMAE_D: {
        "cat": "Actividad", "fuente": "INDEC", "unidad": "Índice 2004 = 100",
        "indec": {"ids": ("143.3_NO_PR_2004_A_31",),
                  "busquedas": ("EMAE desestacionalizado", "estimador mensual de actividad económica desestacionalizado"),
                  "incluir": ("desestacionalizad",), "incluir_alguno": ("emae", "estimador mensual"),
                  "excluir": ("tendencia", "variacion"), "frecuencia": "mensual"}},
    NOM_EMAE_O: {
        "cat": "Actividad", "fuente": "INDEC", "unidad": "Índice 2004 = 100",
        "indec": {"ids": ("143.3_NO_PR_2004_A_21",),
                  "busquedas": ("EMAE serie original", "estimador mensual de actividad económica"),
                  "incluir_alguno": ("emae", "estimador mensual"),
                  "excluir": ("desestacionalizad", "tendencia", "variacion"), "frecuencia": "mensual"}},
    NOM_PBI: {
        "cat": "Actividad", "fuente": "INDEC", "unidad": "Millones de pesos constantes",
        "indec": {"ids": (),
                  "busquedas": ("producto interno bruto millones de pesos de 2004 trimestral",
                                "oferta global PIB pesos de 2004 trimestral",
                                "PIB a precios de mercado precios constantes trimestral",
                                "oferta y demanda globales PIB", "producto interno bruto trimestral"),
                  "incluir_alguno": ("pib", "producto interno bruto"),
                  "excluir": ("desestacionalizad", "corrientes", "dolar") + _EXCL_COMUN, "frecuencia": "trimestral"}},
    NOM_INV: {
        "cat": "Inversión", "fuente": "INDEC", "unidad": "Millones de pesos constantes",
        "indec": {"ids": (),
                  "busquedas": ("formación bruta de capital fijo precios constantes",
                                "inversión bruta interna fija", "oferta y demanda globales inversión"),
                  "incluir_alguno": ("formacion bruta de capital fijo", "inversion bruta interna fija", "fbcf"),
                  "excluir": ("desestacionalizad", "corrientes", "dolar") + _EXCL_COMUN, "frecuencia": "trimestral"}},
    NOM_DES: {
        "cat": "Empleo", "fuente": "INDEC", "unidad": "% de la PEA",
        "indec": {"ids": ("45.2_ECTDT_0_T_33",),
                  "busquedas": ("tasa de desocupación total aglomerados urbanos EPH", "tasa de desocupación"),
                  "incluir_alguno": ("desocupaci", "desempleo"), "excluir": ("subocup", "variacion", "varones", "mujeres"),
                  "frecuencia": "trimestral", "proporcion_a_porcentaje": True}},
    NOM_SAL_RAW: {
        "cat": "Empleo", "fuente": "INDEC", "unidad": "Índice (oct 2016 = 100)",
        "indec": {"ids": ("149.1_TL_INDIIOS_OCTU_0_21",),
                  "busquedas": ("índice de salarios nivel general", "índice de salarios total"),
                  "incluir_alguno": ("salario",),
                  "excluir": ("no registrado", "privado", "publico", "sector", "variacion"), "frecuencia": "mensual"}},
    NOM_SAL_N: {
        "cat": "Empleo", "fuente": "INDEC (cálculo propio)", "unidad": "Base 100",
        "derivada": {"tipo": "nominal", "base": NOM_SAL_RAW, "ipc": NOM_IPC}},
    NOM_SAL_R: {
        "cat": "Empleo", "fuente": "INDEC (cálculo propio)", "unidad": "Base 100",
        "derivada": {"tipo": "real", "base": NOM_SAL_RAW, "ipc": NOM_IPC}},
    NOM_EXP: {
        "cat": "Comercio", "fuente": "INDEC", "unidad": "Millones de USD",
        "indec": {"ids": ("74.3_IET_0_M_16",),
                  "busquedas": ("exportaciones totales intercambio comercial argentino", "ICA exportaciones"),
                  "incluir_alguno": ("exportacion",),
                  "excluir": ("precio", "cantidad", "quantum", "valor unitario", "variacion", "termino"),
                  "frecuencia": "mensual"}},
    NOM_IMP: {
        "cat": "Comercio", "fuente": "INDEC", "unidad": "Millones de USD",
        "indec": {"ids": ("74.3_IIT_0_M_25",),
                  "busquedas": ("importaciones totales intercambio comercial argentino", "ICA importaciones"),
                  "incluir_alguno": ("importacion",),
                  "excluir": ("precio", "cantidad", "quantum", "valor unitario", "variacion", "termino"),
                  "frecuencia": "mensual"}},
    NOM_RES: {
        "cat": "Monetario", "fuente": "BCRA", "unidad": "Millones de USD",
        "bcra": {"variable": {"incluir": ("reservas internacionales",)}}},
    NOM_BM: {
        "cat": "Monetario", "fuente": "BCRA", "unidad": "Millones de pesos",
        "bcra": {"variable": {"incluir": ("base monetaria",), "preferir": ("total",)}}},
}


def catalogo_total() -> dict:
    """Catálogo base + series agregadas por el usuario desde el explorador."""
    return {**CATALOGO, **EXTRA}


# ----------------------------------------------------------------------------
# Utilidades
# ----------------------------------------------------------------------------
def _norm(t) -> str:
    return unicodedata.normalize("NFKD", str(t or "")).encode("ascii", "ignore").decode().lower()


def _tiene(texto_norm: str, clave: str) -> bool:
    """La palabra/frase aparece (comienzo de palabra, así 'nea' no coincide con 'lineal')."""
    return re.search(r"(?<![a-z0-9])" + re.escape(_norm(clave)), texto_norm) is not None


def _coincide(texto: str, incluir=(), incluir_alguno=(), excluir=()) -> bool:
    t = _norm(texto)
    return (all(_tiene(t, k) for k in incluir)
            and (not incluir_alguno or any(_tiene(t, k) for k in incluir_alguno))
            and not any(_tiene(t, k) for k in excluir))


def _frecuencia(idx) -> str:
    if len(idx) < 3:
        return "s/d"
    d = pd.Series(idx).diff().dt.days.median()
    return "diaria" if d <= 5 else "mensual" if d <= 40 else "trimestral" if d <= 100 else "anual"


def _a_df(filas) -> pd.DataFrame:
    if not filas:
        return pd.DataFrame({"valor": pd.Series(dtype=float)})
    df = pd.DataFrame(filas, columns=["fecha", "valor"])
    df["fecha"] = pd.to_datetime(df["fecha"], errors="coerce")
    df["valor"] = pd.to_numeric(df["valor"], errors="coerce")
    return df.dropna().drop_duplicates("fecha").set_index("fecha").sort_index()


def _estado(df: pd.DataFrame, frecuencia: str):
    atraso = (date.today() - df.index.max().date()).days
    return atraso, atraso > MAX_ATRASO.get(frecuencia, 130)


def _verificar_ssl(url: str) -> bool:
    if "api.bcra.gob.ar" in url and os.environ.get("BCRA_SSL_VERIFY", "1").strip().lower() in ("0", "false", "no"):
        return False
    return True


def _get_json(url: str, params: dict | None = None, intentos: int = 3):
    ultimo = None
    for i in range(intentos):
        try:
            r = requests.get(url, params=params, timeout=40, verify=_verificar_ssl(url),
                             headers={"Accept": "application/json", "User-Agent": "macro-argentina/1.0"})
        except requests.exceptions.SSLError as e:
            raise DatosNoDisponibles(MSG_SSL) from e
        except requests.exceptions.RequestException as e:
            ultimo = e
            time.sleep(1.5 * (i + 1))
            continue
        if r.status_code == 429 or r.status_code >= 500:
            ultimo = f"HTTP {r.status_code}"
            time.sleep(1.5 * (i + 1))
            continue
        if r.status_code >= 400:
            raise DatosNoDisponibles(f"HTTP {r.status_code} en {url} {params or ''}: {r.text[:150]}")
        try:
            return r.json()
        except ValueError as e:
            raise DatosNoDisponibles(f"Respuesta no válida de {url}") from e
    raise DatosNoDisponibles(f"No se pudo consultar {url}: {ultimo}")


# ----------------------------------------------------------------------------
# BCRA
# ----------------------------------------------------------------------------
@_cache
def bcra_listar_variables():
    """Devuelve (url_base, lista de variables). Prueba v4.0 y luego v3.0, y pagina el listado completo."""
    ultimo = None
    for base in BCRA_VAR_VERSIONES:
        try:
            res, offset = [], 0
            while True:
                j = _get_json(base, {"offset": offset, "limit": 1000})
                lote = j.get("results", [])
                res += lote
                total = j.get("metadata", {}).get("resultset", {}).get("count", len(res))
                offset += len(lote)
                if not lote or offset >= total:
                    break
            if res:
                return base, res
        except DatosNoDisponibles as e:
            ultimo = e
    raise DatosNoDisponibles(f"No se pudo listar las variables del BCRA (v4.0 ni v3.0): {ultimo}")


@_cache
def bcra_descargar_variable(base: str, id_var: int, desde: str) -> pd.DataFrame:
    filas, offset = [], 0
    while True:
        j = _get_json(f"{base}/{id_var}", {"desde": desde, "limit": 3000, "offset": offset})
        res = j.get("results", [])
        n = 0
        for r in res:  # formato anidado {detalle: [{fecha, valor}]} (v4.0) o plano {fecha, valor} (v3.0)
            if "detalle" in r:
                filas += [(d["fecha"], d["valor"]) for d in r["detalle"]]
                n += len(r["detalle"])
            else:
                filas.append((r["fecha"], r["valor"]))
                n += 1
        total = j.get("metadata", {}).get("resultset", {}).get("count", len(filas))
        offset += n
        if n == 0 or offset >= total:
            break
    return _a_df(filas)


@_cache
def _cotizacion_ventana(moneda: str, d0: date, d1: date) -> list:
    """Descarga una ventana de fechas de Estadísticas Cambiarias (con paginado)."""
    filas, offset = [], 0
    while True:
        j = _get_json(f"{BCRA_COT}/{moneda}", {"fechadesde": d0.isoformat(), "fechahasta": d1.isoformat(),
                                               "limit": 1000, "offset": offset})
        res = j.get("results", [])
        for r in res:
            for d in r.get("detalle", []):
                if d.get("codigoMoneda") == moneda:
                    filas.append((r["fecha"], d["tipoCotizacion"]))
        total = j.get("metadata", {}).get("resultset", {}).get("count", len(res))
        offset += len(res)
        if not res or offset >= total:
            return filas


def bcra_descargar_cotizacion(moneda: str, desde: str) -> pd.DataFrame:
    """Estadísticas Cambiarias: se pide por ventanas anuales (la API rechaza rangos muy largos)."""
    hoy = date.today()
    filas, errores, y = [], [], pd.Timestamp(desde).year
    while y <= hoy.year:
        d0, d1 = date(y, 1, 1), min(date(y, 12, 31), hoy)
        try:
            filas += _cotizacion_ventana(moneda, d0, d1)
        except DatosNoDisponibles as e:
            errores.append(f"{y}: {str(e)[:120]}")
        y += 1
    if not filas:
        raise DatosNoDisponibles("Estadísticas Cambiarias sin datos. " + " | ".join(errores[:3]))
    return _a_df(filas)


def _resolver_bcra(nombre: str, ent: dict, desde: str):
    cfg, notas, vieja = ent["bcra"], [], None

    def info_de(df, ident, titulo, via, api):
        frec = _frecuencia(df.index)
        atraso, vieja_ = _estado(df, frec)
        return {"id": ident, "titulo": titulo, "unidad": ent.get("unidad", ""), "frecuencia": frec,
                "fuente": "BCRA", "api": api, "via": via, "ultimo": str(df.index.max().date()),
                "atraso_dias": atraso, "desactualizada": vieja_, "revisar": False, "notas": notas}

    v = cfg.get("variable")
    if v:
        base_url = BCRA_VAR_VERSIONES[0]
        try:
            base_url, lista = bcra_listar_variables()
            cands = [x for x in lista
                     if _coincide(x.get("descripcion", ""), v.get("incluir", ()), v.get("incluir_alguno", ()),
                                  v.get("excluir", ()))]
            if not cands and cfg.get("variable_id_respaldo"):
                cands = [{"idVariable": cfg["variable_id_respaldo"], "descripcion": nombre}]
                notas.append("no se halló la variable por descripción: se prueba el id histórico de Principales Variables")
            pref = v.get("preferir", ())
            cands.sort(key=lambda x: (-int(_tiene(_norm(x.get("categoria", "")), "principales")),
                                      -sum(_tiene(_norm(x.get("descripcion", "")), p) for p in pref),
                                      x.get("idVariable", 0)))
        except Exception as e:
            cands = []
            notas.append(f"No se pudo listar las variables del BCRA: {str(e)[:200]}")
        version = "v4.0" if "v4.0" in base_url else "v3.0"
        for x in cands[:3]:
            try:
                df = bcra_descargar_variable(base_url, int(x["idVariable"]), desde)
            except Exception as e:
                notas.append(f"variable {x.get('idVariable')}: error ({str(e)[:100]})")
                continue
            if df.empty:
                notas.append(f"variable {x.get('idVariable')}: sin datos")
                continue
            info = info_de(df, f"BCRA variable {x['idVariable']}", x.get("descripcion", ""), "id",
                           f"BCRA - Principales Variables {version}")
            if not info["desactualizada"]:
                return df, info
            notas.append(f"variable {x['idVariable']}: desactualizada (último dato {info['ultimo']})")
            if vieja is None or df.index.max() > vieja[0].index.max():
                vieja = (df, info)
        if not cands:
            notas.append("no se encontró una variable con esa descripción en Principales Variables")

    if cfg.get("cotizacion"):
        mon = cfg["cotizacion"]
        try:
            df = bcra_descargar_cotizacion(mon, desde)
            if not df.empty:
                info = info_de(df, f"BCRA cotización {mon}", f"Cotización {mon} (Estadísticas Cambiarias)",
                               "respaldo", "BCRA - Estadísticas Cambiarias v1.0")
                if not info["desactualizada"] or vieja is None or df.index.max() > vieja[0].index.max():
                    return df, info
        except Exception as e:
            notas.append(f"cotizaciones {mon}: error ({str(e)[:100]})")

    if vieja:
        vieja[1]["revisar"] = True
        return vieja
    raise DatosNoDisponibles(f"No se pudo obtener '{nombre}' del BCRA. " + " | ".join(notas))


# ----------------------------------------------------------------------------
# API de Series de Tiempo (INDEC y otros organismos)
# ----------------------------------------------------------------------------
@_cache
def buscar_series(texto: str, limite: int = 20) -> list:
    """Busca series por texto en el catálogo oficial."""
    j = _get_json(f"{SERIES_API}/search/", {"q": texto, "limit": limite})
    salida = []
    for it in j.get("data", []):
        f, d = it.get("field") or {}, it.get("dataset") or {}
        if not f.get("id"):
            continue
        salida.append({"id": f["id"], "titulo": f.get("description") or f.get("title") or f["id"],
                       "corto": f.get("title") or "", "unidad": f.get("units"),
                       "desde": f.get("time_index_start"), "hasta": f.get("time_index_end"),
                       "dataset": d.get("title") or "", "fuente": d.get("source") or ""})
    return salida


@_cache
def serie_descargar(serie_id: str, desde: str):
    """Descarga una serie completa (paginada de a 1000). Devuelve (DataFrame, info)."""
    filas, inicio, meta = [], 0, []
    while True:
        j = _get_json(f"{SERIES_API}/series", {"ids": serie_id, "start_date": desde, "limit": 1000, "start": inicio,
                                               "format": "json", "metadata": "full"})
        if inicio == 0:
            meta = j.get("meta", [])
        datos = j.get("data", [])
        filas += datos
        if len(datos) < 1000:
            break
        inicio += 1000
    df = _a_df(filas)
    info = {"id": serie_id, "titulo": serie_id, "corto": "", "unidad": "", "dataset": "", "fuente": ""}
    try:
        campo = meta[1].get("field", {}) or {}
        ds = meta[1].get("dataset", {}) or {}
        info.update({"titulo": campo.get("description") or campo.get("title") or serie_id,
                     "corto": campo.get("title") or "", "unidad": campo.get("units", "") or "",
                     "dataset": ds.get("title", "") or "", "fuente": ds.get("source", "") or ""})
    except Exception:
        pass
    return df, info


def _resolver_indec(nombre: str, ent: dict, desde: str):
    cfg = ent["indec"]
    incl, alg, excl = cfg.get("incluir", ()), cfg.get("incluir_alguno", ()), cfg.get("excluir", ())
    frec_esp = cfg.get("frecuencia")
    notas, dudosa, vieja = [], None, None
    forzado = OVERRIDE_IDS.get(nombre)
    ids = (forzado,) if forzado else tuple(cfg.get("ids", ()))
    forzar = bool(forzado) or bool(cfg.get("forzar"))

    def evaluar(sid, via):
        """Devuelve (df, info) si la serie es válida y reciente; si no, None (anotando por qué)."""
        nonlocal dudosa, vieja
        try:
            df, base = serie_descargar(sid, desde)
        except Exception as e:
            notas.append(f"{sid}: error al descargar ({str(e)[:100]})")
            return None
        if df.empty:
            notas.append(f"{sid}: sin datos")
            return None
        if cfg.get("proporcion_a_porcentaje") and float(df["valor"].max()) <= 1.5:
            # La API publica esta serie como proporción (0,08 = 8%): se pasa a porcentaje
            df = df.assign(valor=df["valor"] * 100)
            notas.append("valores originales en proporción (0 a 1): se muestran multiplicados por 100 (en %)")
        frec = _frecuencia(df.index)
        atraso, es_vieja = _estado(df, frec)
        info = {**base, "frecuencia": frec, "fuente": base.get("fuente") or ent.get("fuente", ""),
                "api": "API de Series de Tiempo (datos.gob.ar)", "via": via, "ultimo": str(df.index.max().date()),
                "atraso_dias": atraso, "desactualizada": es_vieja, "revisar": False, "notas": notas,
                "unidad": base.get("unidad") or ent.get("unidad", "")}
        if forzar:
            return df, info
        texto = f"{base.get('titulo')} {base.get('corto')} {base.get('dataset')} {base.get('unidad')}"
        coincide = _coincide(texto, incl, alg, excl) and (not frec_esp or frec == frec_esp)
        if not coincide:
            notas.append(f"{sid}: no coincide con lo buscado ('{str(base.get('titulo'))[:70]}', frecuencia {frec})")
            if dudosa is None and not es_vieja:
                dudosa = (df, {**info, "revisar": True})
            return None
        if es_vieja:
            notas.append(f"{sid}: desactualizada (último dato {info['ultimo']})")
            if vieja is None or df.index.max() > vieja[0].index.max():
                vieja = (df, {**info, "revisar": True})
            return None
        return df, info

    for sid in ids:
        r = evaluar(sid, "override" if forzado else "id")
        if r:
            return r

    # Búsqueda por texto en el catálogo oficial
    vistos, cands = set(ids), []
    clave = _norm(cfg.get("fuente_clave", "indec"))
    for q in cfg.get("busquedas", ()):
        try:
            res = buscar_series(q, 30)
        except Exception as e:
            notas.append(f"búsqueda '{q}': error ({str(e)[:100]})")
            continue
        for r in res:
            if r["id"] in vistos:
                continue
            vistos.add(r["id"])
            if _coincide(f"{r['titulo']} {r['corto']} {r['dataset']} {r.get('unidad') or ''}", incl, alg, excl):
                cands.append(r)
    cands.sort(key=lambda r: ((clave in _norm(r.get("fuente", "") + " " + r.get("dataset", ""))) if clave else True,
                              r.get("hasta") or ""), reverse=True)
    for r in cands[:6]:
        res = evaluar(r["id"], "búsqueda")
        if res:
            return res

    if dudosa:
        return dudosa
    if vieja:
        return vieja
    raise DatosNoDisponibles(f"No se encontró una serie válida para '{nombre}'. " + " | ".join(notas[:6]))


# ----------------------------------------------------------------------------
# Series derivadas (poder adquisitivo, salarios reales)
# ----------------------------------------------------------------------------
def _a_mes(s: pd.Series) -> pd.Series:
    s = s.copy()
    s.index = s.index.to_period("M").to_timestamp()
    return s.groupby(level=0).last()


def _cargar_derivada(nombre: str, ent: dict, desde: str):
    d = ent["derivada"]
    ipc_df, ipc_info = cargar(d["ipc"], desde)
    ipc = _a_mes(ipc_df["valor"])
    comp = [ipc_info]
    if d["tipo"] == "poder_adquisitivo":
        s = ipc.iloc[0] / ipc * 100
        texto = "Poder adquisitivo del peso = 100 × IPC inicial / IPC (valor real de un peso respecto del primer mes)"
    else:
        b_df, b_info = cargar(d["base"], desde)
        comp.append(b_info)
        com = pd.concat([_a_mes(b_df["valor"]), ipc], axis=1, join="inner").dropna()
        com.columns = ["b", "ipc"]
        if com.empty:
            raise DatosNoDisponibles(f"No hay meses en común entre {d['base']} y el IPC.")
        if d["tipo"] == "nominal":
            s = com["b"] / com["b"].iloc[0] * 100
            texto = "Índice de salarios nominal, reexpresado en base 100 en el primer mes en común con el IPC"
        else:
            real = com["b"] / com["ipc"]
            s = real / real.iloc[0] * 100
            texto = "Salarios reales = índice de salarios / IPC, base 100 en el primer mes en común"
    df = pd.DataFrame({"valor": s}).dropna()
    primera = df.index.min()
    info = {"id": "calculada", "titulo": f"{nombre}. {texto}", "unidad": f"Base 100 = {primera:%m/%Y}",
            "frecuencia": "mensual", "fuente": ent.get("fuente", ""), "api": "Cálculo propio a partir de: " +
            " + ".join(i["id"] for i in comp), "via": "calculada", "ultimo": str(df.index.max().date()),
            "atraso_dias": (date.today() - df.index.max().date()).days,
            "desactualizada": any(i.get("desactualizada") for i in comp),
            "revisar": any(i.get("revisar") for i in comp), "notas": [n for i in comp for n in i.get("notas", [])]}
    return df, info


# ----------------------------------------------------------------------------
# Carga de un indicador
# ----------------------------------------------------------------------------
def cargar(nombre: str, desde: str = FECHA_INICIO_DEFECTO):
    """Devuelve (DataFrame con índice fecha y columna 'valor', info) del indicador."""
    cat = catalogo_total()
    if nombre not in cat:
        raise DatosNoDisponibles(f"Indicador desconocido: {nombre}")
    ent = cat[nombre]
    if "derivada" in ent:
        return _cargar_derivada(nombre, ent, desde)
    if "bcra" in ent:
        return _resolver_bcra(nombre, ent, desde)
    return _resolver_indec(nombre, ent, desde)


# ----------------------------------------------------------------------------
# Transformaciones
# ----------------------------------------------------------------------------
def transformar(s: pd.Series, trans: str) -> pd.Series:
    """trans: 'nivel' | 'var_periodo' | 'var_interanual' (en %)."""
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


def inflacion_dic_dic(ipc: pd.Series) -> pd.Series:
    """Inflación anual diciembre a diciembre (%), indexada por año."""
    s = ipc.dropna().sort_index()
    dic = s[s.index.month == 12]
    r = (dic.pct_change() * 100).dropna()
    r.index = r.index.year
    return r


def candidatos(nombre: str, limite: int = 25) -> pd.DataFrame:
    """Series candidatas para un indicador (sin aplicar el filtro de frescura), para elegir el id correcto a mano."""
    ent = catalogo_total().get(nombre, {})
    filas = []
    if "bcra" in ent:
        v = ent["bcra"].get("variable") or {}
        try:
            base, lista = bcra_listar_variables()
            for x in lista:
                if _coincide(x.get("descripcion", ""), v.get("incluir", ()), (), ()):
                    filas.append({"id": x.get("idVariable"), "Título": str(x.get("descripcion"))[:110],
                                  "Categoría": x.get("categoria"), "Periodicidad": x.get("periodicidad"),
                                  "Hasta": x.get("ultFechaInformada"), "Fuente": "BCRA"})
        except Exception as e:
            filas.append({"id": "", "Título": f"Error al listar variables del BCRA: {str(e)[:150]}"})
        return pd.DataFrame(filas).head(limite)
    cfg = ent.get("indec")
    if not cfg:
        return pd.DataFrame()
    vistos = set()
    for q in cfg.get("busquedas", ()):
        try:
            res = buscar_series(q, limite)
        except Exception as e:
            filas.append({"id": "", "Título": f"Error en la búsqueda '{q}': {str(e)[:150]}"})
            continue
        for r in res:
            if r["id"] in vistos:
                continue
            vistos.add(r["id"])
            ok = _coincide(f"{r['titulo']} {r['corto']} {r['dataset']} {r.get('unidad') or ''}",
                           cfg.get("incluir", ()), cfg.get("incluir_alguno", ()), cfg.get("excluir", ()))
            filas.append({"id": r["id"], "Título": str(r["titulo"])[:110], "Unidad": r.get("unidad"),
                          "Desde": r.get("desde"), "Hasta": r.get("hasta"), "Fuente": str(r.get("fuente"))[:35],
                          "Pasa filtros": ok})
    df = pd.DataFrame(filas)
    return df.sort_values("Hasta", ascending=False) if "Hasta" in df.columns else df


# ----------------------------------------------------------------------------
# Verificación (para chequear a mano que las series son las correctas)
# ----------------------------------------------------------------------------
# Inflación diciembre a diciembre publicada por el INDEC (IPC nacional)
INFLACION_OFICIAL = {2017: 24.8, 2018: 47.6, 2019: 53.8, 2020: 36.1, 2021: 50.9, 2022: 94.8, 2023: 211.4, 2024: 117.8}


def chequeos_inflacion(tolerancia: float = 0.3) -> pd.DataFrame:
    ipc_df, _ = cargar(NOM_IPC)
    calc = inflacion_dic_dic(ipc_df["valor"])
    filas = []
    for anio, oficial in INFLACION_OFICIAL.items():
        if anio in calc.index:
            c = round(float(calc[anio]), 1)
            filas.append({"Año": anio, "Calculada con la API (%)": c, "Publicada por INDEC (%)": oficial,
                          "Diferencia (pp)": round(c - oficial, 1), "Coincide": abs(c - oficial) <= tolerancia})
        else:
            filas.append({"Año": anio, "Calculada con la API (%)": None, "Publicada por INDEC (%)": oficial,
                          "Diferencia (pp)": None, "Coincide": False})
    return pd.DataFrame(filas)


def verificar_fuentes(desde: str = FECHA_INICIO_DEFECTO) -> pd.DataFrame:
    """Tabla con la serie realmente usada para cada indicador, su último dato y su estado."""
    filas = []
    for nombre in catalogo_total():
        try:
            df, i = cargar(nombre, desde)
            estado = "DESACTUALIZADA" if i.get("desactualizada") else "REVISAR" if i.get("revisar") else "OK"
            filas.append({"Indicador": nombre, "Estado": estado, "Fuente": i.get("fuente", ""), "API": i.get("api", ""),
                          "Serie usada": i.get("id", ""), "Título oficial": str(i.get("titulo", ""))[:90],
                          "Unidad": i.get("unidad", ""), "Frecuencia": i.get("frecuencia", ""),
                          "Primer dato": str(df.index.min().date()), "Último dato": i.get("ultimo", ""),
                          "Atraso (días)": i.get("atraso_dias"), "Notas": " | ".join(i.get("notas", []))[:300]})
        except Exception as e:
            filas.append({"Indicador": nombre, "Estado": "ERROR", "Notas": str(e)[:300]})
    return pd.DataFrame(filas)
