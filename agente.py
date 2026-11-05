"""Agente de IA (Google Gemini, capa gratuita) que responde preguntas sobre los indicadores.

El modelo NO inventa números: llama a funciones que calculan con pandas sobre los
datos descargados de la API oficial, y luego redacta la respuesta.
Si un modelo está saturado (503) o no existe (404), reintenta y prueba con otro.
"""
import time
from datetime import date

import pandas as pd

from datos import agregar_anual, transformar

MAX_FILAS = 80
TRANSITORIOS = ("503", "UNAVAILABLE", "429", "RESOURCE_EXHAUSTED", "500", "INTERNAL", "DEADLINE")
EXCLUIR = ("image", "tts", "live", "audio", "embedding", "robotics", "computer-use", "thinking-exp")

_CTX: dict = {}
LLAMADAS: list = []


# ----------------------------------------------------------------------------
# Cálculos (los hace pandas, no el modelo)
# ----------------------------------------------------------------------------
def _resolver(ctx, nombre: str) -> str:
    """Acepta el nombre aunque el modelo lo escriba distinto (mayúsculas, parte del nombre)."""
    nombres = ctx["nombres"]
    if nombre in nombres:
        return nombre
    n = str(nombre).strip().lower()
    for x in nombres:
        if x.lower() == n:
            return x
    cand = [x for x in nombres if n in x.lower() or x.lower() in n]
    if len(cand) == 1:
        return cand[0]
    raise ValueError(f"Indicador '{nombre}' no reconocido. Disponibles: {'; '.join(nombres)}")


def _serie(ctx, nombre: str, trans: str, desde=None, hasta=None) -> pd.Series:
    df, _ = ctx["cargar"](_resolver(ctx, nombre))
    s = transformar(df["valor"], trans)
    if desde:
        s = s[s.index >= pd.Timestamp(desde)]
    if hasta:
        s = s[s.index <= pd.Timestamp(hasta)]
    return s.dropna()


def _fmt_idx(i) -> str:
    return str(i.date()) if hasattr(i, "date") else str(i)


def _filas(s: pd.Series) -> list:
    return [{"periodo": _fmt_idx(i), "valor": round(float(v), 4)} for i, v in s.items()]


def ejecutar(nombre_tool: str, a: dict, ctx: dict):
    if nombre_tool == "listar_indicadores":
        salida = []
        for n in ctx["nombres"]:
            try:
                df, info = ctx["cargar"](n)
                salida.append({"indicador": n, "unidad": info.get("unidad"), "frecuencia": info.get("frecuencia"),
                               "desde": _fmt_idx(df.index.min()), "hasta": _fmt_idx(df.index.max())})
            except Exception as e:
                salida.append({"indicador": n, "error": str(e)[:120]})
        return salida

    trans = a.get("transformacion", "nivel")

    if nombre_tool == "consultar_serie":
        s = agregar_anual(_serie(ctx, a["indicador"], trans, a.get("desde"), a.get("hasta")),
                          a.get("agregacion", "ninguna"))
        return {"total_filas": len(s), "mostrando_ultimas": min(len(s), MAX_FILAS), "datos": _filas(s.tail(MAX_FILAS))}

    if nombre_tool == "estadisticas":
        s = _serie(ctx, a["indicador"], trans, a.get("desde"), a.get("hasta"))
        if s.empty:
            return {"error": "Sin datos en ese rango."}
        nivel = _serie(ctx, a["indicador"], "nivel", a.get("desde"), a.get("hasta"))
        out = {
            "observaciones": len(s),
            "primera": {"fecha": _fmt_idx(s.index[0]), "valor": round(float(s.iloc[0]), 4)},
            "ultima": {"fecha": _fmt_idx(s.index[-1]), "valor": round(float(s.iloc[-1]), 4)},
            "media": round(float(s.mean()), 4), "mediana": round(float(s.median()), 4),
            "maximo": {"fecha": _fmt_idx(s.idxmax()), "valor": round(float(s.max()), 4)},
            "minimo": {"fecha": _fmt_idx(s.idxmin()), "valor": round(float(s.min()), 4)},
        }
        if len(nivel) > 1 and nivel.iloc[0] != 0:
            out["variacion_acumulada_pct_del_nivel"] = round(float(nivel.iloc[-1] / nivel.iloc[0] - 1) * 100, 4)
            out["variacion_promedio_por_periodo_pct"] = round(float(nivel.pct_change().mean() * 100), 6)
        return out

    if nombre_tool == "ranking":
        s = agregar_anual(_serie(ctx, a["indicador"], trans, a.get("desde"), a.get("hasta")),
                          a.get("agregacion", "ninguna"))
        s = s.sort_values(ascending=(a.get("orden", "desc") == "asc")).head(int(a.get("n", 5)))
        return _filas(s)

    if nombre_tool == "comparar":
        tabla = {}
        for n in a["indicadores"]:
            s = agregar_anual(_serie(ctx, n, trans), a.get("agregacion", "suma_anual"))
            tabla[n] = {int(y): (round(float(s[y]), 4) if y in s.index else None) for y in a["anios"]}
        return tabla

    return {"error": f"Herramienta desconocida: {nombre_tool}"}


# ----------------------------------------------------------------------------
# Herramientas que Gemini puede llamar (lee los tipos y la descripción de cada función)
# ----------------------------------------------------------------------------
def _correr(nombre: str, **args) -> dict:
    args = {k: v for k, v in args.items() if v not in ("", None)}
    LLAMADAS.append({"herramienta": nombre, "argumentos": args})
    try:
        res = ejecutar(nombre, args, _CTX)
    except Exception as e:
        return {"error": str(e)}
    return res if isinstance(res, dict) else {"resultado": res}


