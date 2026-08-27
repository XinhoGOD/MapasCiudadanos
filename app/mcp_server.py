from __future__ import annotations

import os
from pathlib import Path
from typing import Any

from mcp.server.mcpserver import MCPServer

from app.analytics import (
    analyze_spatial_distribution,
    create_map_from_intent,
    generate_dominant_answer_map,
    generate_frequency_map,
    generate_composition_map,
    generate_participation_map,
    get_question_options,
    inspect_dataset,
    resolve_geography,
)

UI_RESOURCE_URI = "ui://participation-map/v1.html"
UI_PATH = Path(__file__).resolve().parents[1] / "ui" / "map.html"

server = MCPServer(
    name="mapa-participacion-ciudadana",
    version="0.1.0",
    instructions=(
        "Analiza archivos Excel/CSV de participación ciudadana. Primero inspecciona el archivo, "
        "resuelve la pregunta y la respuesta, y después genera un mapa. No expongas datos personales; "
        "usa únicamente agregaciones territoriales. Si falta una columna o hay ambigüedad real, informa "
        "el problema en lenguaje claro y pide una selección concreta. Conserva los polígonos oficiales de INEGI "
        "para Hidalgo y distingue siempre esos límites de la cobertura analítica opcional por influencia geográfica."
    ),
)


@server.resource(UI_RESOURCE_URI, name="participation_map", mime_type="text/html;profile=mcp-app", meta={"ui": {"prefersBorder": True}})
def participation_map_resource() -> str:
    return UI_PATH.read_text(encoding="utf-8")


@server.tool(name="inspect_survey_file", description="Inspecciona un Excel/CSV, detecta hojas, columnas, municipio, localidad, preguntas, respuestas y advertencias.", structured_output=True)
def inspect_survey_file(file_path: str | None = None, file: dict[str, Any] | str | None = None) -> dict[str, Any]:
    return inspect_dataset(file_path=file_path, file=file)


@server.tool(name="resolve_geography", description="Valida localidades del archivo contra la cartografía configurada y separa polígonos oficiales, sitios puntuales, revisiones y nombres no identificados.", structured_output=True)
def resolve_geography_tool(file_path: str | None = None, file: dict[str, Any] | str | None = None, municipality: str | None = None, question: str | None = None, catalog_path: str | None = None) -> dict[str, Any]:
    return resolve_geography(file_path=file_path, file=file, municipality=municipality, question=question, catalog_path=catalog_path)


@server.tool(name="get_question_options", description="Devuelve las respuestas reales disponibles para una pregunta y municipio, con sus frecuencias.", structured_output=True)
def get_question_options_tool(file_path: str | None = None, file: dict[str, Any] | str | None = None, question: str | None = None, intent: str | None = None, municipality: str | None = None) -> dict[str, Any]:
    return get_question_options(file_path=file_path, file=file, question=question, intent=intent, municipality=municipality)


@server.tool(name="generate_frequency_map", description="Agrupa una respuesta por frecuencia en cada localidad/colonia y genera una coropleta municipal con vista oficial y cobertura analítica completa.", meta={"ui": {"resourceUri": UI_RESOURCE_URI}}, structured_output=True)
def generate_frequency_map_tool(file_path: str | None = None, file: dict[str, Any] | str | None = None, municipality: str | None = None, question: str | None = None, answer: str | None = None, catalog_path: str | None = None) -> dict[str, Any]:
    return generate_frequency_map(file_path=file_path, file=file, municipality=municipality, question=question, answer=answer, catalog_path=catalog_path)


@server.tool(name="generate_composition_map", description="Agrupa todas las respuestas de una pregunta por localidad/colonia y pinta cada territorio con su composición porcentual completa.", meta={"ui": {"resourceUri": UI_RESOURCE_URI}}, structured_output=True)
def generate_composition_map_tool(file_path: str | None = None, file: dict[str, Any] | str | None = None, municipality: str | None = None, question: str | None = None, catalog_path: str | None = None) -> dict[str, Any]:
    return generate_composition_map(file_path=file_path, file=file, municipality=municipality, question=question, catalog_path=catalog_path)


@server.tool(name="create_map_from_intent", description="Ruta principal: toma el Excel adjunto y la petición en lenguaje natural, detecta la pregunta/respuesta y crea el mapa de Hidalgo. Si existe ambigüedad real devuelve opciones concretas.", meta={"ui": {"resourceUri": UI_RESOURCE_URI}}, structured_output=True)
def create_map_from_intent_tool(file_path: str | None = None, file: dict[str, Any] | str | None = None, intent: str | None = None, municipality: str | None = None, question: str | None = None, answer: str | None = None, catalog_path: str | None = None) -> dict[str, Any]:
    return create_map_from_intent(file_path=file_path, file=file, intent=intent, municipality=municipality, question=question, answer=answer, catalog_path=catalog_path)


@server.tool(name="generate_dominant_answer_map", description="Agrupa todas las respuestas por localidad/colonia y genera un mapa con su mezcla completa, conteos y respuesta predominante.", meta={"ui": {"resourceUri": UI_RESOURCE_URI}}, structured_output=True)
def generate_dominant_answer_map_tool(file_path: str | None = None, file: dict[str, Any] | str | None = None, municipality: str | None = None, question: str | None = None, catalog_path: str | None = None) -> dict[str, Any]:
    return generate_dominant_answer_map(file_path=file_path, file=file, municipality=municipality, question=question, catalog_path=catalog_path)


@server.tool(name="generate_participation_map", description="Genera una coropleta municipal con el total de respuestas válidas por localidad, usando sólo polígonos oficiales.", meta={"ui": {"resourceUri": UI_RESOURCE_URI}}, structured_output=True)
def generate_participation_map_tool(file_path: str | None = None, file: dict[str, Any] | str | None = None, municipality: str | None = None, question: str | None = None, catalog_path: str | None = None) -> dict[str, Any]:
    return generate_participation_map(file_path=file_path, file=file, municipality=municipality, question=question, catalog_path=catalog_path)


@server.tool(name="analyze_spatial_distribution", description="Resume descriptivamente un mapa ya calculado, sin inventar causalidad.", structured_output=True)
def analyze_spatial_distribution_tool(map_result: dict[str, Any]) -> dict[str, Any]:
    return analyze_spatial_distribution(map_result)


if __name__ == "__main__":
    server.run(transport="streamable-http", host=os.getenv("MCP_HOST", "127.0.0.1"), port=int(os.getenv("MCP_PORT", "8000")))
