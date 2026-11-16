"""Indexador del catálogo de Dropbox.

Recorre la carpeta de Dropbox, detecta archivos nuevos o cambiados y, para cada uno:
  1. Saca fotogramas (vídeo) o una miniatura (foto) sin descargar el archivo completo.
  2. Pide a Claude Haiku una descripción en español de lo que se ve.
  3. Crea (o reutiliza) un enlace público de Dropbox para descargarlo.
  4. Guarda una miniatura pequeña y actualiza sitio/datos/catalogo.json.

Se ejecuta en GitHub Actions (ver .github/workflows/sincronizar.yml).
Variables de entorno: ver README.md.
"""

from __future__ import annotations

import base64
import concurrent.futures as cf
import io
import json
import os
import re
import subprocess
import sys
import tempfile
import threading
import time
import unicodedata
from pathlib import Path

import anthropic
import dropbox
import requests
from dropbox import common, files, sharing
from PIL import Image, ImageOps

try:  # Soporte para fotos .heic de iPhone
    import pillow_heif

    pillow_heif.register_heif_opener()
except ImportError:  # pragma: no cover
    pass

# --------------------------------------------------------------------------- config
RAIZ = Path(__file__).resolve().parent.parent
SITIO = RAIZ / "sitio"
DATOS = SITIO / "datos"
MINIS = SITIO / "minis"
CATALOGO = DATOS / "catalogo.json"
ERRORES = RAIZ / "indexador" / "errores.json"

CARPETA = (os.environ.get("CARPETA_DROPBOX") or "/CPTQ/DESTINOS/2026 Stock Destinos Enlinea Media").rstrip("/")
# Carpetas que NUNCA entran al catálogo (comparación sin mayúsculas ni acentos).
# "03. Editado" se excluye SIEMPRE; la variable solo puede añadir más carpetas.
EXCLUIR = ["03. Editado"] + [s.strip() for s in (os.environ.get("CARPETAS_EXCLUIDAS") or "").split(",") if s.strip()]
MODELO = os.environ.get("MODELO_CLAUDE") or "claude-haiku-5-5"
MAX_ARCHIVOS = int(os.environ.get("MAX_ARCHIVOS", "0") or 0)  # 0 = sin límite
MAX_MINUTOS = float(os.environ.get("MAX_MINUTOS", "300") or 300)
MAX_USD = float(os.environ.get("MAX_USD", "15") or 15)  # tope de gasto por ejecución
HILOS = int(os.environ.get("HILOS", "6") or 6)
# Precio Claude Haiku 5.5 (USD por millón de tokens), para llevar la cuenta del gasto.
PRECIO_ENTRADA, PRECIO_SALIDA = 0.10, 0.50

VIDEO = {".mp4", ".mov", ".m4v", ".avi", ".mkv", ".mts", ".webm"}
FOTO = {".jpg", ".jpeg", ".png", ".heic", ".heif", ".tif", ".tiff", ".webp", ".gif", ".bmp"}
FFMPEG_TIMEOUT = 120

_lock = threading.Lock()
_gasto = {"entrada": 0, "salida": 0}


def normaliza(s: str) -> str:
    s = unicodedata.normalize("NFKD", s.lower())
    return "".join(c for c in s if not unicodedata.combining(c)).strip()


EXCLUIR_N = {normaliza(e) for e in EXCLUIR}


def excluido(ruta: str) -> bool:
    return any(normaliza(p) in EXCLUIR_N for p in ruta.split("/")[:-1])


def rel(ruta: str) -> str:
    """Ruta relativa a la carpeta del catálogo."""
    if ruta.lower().startswith(CARPETA.lower() + "/"):
        return ruta[len(CARPETA) + 1:]
    return ruta.lstrip("/")


def gasto_usd() -> float:
    return (_gasto["entrada"] * PRECIO_ENTRADA + _gasto["salida"] * PRECIO_SALIDA) / 1e6


def log(*a):
    print(time.strftime("%H:%M:%S"), *a, flush=True)