def listar_indicadores() -> dict:
    """Lista los indicadores disponibles con unidad, frecuencia y rango de fechas."""
    return _correr("listar_indicadores")


def consultar_serie(indicador: str, transformacion: str = "nivel", agregacion: str = "ninguna",
                    desde: str = "", hasta: str = "") -> dict:
    """Devuelve valores de un indicador (máx. 80 filas, las más recientes).
    indicador: nombre exacto del indicador (usar listar_indicadores si hay dudas).
    transformacion: 'nivel', 'var_periodo' (% vs período anterior) o 'var_interanual' (% vs mismo período del año anterior).
    agregacion: 'ninguna', 'promedio_anual', 'fin_anual', 'suma_anual' o 'max_anual'.
    Inflación anual = var_interanual + fin_anual. desde, hasta: fechas YYYY-MM-DD opcionales."""
    return _correr("consultar_serie", indicador=indicador, transformacion=transformacion,
                   agregacion=agregacion, desde=desde, hasta=hasta)


def estadisticas(indicador: str, transformacion: str = "nivel", desde: str = "", hasta: str = "") -> dict:
    """Primer y último valor, media, mediana, máximo, mínimo (con fechas) y variación promedio por período
    de un indicador en un rango. transformacion: 'nivel', 'var_periodo' o 'var_interanual'.
    desde, hasta: fechas YYYY-MM-DD opcionales."""
    return _correr("estadisticas", indicador=indicador, transformacion=transformacion, desde=desde, hasta=hasta)


def ranking(indicador: str, transformacion: str = "nivel", agregacion: str = "ninguna", n: int = 5,
            orden: str = "desc", desde: str = "", hasta: str = "") -> dict:
    """Ordena períodos o años por valor. Sirve para 'año con mayor o menor X'.
    orden: 'desc' (mayor primero) o 'asc'.
    Para inflación anual usar transformacion='var_interanual' y agregacion='fin_anual'."""
    return _correr("ranking", indicador=indicador, transformacion=transformacion, agregacion=agregacion,
                   n=n, orden=orden, desde=desde, hasta=hasta)


def comparar(indicadores: list[str], anios: list[int], transformacion: str = "nivel",
             agregacion: str = "suma_anual") -> dict:
    """Tabla de uno o más indicadores para varios años (ej. exportaciones vs importaciones).
    agregacion: 'suma_anual', 'promedio_anual', 'fin_anual' o 'max_anual'."""
    return _correr("comparar", indicadores=indicadores, anios=anios, transformacion=transformacion,
                   agregacion=agregacion)


def sistema(nombres: list) -> str:
    return (
        "Sos un asistente de economía argentina dentro de una app de indicadores macroeconómicos. "
        f"Hoy es {date.today().isoformat()}. Respondé en español, de forma clara y breve. "
        "Todos los números deben salir de las herramientas (datos oficiales de INDEC, BCRA, etc. vía la API de "
        "Series de Tiempo de datos.gob.ar); no uses cifras de memoria. Indicá siempre unidad, período y fuente. "
        "Si un indicador no está disponible o el dato no alcanza para responder, decilo. "
        "Aclará el criterio cuando haya ambigüedad (p. ej. inflación anual = diciembre a diciembre). "
        "Indicadores disponibles: " + "; ".join(nombres) + "."
    )


def _modelos(client) -> list:
    """Modelos 'flash' disponibles hoy (los nombres cambian seguido), más nuevos primero."""
    try:
        nombres = {m.name.replace("models/", "") for m in client.models.list()
                   if "flash" in m.name and "generateContent" in (getattr(m, "supported_actions", None) or [])
                   and not any(x in m.name for x in EXCLUIR)}
        if nombres:
            return sorted(nombres, reverse=True)[:4]
    except Exception:
        pass
    return ["gemini-flash-latest", "gemini-2.5-flash"]


def preguntar(api_key: str, pregunta: str, historial: list, nombres: list, cargar_fn):
    """Devuelve (respuesta, llamadas_realizadas)."""
    from google import genai
    from google.genai import types

    client = genai.Client(api_key=api_key)
    _CTX.clear()
    _CTX.update({"cargar": cargar_fn, "nombres": nombres})

    contenidos = [types.Content(role=("user" if m["role"] == "user" else "model"),
                                parts=[types.Part(text=m["content"])]) for m in historial]
    contenidos.append(types.Content(role="user", parts=[types.Part(text=pregunta)]))
    cfg = types.GenerateContentConfig(
        system_instruction=sistema(nombres), temperature=0,
        tools=[listar_indicadores, consultar_serie, estadisticas, ranking, comparar])

    ultimo = None
    for modelo in _modelos(client):
        for intento in range(3):
            LLAMADAS.clear()
            try:
                resp = client.models.generate_content(model=modelo, contents=contenidos, config=cfg)
                return (resp.text or "No pude generar una respuesta."), list(LLAMADAS)
            except Exception as e:
                ultimo = e
                txt = str(e)
                if "API_KEY_INVALID" in txt or "API key not valid" in txt:
                    raise RuntimeError("La clave de API no es válida. Revisá que la copiaste completa.")
                if any(c in txt for c in TRANSITORIOS):
                    time.sleep(3 * (intento + 1))   # servicio saturado: esperar y reintentar
                    continue
                break                                  # p. ej. 404: probar con el siguiente modelo
    raise RuntimeError(f"Gemini no respondió después de varios intentos. Probá de nuevo en un minuto. Último error: {ultimo}")
