"""
Modelos de Whisper y configuración: dónde viven, cómo se bajan, cuál se usa.

Lo comparten la aplicación (app.py) y el transcriptor por línea de comandos
(transcribe_audio.py), para que los dos miren siempre la misma carpeta y el
mismo fichero de configuración.
"""

import configparser
import os
import shutil
import sys

MODELO_POR_DEFECTO = "small"
NOMBRE_INI = "TranscribirAudio.ini"

# Notas para el listado, medidas sobre 10 min de audio en español en un
# i7-12700H. Los modelos que no aparecen aquí se listan igual (salen de
# faster-whisper, que añade los nuevos en cada versión), sólo que sin nota.
NOTAS_MODELOS = {
    "tiny": "el más rápido, calidad pobre",
    "base": "muy rápido, calidad justa",
    "small": "equilibrio velocidad/calidad (16x tiempo real)",
    "medium": "mejor que small, bastante más lento",
    "large-v1": "antiguo: mejor large-v3",
    "large-v2": "antiguo: mejor large-v3",
    "large-v3": "máxima calidad, muy lento en CPU",
    "large-v3-turbo": "casi como large-v3; 2,5x más lento que small",
}

FICHEROS_MODELO = ("config.json", "preprocessor_config.json", "model.bin",
                   "tokenizer.json", "vocabulary.*")


# --------------------------------------------------------------------------
# Rutas
# --------------------------------------------------------------------------

def carpeta_programa():
    """Carpeta del .exe (o del script, al ejecutarlo con Python)."""
    if getattr(sys, "frozen", False):
        return os.path.dirname(sys.executable)
    return os.path.dirname(os.path.abspath(__file__))


def carpeta_modelos():
    """
    Dónde se guardan los modelos: `modelos/` junto al programa (o al .exe).

    Así la carpeta del programa es autocontenida y se puede copiar a otro
    equipo con el modelo ya descargado. Si ahí no se puede escribir (p. ej.
    en Archivos de programa), se usa %LOCALAPPDATA%.
    """
    elegida = os.environ.get("TRANSCRIBIR_MODELOS")
    if elegida:
        return elegida

    ruta = os.path.join(carpeta_programa(), "modelos")
    try:
        os.makedirs(ruta, exist_ok=True)
        if os.access(ruta, os.W_OK):
            return ruta
    except OSError:
        pass

    local = os.environ.get("LOCALAPPDATA") or os.path.expanduser("~")
    return os.path.join(local, "TranscribirAudio", "modelos")


def ruta_ini():
    return os.path.join(carpeta_programa(), NOMBRE_INI)


# --------------------------------------------------------------------------
# Configuración
# --------------------------------------------------------------------------

PLANTILLA_INI = """\
; Configuración del Transcriptor de Audio.
; Lo más cómodo es cambiarla desde la propia aplicación, pero también se puede
; editar con el Bloc de notas.

[transcripcion]
; Modelo de Whisper. Si no está descargado, se descarga solo la primera vez
; que se usa (a la carpeta "modelos", junto al programa).
;
;   small   buen equilibrio entre velocidad y calidad (el de por defecto)
;   medium  mejor calidad, bastante más lento
;   turbo   calidad casi máxima, unas 2,5 veces más lento que small
;   base    más rápido, peor calidad
;
; También vale cualquier modelo compatible con faster-whisper, como "usuario/modelo".
modelo = {modelo}

; Idioma fijo (es, en, fr...). Vacío = detectarlo en cada audio.
idioma = {idioma}

; Carpeta donde dejar las transcripciones. Vacío = junto a cada audio.
carpeta_salida = {carpeta_salida}
"""


def leer_configuracion():
    """Lee el .ini junto al programa. Si no existe o está roto, valores vacíos."""
    # Sin interpolación: una ruta con "%" no debe romper la lectura
    ini = configparser.ConfigParser(interpolation=None)
    try:
        ini.read(ruta_ini(), encoding="utf-8")
    except (configparser.Error, UnicodeDecodeError) as e:
        print(f"[!] {NOMBRE_INI} ilegible ({e}); se usan los valores por defecto.")
        return {}
    if not ini.has_section("transcripcion"):
        return {}
    return {k: v.strip() for k, v in ini.items("transcripcion") if v.strip()}