# --------------------------------------------------------------------------- Dropbox
def conecta_dropbox() -> dropbox.Dropbox:
    dbx = dropbox.Dropbox(
        oauth2_refresh_token=os.environ["DROPBOX_REFRESH_TOKEN"],
        app_key=os.environ["DROPBOX_APP_KEY"],
        app_secret=os.environ.get("DROPBOX_APP_SECRET") or None,
        timeout=120,
    )
    cuenta = dbx.users_get_current_account()
    raiz = cuenta.root_info
    # Cuentas de equipo: trabajar desde el espacio del equipo para ver /CPTQ/...
    if raiz.root_namespace_id != raiz.home_namespace_id:
        dbx = dbx.with_path_root(common.PathRoot.root(raiz.root_namespace_id))
    log("Dropbox conectado como", cuenta.name.display_name)
    return dbx


def lista_archivos(dbx: dropbox.Dropbox) -> list[files.FileMetadata]:
    res = dbx.files_list_folder(CARPETA, recursive=True, limit=2000)
    salida = list(res.entries)
    while res.has_more:
        res = dbx.files_list_folder_continue(res.cursor)
        salida.extend(res.entries)
    return [e for e in salida if isinstance(e, files.FileMetadata)]


def enlace_publico(dbx: dropbox.Dropbox, ruta: str) -> str | None:
    ajustes = sharing.SharedLinkSettings(
        requested_visibility=sharing.RequestedVisibility.public,
        audience=sharing.LinkAudience.public,
        access=sharing.RequestedLinkAccessLevel.viewer,
    )
    try:
        return dbx.sharing_create_shared_link_with_settings(ruta, settings=ajustes).url
    except dropbox.exceptions.ApiError as e:
        err = e.error
        if err.is_shared_link_already_exists():
            enlaces = dbx.sharing_list_shared_links(path=ruta, direct_only=True).links
            if enlaces:
                return enlaces[0].url
        log("  sin enlace público:", ruta, err)
        return None


# --------------------------------------------------------------------------- imagen
def ajusta(img: Image.Image, lado: int) -> Image.Image:
    img = ImageOps.exif_transpose(img).convert("RGB")
    img.thumbnail((lado, lado), Image.LANCZOS)
    return img


def a_jpeg_b64(img: Image.Image) -> str:
    buf = io.BytesIO()
    img.save(buf, "JPEG", quality=82)
    return base64.b64encode(buf.getvalue()).decode()


def guarda_mini(img: Image.Image, id_: str) -> str:
    nombre = re.sub(r"[^A-Za-z0-9_-]", "", id_.replace("id:", "")) + ".webp"
    mini = img.copy()
    mini.thumbnail((480, 480), Image.LANCZOS)
    mini.save(MINIS / nombre, "WEBP", quality=68, method=6)
    return f"minis/{nombre}"


