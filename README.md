# Mapa de Participación Ciudadana

Aplicación orientada a ChatGPT para convertir resultados de participación ciudadana en mapas territoriales agregados. El núcleo procesa `.xlsx`, `.xls` (si el motor de lectura está instalado) y `.csv`; no muestra datos individuales ni envía campos personales al modelo.

## Arquitectura

```text
ChatGPT / Developer Mode
        │ MCP Apps (Streamable HTTP)
        ▼
MCP server (/mcp)
        ├── tools de producto: inspección, opciones, matching y mapas
        ├── servicios de encuesta y analítica
        ├── GeographyRepository (catálogo INEGI-derived configurable)
        └── recurso UI MCP Apps: ui://participation-map/v1.html
```

La UI usa el estándar MCP Apps (`text/html;profile=mcp-app`, recurso enlazado desde `_meta.ui.resourceUri`) y el puente `ui/*`. Las extensiones `window.openai` se detectan como mejora opcional para seleccionar archivos. El servidor permanece útil sin UI y devuelve resultados estructurados con GeoJSON agregado.

## Instalación

Requiere Python 3.12+.

```powershell
py -3.13 -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -r requirements.txt
Copy-Item .env.example .env
```

La aplicación ya incluye en el backend el catálogo oficial de Hidalgo en `data/geography.catalog.geojson` y los archivos municipales en `data/geography/hidalgo/<CVE_MUN>.geojson`. `GEOGRAPHY_CATALOG_PATH` sólo se necesita para pruebas con otro catálogo revisado.

## Geometría territorial y cobertura

El backend conserva únicamente cartografía oficial: el límite del municipio seleccionado, polígonos de localidades amanzanadas del servicio vectorial de INEGI y polígonos de colonias/asentamientos de la DCAH. El motor carga sólo el municipio solicitado.

La interfaz ofrece dos vistas explícitas:

- **Sólo límites oficiales (predeterminada):** colorea únicamente los polígonos publicados por INEGI que tienen localidades/colonias ubicadas y datos en el Excel; las áreas sin construcciones o sin datos permanecen sin color.
- **Cobertura completa · influencia analítica:** divide el límite municipal mediante vecino más cercano (Voronoi) a partir de coordenadas oficiales o centroides de polígonos oficiales. Esta vista aplica la Primera Ley de la Geografía de Tobler y cubre todo el municipio, pero sus divisiones no se presentan como límites oficiales.

En ambas vistas los conteos proceden exclusivamente del Excel y se agrupan por localidad/colonia. No se inventan respuestas ni se transfieren conteos de una localidad a otra.

La caché validada contiene 84 municipios, 5,291 referencias puntuales de localidad, 2,534 polígonos de localidad y 3,843 polígonos de asentamiento. La procedencia, las URLs, los SHA-256 de los archivos fuente y los conteos están en `data/inegi_hidalgo_manifest.json`.

### Contexto visual OpenStreetMap

El resultado incluye una capa opcional de contexto vial de OpenStreetMap detrás de la cartografía analítica. La capa muestra calles y caminos para facilitar la lectura del entorno, pero no modifica límites, nombres, conteos ni la vinculación oficial de INEGI/DCAH. El botón **Ocultar contexto OSM / Mostrar contexto OSM** permite alternarla. La vista conserva atribución visible a OpenStreetMap y requiere conexión a internet para cargar sus teselas; si no están disponibles, la cartografía oficial sigue funcionando.

Para reconstruir la cache completa de Hidalgo:

```powershell
py -3.13 scripts/build_hidalgo_geography.py --settlement-zip work/inegi-dcah-hidalgo.zip
py -3.13 scripts/validate_hidalgo_geography.py
```

INEGI publica las localidades rurales no amanzanadas como puntos, no como polígonos; además, la DCAH no tiene cobertura integral. Por eso la vista oficial no forma un mosaico continuo de todo el municipio. Una localidad puntual puede participar en la vista analítica usando su coordenada oficial, pero nunca se etiqueta como polígono oficial.

## Ejecutar localmente

```powershell
python -m app.mcp_server
```

El endpoint queda en `http://127.0.0.1:8000/mcp`. Para inspeccionar las tools:

```powershell
npx @modelcontextprotocol/inspector@latest
```

Selecciona **Streamable HTTP** y usa `http://127.0.0.1:8000/mcp`.

### Probar sin Developer Mode

Para ver cómo se colorea el mapa y cargar un Excel directamente desde el navegador:

```powershell
py -3.13 scripts/preview_map.py
```

Abre `http://127.0.0.1:8765/`. La interfaz institucional de la Dirección de Participación Ciudadana siempre inicia vacía: no carga un municipio ni un archivo de demostración. Pulsa **Seleccionar Excel o CSV**, elige cualquier base compatible y revisa el municipio y la pregunta detectados. Antes de generar, la app resume cuántos nombres tienen polígono oficial, cuántos sólo cuentan con sitio puntual y cuántos requieren revisión. **Generar mapa** usa siempre **Todas las respuestas · composición (%)**: cada localidad/colonia conserva todas las categorías de la pregunta dentro de su territorio y el ancho de cada franja representa su porcentaje local; la opacidad indica el volumen de participaciones. La interfaz ya no permite seleccionar una sola respuesta ni cambiar a frecuencia absoluta, porque las respuestas son la simbología del mapa. El selector del mapa alterna entre cobertura municipal completa mediante zonas analíticas de influencia y los límites oficiales disponibles. Esta vista procesa el archivo en tu computadora; la conexión con ChatGPT Developer Mode sigue siendo opcional.

