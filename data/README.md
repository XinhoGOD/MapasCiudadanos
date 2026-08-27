# Cartografía oficial de Hidalgo

El backend conserva únicamente datos derivados de fuentes oficiales de INEGI para Hidalgo (`CVE_ENT=13`):

- `geography.catalog.geojson`: 5,291 referencias puntuales del Catálogo Único AGEEML, corte identificado por el archivo fuente como 2025-11.
- `geography/hidalgo/<CVE_MUN>.geojson`: un archivo por cada uno de los 84 municipios, con el límite municipal y los polígonos publicados de localidades amanzanadas y asentamientos/colonias DCAH.
- `inegi_hidalgo_manifest.json`: URLs de origen, conteos, política geométrica y huellas SHA-256.

La caché y el `feature_collection` oficial sólo contienen `Polygon` y `MultiPolygon` publicados por INEGI. Las referencias puntuales nunca se guardan ni se presentan como límites oficiales.

La UI puede construir en memoria una vista opcional de cobertura completa mediante vecino más cercano (Voronoi), recortada al límite municipal. Esa capa usa los sitios oficiales como anclas y los conteos observados del Excel, pero se identifica expresamente como **influencia analítica**, no como cartografía oficial; no modifica los archivos de esta carpeta.

La cartografía oficial de localidades no cubre necesariamente toda la superficie municipal como una partición continua. En la vista oficial, los huecos no significan frecuencia cero: significan que INEGI no publicó ahí una unidad poligonal compatible con el nombre del Excel.

Fuentes: [Catálogo Único AGEEML](https://www.inegi.org.mx/app/ageeml/), [Marco Geoestadístico](https://www.inegi.org.mx/temas/mg/), [Servicio Web del Catálogo Único](https://www.inegi.org.mx/servicios/catalogounico.html) y DCAH Hidalgo (URL registrada en el manifiesto).
