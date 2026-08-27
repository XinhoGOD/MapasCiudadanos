"""Serve the Hidalgo map UI locally with Excel/CSV upload support."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from starlette.applications import Starlette
from starlette.datastructures import UploadFile
from starlette.responses import HTMLResponse, JSONResponse
from starlette.routing import Route

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from app.analytics.engine import (  # noqa: E402
    generate_dominant_answer_map,
    generate_frequency_map,
    generate_composition_map,
    generate_participation_map,
    get_question_options,
    inspect_dataset,
    resolve_geography,
)
def local_bridge_script() -> str:
    """Expose the same small file/tool bridge that Developer Mode provides."""
    return r"""
<script>
window.localPreview = (() => {
  let input;
  const chooseFile = () => new Promise(resolve => {
    if (!input) {
      input = document.createElement('input');
      input.type = 'file';
      input.accept = '.xlsx,.xls,.csv,text/csv,application/vnd.openxmlformats-officedocument.spreadsheetml.sheet,application/vnd.ms-excel';
      input.style.display = 'none';
      document.body.appendChild(input);
    }
    input.value = '';
    input.onchange = () => resolve(input.files && input.files.length ? [input.files[0]] : []);
    input.click();
  });
    const callTool = async (name, args = {}) => {
    const body = new FormData();
    if (args.file) body.append('file', args.file, args.file.name || 'encuesta.xlsx');
    for (const key of ['municipality', 'question', 'answer', 'intent']) {
      if (args[key] !== undefined && args[key] !== null && args[key] !== '') body.append(key, args[key]);
    }
    const response = await fetch('/api/tool/' + encodeURIComponent(name), {method: 'POST', body});
    const data = await response.json();
    if (!response.ok) throw new Error(data.detail || data.error || 'No se pudo procesar el archivo.');
    return data;
  };
  return {selectFiles: chooseFile, callTool};
})();
window.openai = {toolOutput: null, selectFiles: window.localPreview.selectFiles, callTool: window.localPreview.callTool};
</script>
"""


def build_page() -> str:
    """Build the upload-first frontend with no demonstration dataset preloaded."""
    ui = (ROOT / "ui" / "map.html").read_text(encoding="utf-8")
    payload = json.dumps(None)
    marker = "<script>\n(() => {"
    injected = local_bridge_script().rstrip() + "\n<script>\nwindow.openai.toolOutput = " + payload + ";\n(() => {"
    if marker not in ui:
        raise RuntimeError("No se encontró el punto de montaje de la UI")
    return ui.replace(marker, injected, 1)


async def save_upload(upload: UploadFile) -> Path:
    """Save the browser upload in a task-owned temporary directory."""
    upload_dir = ROOT / "work" / "local-uploads"
    upload_dir.mkdir(parents=True, exist_ok=True)
    suffix = Path(upload.filename or "encuesta.xlsx").suffix.lower()
    if suffix not in {".xlsx", ".xls", ".csv"}:
        raise ValueError("El archivo debe ser .xlsx, .xls o .csv.")
    destination = upload_dir / ("current-upload" + suffix)
    data = await upload.read()
    if len(data) > 50 * 1024 * 1024:
        raise ValueError("El archivo supera el límite local de 50 MB.")
    destination.write_bytes(data)
    return destination


async def api_tool(request):
    name = request.path_params["name"]
    if name not in {"inspect_survey_file", "resolve_geography", "get_question_options", "generate_frequency_map", "generate_composition_map", "generate_dominant_answer_map", "generate_participation_map"}:
        return JSONResponse({"error": "Herramienta local no disponible."}, status_code=404)
    try:
        form = await request.form()
        upload = form.get("file")
        if not isinstance(upload, UploadFile):
            raise ValueError("Selecciona primero un archivo Excel o CSV.")
        file_path = await save_upload(upload)
        if name == "inspect_survey_file":
            result = inspect_dataset(file_path=str(file_path))
        elif name == "resolve_geography":
            result = resolve_geography(
                file_path=str(file_path),
                municipality=str(form.get("municipality") or "") or None,
                question=str(form.get("question") or "") or None,
            )
        elif name == "get_question_options":
            result = get_question_options(
                file_path=str(file_path),
                question=str(form.get("question") or ""),
                municipality=str(form.get("municipality") or "") or None,
            )
        elif name == "generate_frequency_map":
            result = generate_frequency_map(
                file_path=str(file_path),
                municipality=str(form.get("municipality") or "") or None,
                question=str(form.get("question") or "") or None,
                answer=str(form.get("answer") or "") or None,
            )
        elif name == "generate_composition_map":
            result = generate_composition_map(
                file_path=str(file_path),
                municipality=str(form.get("municipality") or "") or None,
                question=str(form.get("question") or "") or None,
            )
        elif name == "generate_dominant_answer_map":
            result = generate_dominant_answer_map(
                file_path=str(file_path),
                municipality=str(form.get("municipality") or "") or None,
                question=str(form.get("question") or "") or None,
            )
        else:
            result = generate_participation_map(
                file_path=str(file_path),
                municipality=str(form.get("municipality") or "") or None,
                question=str(form.get("question") or "") or None,
            )
        return JSONResponse(result)
    except Exception as error:  # local UI needs a readable error response
        return JSONResponse({"error": str(error)}, status_code=400)


def make_app() -> Starlette:
    page = build_page()

    async def homepage(request):
        return HTMLResponse(page)

    return Starlette(
        debug=False,
        routes=[
            Route("/", homepage),
            Route("/index.html", homepage),
            Route("/api/tool/{name}", api_tool, methods=["POST"]),
        ],
    )


def main() -> None:
    parser = argparse.ArgumentParser(description="Vista local del mapa de Hidalgo")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8765)
    args = parser.parse_args()

    import uvicorn

    print(f"Vista local del mapa: http://{args.host}:{args.port}/", flush=True)
    uvicorn.run(make_app(), host=args.host, port=args.port, log_level="warning")


if __name__ == "__main__":
    main()
