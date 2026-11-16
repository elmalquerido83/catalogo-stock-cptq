# Catálogo de contenidos

Buscador web de las fotos y vídeos de la carpeta de Dropbox
`/CPTQ/DESTINOS/2026 Stock Destinos Enlinea Media`. Las carpetas llamadas
**"03. Editado"** nunca entran al catálogo.

## Cómo funciona

- `indexador/indexar.py` revisa Dropbox, y para cada archivo nuevo o modificado:
  - vídeos: lee 4 fotogramas directamente desde Dropbox con `ffmpeg` (sin bajar el archivo completo);
  - fotos: usa la miniatura que genera Dropbox;
  - pide a Claude Haiku 5.5 una descripción y etiquetas en español;
  - crea o reutiliza un enlace público de Dropbox para ver y descargar;
  - guarda una miniatura en `sitio/minis/` y actualiza `sitio/datos/catalogo.json`.
  Los archivos borrados desaparecen del catálogo; los movidos o renombrados se actualizan sin volver a procesarse.
- `.github/workflows/sincronizar.yml` lo ejecuta cada 6 horas (o a mano desde la pestaña **Actions**)
  y publica la web en GitHub Pages.
- `sitio/` es la web estática: búsqueda en el navegador con [MiniSearch](https://github.com/lucaong/minisearch),
  filtros por tipo, destino, formato, toma y momento.

## Secretos (Settings → Secrets and variables → Actions)

| Nombre | Qué es |
|---|---|
| `DROPBOX_APP_KEY` | App key de la app de Dropbox |
| `DROPBOX_REFRESH_TOKEN` | Token obtenido con `conectar-dropbox.html` |
| `ANTHROPIC_API_KEY` | Clave de la API de Anthropic |

Variables opcionales (pestaña *Variables*): `MAX_USD` (tope de gasto de IA por ejecución, 15 por defecto),
`CARPETAS_EXCLUIDAS` (más carpetas a excluir, separadas por comas), `CARPETA_DROPBOX`.

## Ejecutar a mano

Actions → **Sincronizar catálogo** → **Run workflow**. En "Máximo de archivos" pon un número pequeño (p. ej. 20) para una prueba.
Los archivos que fallan quedan anotados en `indexador/errores.json` y se reintentan hasta 3 veces.
