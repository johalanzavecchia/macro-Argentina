"""Agente de IA que responde preguntas sobre los indicadores usando herramientas.

El modelo NO inventa números: llama a funciones que calculan con pandas sobre los
datos descargados de la API oficial, y luego redacta la respuesta.
"""
from __future__ import annotations

import json
from datetime import date

import pandas as pd

from datos import agregar_anual, transformar

MODELO = "claude-sonnet-5-5"
MAX_FILAS = 80

TRANS = ["nivel", "var_periodo", "var_interanual"]
AGREG = ["ninguna", "promedio_anual", "fin_anual", "suma_anual", "max_anual"]


def herramientas(nombres: list[str]) -> list[dict]:
    ind = {"type": "string", "enum": nombres, "description": "Nombre exacto del indicador."}
    trans = {"type": "string", "enum": TRANS, "default": "nivel",
             "description": "nivel = valor tal cual; var_periodo = variación % vs. período anterior; "
                            "var_interanual = variación % vs. mismo período del año anterior."}
    agreg = {"type": "string", "enum": AGREG, "default": "ninguna",
             "description": "Agregación anual aplicada DESPUÉS de la transformación. Para la inflación anual "
                            "usar var_interanual + fin_anual (variación diciembre a diciembre)."}
    fechas = {
        "desde": {"type": "string", "description": "YYYY-MM-DD (opcional)."},
        "hasta": {"type": "string", "description": "YYYY-MM-DD (opcional)."},
    }
    return [
        {"name": "listar_indicadores",
         "description": "Lista indicadores disponibles con unidad, frecuencia y rango de fechas.",
         "input_schema": {"type": "object", "properties": {}}},
        {"name": "consultar_serie",
         "description": f"Devuelve valores de un indicador (máx. {MAX_FILAS} filas, las más recientes).",
         "input_schema": {"type": "object", "properties": {
             "indicador": ind, "transformacion": trans, "agregacion": agreg, **fechas},
             "required": ["indicador"]}},
        {"name": "estadisticas",
         "description": "Estadísticas de un indicador en un rango: primer/último valor, variación acumulada, "
                        "media, mediana, máximo y mínimo con fechas, y variación promedio por período.",
         "input_schema": {"type": "object", "properties": {
             "indicador": ind, "transformacion": trans, **fechas}, "required": ["indicador"]}},
        {"name": "ranking",
         "description": "Ordena períodos (o años si hay agregación) por valor. Sirve para 'año con mayor/menor X'.",
         "input_schema": {"type": "object", "properties": {
             "indicador": ind, "transformacion": trans, "agregacion": agreg, **fechas,
             "n": {"type": "integer", "default": 5},
             "orden": {"type": "string", "enum": ["desc", "asc"], "default": "desc"}},
             "required": ["indicador"]}},
        {"name": "comparar",
         "description": "Tabla de uno o más indicadores para varios años (ej. exportaciones vs importaciones).",
         "input_schema": {"type": "object", "properties": {
             "indicadores": {"type": "array", "items": ind},
             "anios": {"type": "array", "items": {"type": "integer"}},
             "transformacion": trans,
             "agregacion": {**agreg, "default": "suma_anual"}},
             "required": ["indicadores", "anios"]}},
    ]


def _serie(ctx, nombre: str, trans: str, desde=None, hasta=None) -> pd.Series:
    df, _ = ctx["cargar"](nombre)
    s = transformar(df["valor"], trans)
    if desde:
        s = s[s.index >= pd.Timestamp(desde)]
    if hasta:
        s = s[s.index <= pd.Timestamp(hasta)]
    return s.dropna()


def _fmt_idx(i) -> str:
    return str(i.date()) if hasattr(i, "date") else str(i)


def _filas(s: pd.Series) -> list[dict]:
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


def sistema(nombres: list[str]) -> str:
    return (
        "Sos un asistente de economía argentina dentro de una app de indicadores macroeconómicos. "
        f"Hoy es {date.today().isoformat()}. Respondé en español, de forma clara y breve. "
        "Todos los números deben salir de las herramientas (datos oficiales de INDEC, BCRA, etc. vía la API de "
        "Series de Tiempo de datos.gob.ar); no uses cifras de memoria. Indicá siempre unidad, período y fuente. "
        "Si un indicador no está disponible o el dato no alcanza para responder, decilo. "
        "Aclará el criterio cuando haya ambigüedad (p. ej. inflación anual = diciembre a diciembre). "
        "Indicadores disponibles: " + "; ".join(nombres) + "."
    )


def preguntar(api_key: str, pregunta: str, historial: list[dict], nombres: list[str], cargar_fn) -> tuple[str, list[dict]]:
    """Corre el bucle de herramientas. Devuelve (respuesta, llamadas_realizadas)."""
    import anthropic

    client = anthropic.Anthropic(api_key=api_key)
    ctx = {"cargar": cargar_fn, "nombres": nombres}
    mensajes = [*historial, {"role": "user", "content": pregunta}]
    llamadas: list[dict] = []

    for _ in range(8):
        resp = client.messages.create(model=MODELO, max_tokens=1500, system=sistema(nombres),
                                      tools=herramientas(nombres), messages=mensajes)
        if resp.stop_reason != "tool_use":
            texto = "".join(b.text for b in resp.content if b.type == "text").strip()
            return texto or "No pude generar una respuesta.", llamadas

        mensajes.append({"role": "assistant", "content": resp.content})
        resultados = []
        for b in resp.content:
            if b.type != "tool_use":
                continue
            try:
                res = ejecutar(b.name, b.input, ctx)
                contenido, error = json.dumps(res, ensure_ascii=False), False
            except Exception as e:
                contenido, error = f"Error: {e}", True
            llamadas.append({"herramienta": b.name, "argumentos": b.input})
            resultados.append({"type": "tool_result", "tool_use_id": b.id, "content": contenido, "is_error": error})
        mensajes.append({"role": "user", "content": resultados})

    return "La consulta requirió demasiados pasos. Probá con una pregunta más acotada.", llamadas