Las pruebas unitarias:

```powershell
py -3.13 -m pytest
```

Para volver a descargar, construir y validar exclusivamente Hidalgo desde INEGI:

```powershell
.\scripts\refresh_hidalgo_catalog.ps1
```

## Conectar a ChatGPT Developer Mode

1. Expón el endpoint con HTTPS público o un Secure MCP Tunnel.
2. En ChatGPT abre Settings → Security and login → Developer mode.
3. En ChatGPT Plugins agrega una conexión MCP e introduce la URL terminada en `/mcp`.
4. Revisa las tools descubiertas y prueba una conversación con un Excel adjunto.

El primer flujo recomendado es: `inspect_survey_file` → `get_question_options` → `generate_composition_map`. Esta última agrupa todas las respuestas de la pregunta por localidad/colonia y calcula el porcentaje dentro de cada territorio. `generate_frequency_map` queda para estudiar una respuesta individual y `generate_dominant_answer_map` para la respuesta predominante. Cuando una pregunta tenga ambigüedad real, se devuelve una selección concreta en vez de adivinar.

## Mapas públicos desde Google Sheets o Excel

Esta copia agrega un flujo web independiente del inspector MCP. Permite pegar el enlace de una hoja pública de Google Sheets o cargar un `.xlsx`, `.xls` o `.csv`; el backend genera un mapa de respuesta predominante con la cartografía de Hidalgo ya incluida y devuelve un enlace `/maps/<id>`.

```powershell
py -3.13 scripts/hosted_map_server.py --host 127.0.0.1 --port 8770
```

Abre `http://127.0.0.1:8770/`. Al pegar la URL, la pantalla consulta el libro y muestra el selector **Elige la hoja de tu Google Sheets**; después llena automáticamente los municipios y preguntas de la pestaña elegida. Si sólo hay una hoja, queda seleccionada automáticamente. El mapa público muestra sólo la agregación territorial: cada localidad/colonia se pinta con el color de la respuesta más frecuente, coloca un punto y etiqueta su nombre, y muestra un relieve topográfico descargado y cacheado para el mapa. No expone el Excel, las filas ni los datos individuales.

Cuando la fuente es Google Sheets, el servidor revisa la hoja periódicamente (60 segundos por defecto), compara su huella de contenido y conserva el mapa anterior si una actualización falla. No usa Apps Script: consume el export CSV HTTPS de una hoja con permiso de lectura público. La URL local sólo es visible en tu computadora; para compartirla por internet configura una dirección HTTPS en `PUBLIC_BASE_URL` y despliega este servidor en un host que ejecute Python.

Variables opcionales:

```text
PUBLIC_BASE_URL=https://mapas.ejemplo.gob.mx
HOSTED_MAPS_DIR=work/hosted-maps
SHEET_REFRESH_SECONDS=60
BLOB_READ_WRITE_TOKEN=vercel_blob_rw_...
```

En local, los mapas se guardan como JSON en `work/hosted-maps`. En Vercel, crea y conecta un almacén **Vercel Blob** de acceso privado al proyecto y agrega `BLOB_READ_WRITE_TOKEN` en Project Settings → Environment Variables; cuando esa variable existe, los snapshots y relieves se guardan en Blob privado y el servidor los entrega mediante las URLs públicas de cada mapa. Esto evita perder los enlaces al reiniciar una función o hacer un nuevo despliegue. Si Vercel no tiene esa variable, la API detiene la creación para no volver a generar enlaces efímeros. La vista previa es un SVG estático generado desde la agregación territorial, sin publicar el archivo original.

## Identidad institucional

La interfaz pública usa el logotipo de Planeación proporcionado para este proyecto y conserva los criterios visuales documentados en el Manual de Identidad Institucional del Gobierno de Hidalgo 2022–2028: guinda y dorado, composición horizontal, lenguaje sobrio y tipografías GMX/Montserrat como referencia. Los materiales descargados se conservan en `ui/assets/identity/`. La página oficial de la Unidad de Planeación y Prospectiva es `https://u-planeacion.hidalgo.gob.mx/` y los logotipos estatales de respaldo provienen del CDN oficial `https://cdn.hidalgo.gob.mx/`.

## Seguridad y privacidad

- Extensiones y tamaño de archivo se validan antes de leer.
- No se ejecutan fórmulas del Excel; sólo se leen los valores calculados guardados en el archivo y se muestra una advertencia si hay fórmulas.
- Las coordenadas solo provienen del catálogo configurado.
- Los matches `FUZZY_REVIEW` y `UNMATCHED` no se dibujan automáticamente.
- Las localidades que sólo tienen coordenada puntual no se convierten en polígonos oficiales; su zona de influencia, cuando se muestra, queda identificada como analítica.
- La salida contiene agregaciones por localidad, nunca filas individuales.
- La normalización conserva el texto original para mostrarlo y solo usa una copia normalizada para matching.

## Cobertura incluida

La configuración está limitada deliberadamente a Hidalgo y no mezcla entidades. Para ampliar a otro estado habría que construir y validar otra caché explícitamente. La vista oficial depende de que la localidad o colonia del Excel tenga un polígono publicado; la cobertura analítica depende de que tenga una coordenada o polígono oficial confiable.

## Fuentes técnicas

La integración sigue la documentación oficial vigente de OpenAI sobre [MCP server](https://developers.openai.com/plugins/build/mcp-server), [UI de ChatGPT/MCP Apps](https://developers.openai.com/plugins/build/chatgpt-ui) y [conexión en Developer Mode](https://developers.openai.com/plugins/deploy/connect-chatgpt).
