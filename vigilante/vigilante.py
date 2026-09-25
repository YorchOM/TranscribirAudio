#!/usr/bin/env python3
"""
Vigilante de carpeta Nextcloud: transcribe todo el audio que aparezca.

Habla con Nextcloud por WebDAV, como un cliente más. No toca el directorio de
datos por debajo, así que Nextcloud indexa y sincroniza solo: no hace falta
lanzar `occ files:scan` ni cuadrar el uid de www-data.

    Transcribir/entrada/   sueltas el audio aquí
    Transcribir/salida/    aparece <nombre>_transcripcion.txt
    Transcribir/hechos/    se aparta el original
    Transcribir/errores/   lo que falló, junto a su .log

La transcripción la hace transcribe_audio.py sin modificar, invocado como
proceso aparte: así un fallo suyo no se lleva por delante al vigilante.
"""

import email.utils
import logging
import os
import subprocess
import sys
import tempfile
import time
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import quote, unquote

import requests

# --- Configuración (todo por variables de entorno) -------------------------
NC_URL = os.environ.get("NC_URL", "").rstrip("/")
NC_USUARIO = os.environ.get("NC_USUARIO", "")
NC_CLAVE = os.environ.get("NC_CLAVE", "")
CARPETA = os.environ.get("NC_CARPETA", "Transcribir")

INTERVALO = int(os.environ.get("INTERVALO", "60"))
EDAD_MINIMA = int(os.environ.get("EDAD_MINIMA", "30"))
DIAS_RETENCION = int(os.environ.get("DIAS_RETENCION", "30"))
LIMITE_HORAS = float(os.environ.get("LIMITE_HORAS", "12"))

MODELO = os.environ.get("WHISPER_MODEL", "")
IDIOMA = os.environ.get("IDIOMA", "")

GUION = Path(os.environ.get("GUION", "/app/transcribe_audio.py"))

EXTENSIONES = {".mp3", ".wav", ".ogg", ".flac", ".opus", ".oga",
               ".m4a", ".wma", ".aac", ".mp4", ".m4b", ".amr", ".webm"}

SUB = ("entrada", "salida", "hechos", "errores")
NS = {"d": "DAV:"}

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-7s %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger("vigilante")


class Dav:
    """Cliente WebDAV mínimo: sólo lo que este vigilante necesita."""

    def __init__(self, url, usuario, clave):
        self.raiz = f"{url}/remote.php/dav/files/{quote(usuario)}"
        self.s = requests.Session()
        self.s.auth = (usuario, clave)
        self.s.headers["User-Agent"] = "vigilante-transcripcion/1.0"

    def url(self, *partes):
        cola = "/".join(quote(str(p).strip("/"), safe="/") for p in partes if p)
        return f"{self.raiz}/{cola}" if cola else self.raiz

    def crear_carpeta(self, ruta):
        r = self.s.request("MKCOL", self.url(ruta), timeout=30)
        if r.status_code not in (201, 405):  # 405 = ya existía
            r.raise_for_status()

    def listar(self, ruta):
        cuerpo = (
            '<?xml version="1.0"?>'
            '<d:propfind xmlns:d="DAV:"><d:prop>'
            "<d:resourcetype/><d:getcontentlength/><d:getlastmodified/>"
            "</d:prop></d:propfind>"
        )
        r = self.s.request(
            "PROPFIND", self.url(ruta), data=cuerpo, timeout=60,
            headers={"Depth": "1", "Content-Type": "application/xml"},
        )
        r.raise_for_status()
        ficheros = []
        for resp in ET.fromstring(r.content).findall("d:response", NS):
            prop = resp.find("d:propstat/d:prop", NS)
            if prop is None or prop.find("d:resourcetype/d:collection", NS) is not None:
                continue  # la propia carpeta, o una subcarpeta
            href = unquote(resp.findtext("d:href", "", NS))
            fecha = prop.findtext("d:getlastmodified", "", NS)
            ficheros.append({
                "nombre": href.rstrip("/").rsplit("/", 1)[-1],
                "tam": int(prop.findtext("d:getcontentlength", "0", NS) or 0),
                "mod": email.utils.parsedate_to_datetime(fecha) if fecha else None,
            })
        return ficheros

    def descargar(self, ruta, destino):
        with self.s.get(self.url(ruta), stream=True, timeout=300) as r:
            r.raise_for_status()
            with open(destino, "wb") as f:
                for trozo in r.iter_content(1 << 20):
                    f.write(trozo)

    def subir(self, origen, ruta):
        with open(origen, "rb") as f:
            r = self.s.put(self.url(ruta), data=f, timeout=600)
        r.raise_for_status()

    def mover(self, origen, destino):
        r = self.s.request(
            "MOVE", self.url(origen), timeout=60,
            headers={"Destination": self.url(destino), "Overwrite": "T"},
        )
        r.raise_for_status()

    def borrar(self, ruta):
        r = self.s.delete(self.url(ruta), timeout=60)
        if r.status_code not in (204, 404):
            r.raise_for_status()