def guardar_configuracion(**cambios):
    """
    Cambia valores del .ini (modelo, idioma, carpeta_salida) y conserva el resto.

    Se reescribe desde la plantilla, así los comentarios no se pierden.
    """
    valores = {"modelo": MODELO_POR_DEFECTO, "idioma": "", "carpeta_salida": ""}
    valores.update(leer_configuracion())
    valores.update({k: (v or "") for k, v in cambios.items()})
    with open(ruta_ini(), "w", encoding="utf-8") as f:
        f.write(PLANTILLA_INI.format(**{k: valores[k] for k in
                                        ("modelo", "idioma", "carpeta_salida")}))


def modelo_configurado():
    """El modelo que toca usar: variable de entorno > .ini > por defecto."""
    return (os.environ.get("WHISPER_MODEL") or leer_configuracion().get("modelo")
            or MODELO_POR_DEFECTO)


# --------------------------------------------------------------------------
# Catálogo
# --------------------------------------------------------------------------

def modelos_conocidos():
    """Nombre corto -> repo, según la versión instalada de faster-whisper."""
    from faster_whisper.utils import _MODELS
    return dict(_MODELS)


def resolver_repo(nombre):
    """'small' -> 'Systran/faster-whisper-small'; un 'usuario/modelo' se deja tal cual."""
    if "/" in nombre:
        return nombre
    repo = modelos_conocidos().get(nombre)
    if not repo:
        raise ValueError(f"Modelo desconocido '{nombre}'. Mira la pestaña Modelos de la"
                         " aplicación")
    return repo


def carpeta_de(repo):
    # Por repo y no por nombre corto: 'turbo' y 'large-v3-turbo' son el mismo
    # modelo y no deben bajarse dos veces
    return os.path.join(carpeta_modelos(), repo.replace("/", "--"))


def esta_descargado(repo):
    # model.bin se baja el último: si está, la descarga se completó
    return os.path.isfile(os.path.join(carpeta_de(repo), "model.bin"))


def solo_ingles(nombre):
    # Los .en y todos los distil-* sólo transcriben inglés
    return nombre.endswith(".en") or nombre.startswith("distil-")


def mismo_modelo(a, b):
    try:
        return resolver_repo(a) == resolver_repo(b)
    except ValueError:
        return a == b


def catalogo():
    """
    Lista de modelos para mostrar: [{nombre, repo, nota, solo_ingles, descargado}].

    Un nombre por repo (los alias large -> large-v3 y turbo -> large-v3-turbo
    se quedan con el nombre completo), más los externos que ya estén bajados.
    """
    por_repo = {}
    for nombre, repo in modelos_conocidos().items():
        por_repo.setdefault(repo, nombre)

    # Modelos "usuario/modelo" bajados a mano: también se listan
    try:
        for carpeta in sorted(os.listdir(carpeta_modelos())):
            repo = carpeta.replace("--", "/", 1)
            if "/" in repo and repo not in por_repo and esta_descargado(repo):
                por_repo[repo] = repo
    except OSError:
        pass

    return [
        {
            "nombre": nombre,
            "repo": repo,
            "nota": NOTAS_MODELOS.get(nombre, "modelo externo" if nombre == repo else ""),
            "solo_ingles": solo_ingles(nombre),
            "descargado": esta_descargado(repo),
        }
        for repo, nombre in por_repo.items()
    ]


# --------------------------------------------------------------------------
# Descarga
# --------------------------------------------------------------------------

def _progreso_consola(fichero, hecho, total):
    """Progreso por defecto: una línea que se reescribe, sólo en los grandes."""
    if total < (10 << 20):
        return
    print(f"\r  {fichero}: {100 * hecho // total}% ({hecho >> 20} de {total >> 20} MB)",
          end="" if hecho < total else "\n", flush=True)