def mosaico(fotogramas: list[Image.Image]) -> Image.Image:
    """Une hasta 4 fotogramas en una cuadrícula 2x2 (una sola imagen para Claude)."""
    w = max(f.width for f in fotogramas)
    h = max(f.height for f in fotogramas)
    cols = 2 if len(fotogramas) > 1 else 1
    filas = (len(fotogramas) + cols - 1) // cols
    lienzo = Image.new("RGB", (w * cols, h * filas), "black")
    for i, f in enumerate(fotogramas):
        lienzo.paste(f, ((i % cols) * w, (i // cols) * h))
    return lienzo


def ffprobe(url: str) -> dict:
    out = subprocess.run(
        ["ffprobe", "-v", "error", "-select_streams", "v:0", "-show_entries",
         "format=duration:stream=width,height,codec_name", "-of", "json", url],
        capture_output=True, text=True, timeout=FFMPEG_TIMEOUT, check=True,
    ).stdout
    return json.loads(out)


def fotograma(url: str, t: float, destino: Path) -> Image.Image | None:
    # -ss antes de -i: ffmpeg salta directamente al punto pedido usando peticiones
    # parciales (Range), así que solo baja unos pocos MB del vídeo.
    r = subprocess.run(
        ["ffmpeg", "-v", "error", "-y", "-ss", f"{t:.2f}", "-i", url, "-frames:v", "1",
         "-vf", "scale='min(640,iw)':-2", "-q:v", "3", str(destino)],
        capture_output=True, timeout=FFMPEG_TIMEOUT,
    )
    if r.returncode != 0 or not destino.exists():
        return None
    return ajusta(Image.open(destino), 640)


def procesa_video(dbx, meta) -> tuple[Image.Image, Image.Image, dict]:
    url = dbx.files_get_temporary_link(meta.path_lower).link
    info = ffprobe(url)
    dur = float(info.get("format", {}).get("duration") or 0)
    tiempos = [dur * p for p in (0.1, 0.37, 0.63, 0.9)] if dur > 1 else [0]
    with tempfile.TemporaryDirectory() as tmp:
        fotos = [fotograma(url, t, Path(tmp) / f"f{i}.jpg") for i, t in enumerate(tiempos)]
    fotos = [f for f in fotos if f is not None]
    if not fotos:
        raise RuntimeError("no se pudo extraer ningún fotograma")
    portada = fotos[len(fotos) // 2] if len(fotos) > 1 else fotos[0]
    extra = {"duracion": round(dur, 1), "ancho": portada.width, "alto": portada.height}
    return mosaico(fotos), portada, extra


def procesa_foto(dbx, meta) -> tuple[Image.Image, Image.Image, dict]:
    img = None
    try:
        _, resp = dbx.files_get_thumbnail_v2(
            files.PathOrLink.path(meta.path_lower),
            format=files.ThumbnailFormat.jpeg,
            size=files.ThumbnailSize.w1024h768,
            mode=files.ThumbnailMode.bestfit,
        )
        img = Image.open(io.BytesIO(resp.content))
    except dropbox.exceptions.ApiError:
        # Formato sin miniatura en Dropbox (p. ej. algunos HEIC/TIF): bajar el archivo.
        url = dbx.files_get_temporary_link(meta.path_lower).link
        r = requests.get(url, timeout=300)
        r.raise_for_status()
        img = Image.open(io.BytesIO(r.content))
    img = ajusta(img, 1024)
    return img, img, {"ancho": img.width, "alto": img.height}


# --------------------------------------------------------------------------- Claude
ESQUEMA = {
    "type": "object",
    "properties": {
        "descripcion": {"type": "string"},
        "etiquetas": {"type": "array", "items": {"type": "string"}},
        "lugar": {"type": "string"},
        "momento": {"type": "string", "enum": ["día", "atardecer", "noche", "interior", "no se sabe"]},
        "toma": {"type": "string", "enum": ["dron/aérea", "terrestre", "submarina", "no se sabe"]},
        "personas": {"type": "boolean"},
    },
    "required": ["descripcion", "etiquetas", "lugar", "momento", "toma", "personas"],
    "additionalProperties": False,
}

SISTEMA = (
    "Catalogas material de foto y vídeo de destinos turísticos de Quintana Roo (México) "
    "para que un equipo de marketing lo encuentre buscando por palabras. Escribe siempre en español."
)


def describe(cliente: anthropic.Anthropic, img: Image.Image, meta, es_video: bool) -> dict:
    ruta_rel = rel(meta.path_display)
    if es_video:
        intro = ("La imagen es un mosaico con 4 fotogramas de un mismo clip de vídeo sin audio, "
                 "en orden cronológico (arriba izquierda → abajo derecha).")
    else:
        intro = "La imagen es una fotografía."
    texto = (
        f"{intro}\nRuta en Dropbox (las carpetas y el nombre suelen indicar destino, lugar y mes): {ruta_rel}\n\n"
        "Devuelve:\n"
        "- descripcion: 1 o 2 frases concretas de lo que se ve (paisaje, actividad, objetos, colores, encuadre, "
        "movimiento de cámara si se nota). No inventes nombres de lugares que no se deduzcan de la ruta o de la imagen.\n"
        "- etiquetas: 10 a 18 palabras clave cortas en español, en minúsculas, útiles para buscar "
        "(p. ej. playa, cenote, alberca, hotel, comida, dron, atardecer, palmeras, mar turquesa, pareja, snorkel). "
        "Incluye siempre, además de lo concreto, las categorías generales y los sinónimos con los que alguien "
        "buscaría sin saber el nombre del lugar. Por ejemplo: un templo o pirámide maya → 'sitio arqueológico', "
        "'zona arqueológica', 'ruinas mayas', 'cultura maya'; un cenote → 'cenote', 'agua dulce', 'naturaleza'; "
        "un hotel → 'hotel', 'resort', 'hospedaje'; un restaurante → 'restaurante', 'gastronomía', 'comida'; "
        "fauna → 'fauna' y el animal concreto; un pueblo o centro → 'ciudad', 'calles', 'arquitectura'.\n"
        "- lugar: destino o sitio según la ruta y la imagen (p. ej. 'Tulum - Cenote Puerta al Cielo'); vacío si no se sabe.\n"
        "- momento, toma y personas según lo que se ve."
    )
    for intento in range(4):
        try:
            r = cliente.messages.create(
                model=MODELO,
                max_tokens=2000,
                system=SISTEMA,
                output_config={"effort": "low", "format": {"type": "json_schema", "schema": ESQUEMA}},
                messages=[{"role": "user", "content": [
                    {"type": "image", "source": {"type": "base64", "media_type": "image/jpeg", "data": a_jpeg_b64(img)}},
                    {"type": "text", "text": texto},
                ]}],
            )
            with _lock:
                _gasto["entrada"] += r.usage.input_tokens
                _gasto["salida"] += r.usage.output_tokens
            if r.stop_reason == "refusal":
                return {"descripcion": "", "etiquetas": [], "lugar": "", "momento": "no se sabe",
                        "toma": "no se sabe", "personas": False}
            texto_r = next(b.text for b in r.content if b.type == "text")
            return json.loads(texto_r)
        except (anthropic.RateLimitError, anthropic.APIConnectionError, anthropic.InternalServerError):
            time.sleep(5 * (intento + 1))
        except (json.JSONDecodeError, StopIteration):
            time.sleep(1)
    raise RuntimeError("Claude no respondió tras varios intentos")


# --------------------------------------------------------------------------- principal
def destino_de(ruta_rel: str) -> str:
    """Primer nivel útil de carpeta: el destino o el hotel/restaurante."""
    partes = ruta_rel.split("/")[:-1]
    saltar = {"levantamientos enlinea media", "02. destinos (raw)", "01. otras visitas", "otro stock",
              "tribu", "hay gente, todo bien", "contenido de hoteles y restaurantes", "kenia",
              "videos raw", "fotos raw", "vertical", "horizontal", "videos", "fotos", "b-roll"}
    meses = {"enero", "febrero", "marzo", "abril", "mayo", "junio", "julio", "agosto",
             "septiembre", "octubre", "noviembre", "diciembre"}
    alias = {"cancun": "Cancún", "pdc": "Playa del Carmen"}
    for p in partes:
        n = normaliza(p)
        sin_num = re.sub(r"^\d+\.?\s*", "", n)
        if n not in saltar and sin_num not in meses:
            return alias.get(n, p)
    return partes[0] if partes else ""


def procesa(dbx, cliente, meta) -> dict:
    ext = os.path.splitext(meta.name)[1].lower()
    es_video = ext in VIDEO
    img_ia, portada, extra = (procesa_video if es_video else procesa_foto)(dbx, meta)
    ia = describe(cliente, img_ia, meta, es_video)
    ruta_rel = rel(meta.path_display)
    return {
        "id": meta.id,
        "rev": meta.rev,
        "nombre": meta.name,
        "ruta": ruta_rel,
        "carpeta": ruta_rel.rsplit("/", 1)[0] if "/" in ruta_rel else "",
        "coleccion": ruta_rel.split("/")[0],
        "destino": destino_de(ruta_rel),
        "tipo": "video" if es_video else "foto",
        "tamano": meta.size,
        "fecha": meta.client_modified.strftime("%Y-%m-%d"),
        "mini": guarda_mini(portada, meta.id),
        "enlace": enlace_publico(dbx, meta.path_lower),
        **extra,
        **ia,
    }


def guarda(catalogo: dict):
    items = sorted(catalogo.values(), key=lambda x: x["ruta"].lower())
    tmp = CATALOGO.with_suffix(".tmp")
    tmp.write_text(json.dumps({"generado": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                               "total": len(items), "items": items},
                              ensure_ascii=False, separators=(",", ":")))
    tmp.replace(CATALOGO)


def main():
    inicio = time.time()
    DATOS.mkdir(parents=True, exist_ok=True)
    MINIS.mkdir(parents=True, exist_ok=True)
    previo = json.loads(CATALOGO.read_text())["items"] if CATALOGO.exists() else []
    catalogo = {x["id"]: x for x in previo}
    errores = json.loads(ERRORES.read_text()) if ERRORES.exists() else {}

    dbx = conecta_dropbox()
    cliente = anthropic.Anthropic(max_retries=4)
    todos = lista_archivos(dbx)
    validos = [m for m in todos
               if not excluido(rel(m.path_display))
               and os.path.splitext(m.name)[1].lower() in VIDEO | FOTO]
    log(f"En Dropbox: {len(todos)} archivos, {len(validos)} de foto/vídeo fuera de carpetas excluidas")

    vigentes = {m.id for m in validos}
    borrados = [i for i in catalogo if i not in vigentes]
    for i in borrados:  # archivos que ya no existen (o se movieron a una carpeta excluida)
        mini = MINIS / Path(catalogo[i]["mini"]).name
        mini.unlink(missing_ok=True)
        del catalogo[i]
    movidos = 0
    pendientes = []
    for m in validos:
        previo_m = catalogo.get(m.id)
        if previo_m and previo_m["rev"] == m.rev:
            ruta_rel = rel(m.path_display)
            if previo_m["ruta"] != ruta_rel:  # se movió o renombró: actualizar sin reprocesar
                previo_m.update(ruta=ruta_rel, nombre=m.name, carpeta=ruta_rel.rsplit("/", 1)[0],
                                coleccion=ruta_rel.split("/")[0], destino=destino_de(ruta_rel))
                movidos += 1
            continue
        if errores.get(m.id, {}).get("rev") == m.rev and errores[m.id].get("intentos", 0) >= 3:
            continue  # falló 3 veces con esta versión; no insistir
        pendientes.append(m)
    # Primero lo más reciente, para que lo nuevo aparezca antes.
    pendientes.sort(key=lambda m: m.client_modified, reverse=True)
    if MAX_ARCHIVOS:
        pendientes = pendientes[:MAX_ARCHIVOS]
    log(f"Borrados: {len(borrados)} · movidos: {movidos} · por procesar en esta ejecución: {len(pendientes)}")

    hechos = fallos = 0
    parar = threading.Event()

    def trabajo(m):
        if parar.is_set():
            return m, None, None
        try:
            return m, procesa(dbx, cliente, m), None
        except Exception as e:  # noqa: BLE001
            return m, None, e

    with cf.ThreadPoolExecutor(HILOS) as pool:
        futuros = [pool.submit(trabajo, m) for m in pendientes]
        for fut in cf.as_completed(futuros):
            m, item, err = fut.result()
            if item:
                catalogo[m.id] = item
                errores.pop(m.id, None)
                hechos += 1
            elif err:
                fallos += 1
                e = errores.get(m.id, {})
                errores[m.id] = {"ruta": m.path_display, "rev": m.rev, "error": str(err)[:300],
                                 "intentos": (e.get("intentos", 0) + 1) if e.get("rev") == m.rev else 1}
                log("  ERROR", m.path_display, "→", str(err)[:200])
            if (hechos + fallos) % 25 == 0 and (hechos or fallos):
                log(f"{hechos} hechos, {fallos} fallos, gasto IA ≈ {gasto_usd():.2f} USD")
                guarda(catalogo)
            if not parar.is_set():
                if (time.time() - inicio) / 60 > MAX_MINUTOS:
                    log("Tiempo máximo alcanzado; el resto se procesa en la próxima ejecución.")
                    parar.set()
                elif gasto_usd() > MAX_USD:
                    log(f"Tope de gasto ({MAX_USD} USD) alcanzado; el resto queda para la próxima ejecución.")
                    parar.set()

    guarda(catalogo)
    ERRORES.write_text(json.dumps(errores, ensure_ascii=False, indent=1))
    resumen = (f"Listo: {hechos} nuevos, {fallos} con error, {len(borrados)} borrados, {movidos} movidos. "
               f"Catálogo: {len(catalogo)} piezas. Gasto IA ≈ {gasto_usd():.2f} USD "
               f"({_gasto['entrada']} tokens entrada, {_gasto['salida']} salida).")
    log(resumen)
    if os.environ.get("GITHUB_STEP_SUMMARY"):
        with open(os.environ["GITHUB_STEP_SUMMARY"], "a") as f:
            f.write(resumen + "\n")
    pend = len([m for m in validos if m.id not in catalogo]) - len([e for e in errores.values() if e.get("intentos", 0) >= 3])
    print(f"::notice::Pendientes para próximas ejecuciones: {max(pend, 0)}")


if __name__ == "__main__":
    sys.exit(main())