def transcribir(ruta_audio):
    """Lanza transcribe_audio.py sobre un fichero ya descargado."""
    orden = [sys.executable, "-u", str(GUION), str(ruta_audio)]
    if MODELO:
        orden += ["--modelo", MODELO]
    if IDIOMA:
        orden += ["--idioma", IDIOMA]

    entorno = dict(os.environ)
    entorno.setdefault("PYTHONUNBUFFERED", "1")

    proc = subprocess.run(
        orden, cwd=ruta_audio.parent, env=entorno, timeout=LIMITE_HORAS * 3600,
        capture_output=True, text=True, errors="replace",
    )
    return proc.returncode, (proc.stdout or "") + (proc.stderr or "")


def procesar(dav, nombre):
    """Descarga, transcribe y coloca los resultados. Devuelve True si fue bien."""
    origen = f"{CARPETA}/entrada/{nombre}"
    base = Path(nombre).stem
    arranque = time.monotonic()

    with tempfile.TemporaryDirectory(prefix="transcribir-") as tmp:
        audio = Path(tmp) / nombre
        log.info("Descargando %s", nombre)
        dav.descargar(origen, audio)

        log.info("Transcribiendo %s (modelo %s)", nombre, MODELO or "por defecto")
        codigo, salida = transcribir(audio)
        minutos = (time.monotonic() - arranque) / 60

        txt = audio.with_name(base + "_transcripcion.txt")
        if codigo != 0 or not txt.exists():
            log.error("Falló %s (código %s, %.1f min)", nombre, codigo, minutos)
            registro = Path(tmp) / (base + ".log")
            registro.write_text(salida, encoding="utf-8")
            dav.subir(registro, f"{CARPETA}/errores/{registro.name}")
            dav.mover(origen, f"{CARPETA}/errores/{nombre}")
            return False

        dav.subir(txt, f"{CARPETA}/salida/{txt.name}")
        dav.mover(origen, f"{CARPETA}/hechos/{nombre}")
        log.info("Listo %s -> %s (%.1f min)", nombre, txt.name, minutos)
        return True


def purgar(dav):
    """Borra de hechos/ lo que pase de DIAS_RETENCION."""
    if DIAS_RETENCION <= 0:
        return
    limite = datetime.now(timezone.utc) - timedelta(days=DIAS_RETENCION)
    for f in dav.listar(f"{CARPETA}/hechos"):
        if f["mod"] and f["mod"] < limite:
            log.info("Purgando %s (más de %d días)", f["nombre"], DIAS_RETENCION)
            dav.borrar(f"{CARPETA}/hechos/" + f["nombre"])


def pendientes(dav):
    """Audios de entrada/ lo bastante asentados como para tocarlos."""
    ahora = datetime.now(timezone.utc)
    listos = []
    for f in dav.listar(f"{CARPETA}/entrada"):
        if Path(f["nombre"]).suffix.lower() not in EXTENSIONES:
            continue
        if f["tam"] == 0:
            continue
        if f["mod"] and (ahora - f["mod"]).total_seconds() < EDAD_MINIMA:
            log.debug("%s aún se está subiendo, espero", f["nombre"])
            continue
        listos.append(f["nombre"])
    return sorted(listos)


def main():
    faltan = [n for v, n in ((NC_URL, "NC_URL"), (NC_USUARIO, "NC_USUARIO"),
                             (NC_CLAVE, "NC_CLAVE")) if not v]
    if faltan:
        log.error("Faltan variables de entorno: %s", ", ".join(faltan))
        return 1
    if not GUION.exists():
        log.error("No encuentro el transcriptor en %s", GUION)
        return 1

    dav = Dav(NC_URL, NC_USUARIO, NC_CLAVE)
    for sub in SUB:
        dav.crear_carpeta(f"{CARPETA}/{sub}")
    log.info("Vigilando %s/entrada cada %d s", CARPETA, INTERVALO)

    ultima_purga = 0.0
    while True:
        try:
            for nombre in pendientes(dav):
                procesar(dav, nombre)
            if time.monotonic() - ultima_purga > 6 * 3600:
                purgar(dav)
                ultima_purga = time.monotonic()
        except requests.RequestException as e:
            log.warning("Nextcloud no responde (%s); reintento en %d s", e, INTERVALO)
        except Exception:
            log.exception("Error inesperado en el ciclo; sigo vivo")
        time.sleep(INTERVALO)


if __name__ == "__main__":
    sys.exit(main())