def _bajar(url, ruta, al_avanzar):
    """Descarga un fichero a `ruta` (vía .part, así un corte no deja basura)."""
    import urllib.request

    parcial = ruta + ".part"
    with urllib.request.urlopen(url, timeout=60) as r, open(parcial, "wb") as f:
        total = int(r.headers.get("Content-Length") or 0)
        hecho, ultimo = 0, 0
        while True:
            bloque = r.read(1 << 20)
            if not bloque:
                break
            f.write(bloque)
            hecho += len(bloque)
            if total and (hecho - ultimo >= total // 100 or hecho == total):
                ultimo = hecho
                al_avanzar(hecho, total)
    os.replace(parcial, ruta)  # ya cerrado: en Windows no se renombra un fichero abierto


def ficheros_remotos(repo):
    """{fichero: bytes} de lo que hay que bajar. Valida que sea un modelo usable."""
    import fnmatch
    import json
    import urllib.error
    import urllib.request

    try:
        url = f"https://huggingface.co/api/models/{repo}?blobs=true"
        with urllib.request.urlopen(url, timeout=60) as r:
            ficheros = {s["rfilename"]: s.get("size") or 0 for s in json.load(r)["siblings"]}
    except urllib.error.HTTPError as e:
        raise ValueError(f"No existe el modelo '{repo}' (HTTP {e.code})") from e
    except OSError as e:
        raise ValueError(f"No hay conexión para descargar '{repo}': {e}") from e
    if "model.bin" not in ficheros:
        raise ValueError(f"'{repo}' no es un modelo para faster-whisper (no trae model.bin"
                         " en formato CTranslate2)")
    if "tokenizer.json" not in ficheros:
        # Sin él, faster-whisper se baja otro por su cuenta, a la caché de
        # huggingface_hub y con rutas largas: mejor no aceptarlo
        raise ValueError(f"'{repo}' no trae tokenizer.json; usa otro modelo")
    return {f: t for f, t in ficheros.items()
            if any(fnmatch.fnmatch(f, p) for p in FICHEROS_MODELO)}


def descargar_modelo(nombre, progreso=None, avisar=print):
    """
    Devuelve la carpeta del modelo, descargándolo si todavía no está.

    Se baja fichero a fichero por HTTPS a una carpeta plana
    (modelos/Systran--faster-whisper-small/model.bin), sin huggingface_hub:
    éste guarda metadatos en rutas como .cache/huggingface/download/<hash>...
    .incomplete, que pasan con facilidad de los 260 caracteres de Windows, y
    el .exe no admite rutas largas. Los modelos son públicos: no hace falta
    cuenta ni token.

    progreso(fichero, hecho, total): para mostrar el avance (por defecto, en
    la consola).
    """
    repo = resolver_repo(nombre)
    destino = carpeta_de(repo)
    if esta_descargado(repo):
        return destino  # ya está: no se contacta con nadie

    ficheros = ficheros_remotos(repo)
    avisar(f"Descargando el modelo '{nombre}' ({sum(ficheros.values()) >> 20} MB) en {destino}")
    avisar("Solo pasa la primera vez que se usa este modelo.")

    progreso = progreso or _progreso_consola
    os.makedirs(destino, exist_ok=True)
    # model.bin el último: su presencia es la marca de "descarga completa"
    for f in sorted(ficheros, key=lambda f: f == "model.bin"):
        _bajar(f"https://huggingface.co/{repo}/resolve/main/{f}", os.path.join(destino, f),
               lambda hecho, total, f=f: progreso(f, hecho, total))
    avisar("Modelo descargado.")
    return destino


def borrar_modelo(nombre):
    """Borra un modelo descargado (se puede volver a bajar cuando se quiera)."""
    carpeta = carpeta_de(resolver_repo(nombre))
    if os.path.isdir(carpeta):
        shutil.rmtree(carpeta)
