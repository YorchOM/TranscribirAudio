"""
Diarización de hablantes (quién habla y cuándo) con pyannote.audio.

Se ejecuta 100% en local sobre CPU. La única dependencia externa es el token
de Hugging Face, necesario UNA vez para descargar el modelo (que queda
cacheado en ~/.cache/huggingface y ya no vuelve a bajarse).

Uso típico desde transcribe_audio.py:
    from diarize import diarizar
    turnos = diarizar(ruta_wav, num_hablantes=3)
    # -> [(inicio_seg, fin_seg, "SPEAKER_00"), ...]
"""

import os
import sys
import wave

# --- Telemetría desactivada ------------------------------------------------
# Hay que fijarlas ANTES de importar pyannote.audio (su módulo telemetry lee la
# variable al importarse y, si no existe, la pone a lo que diga su config.yaml).
#
# PYANNOTE_METRICS_ENABLED: por defecto pyannote.audio 4 envía a
#   https://otel.pyannote.ai/v1/traces —en cada carga de modelo y en cada audio
#   procesado— un id de sesión aleatorio, su versión, el nombre del pipeline, la
#   DURACIÓN del audio y el número de hablantes. No manda audio ni texto, pero sí
#   un perfil de uso. Además silencia sus propios errores (log level CRITICAL).
# HF_HUB_DISABLE_TELEMETRY: telemetría de huggingface_hub. Hoy pyannote no la
#   usa, se pone por si acaso.
#
# Se usa setdefault a propósito: si algún día quieres reactivarlas, basta con
# exportar la variable correspondiente, sin tocar este fichero.
os.environ.setdefault("PYANNOTE_METRICS_ENABLED", "false")
os.environ.setdefault("HF_HUB_DISABLE_TELEMETRY", "1")

# Nota: NO se fuerza HF_HUB_OFFLINE porque impediría la descarga inicial del
# modelo. Una vez cacheado, puedes exportarlo a 1 y no se vuelve a contactar
# con Hugging Face.
# ---------------------------------------------------------------------------

# Modelo por defecto. Se puede sobreescribir con la variable PYANNOTE_MODEL.
MODELO_DIARIZACION = os.environ.get(
    "PYANNOTE_MODEL", "pyannote/speaker-diarization-community-1"
)

_pipeline = None  # cache del pipeline ya cargado


class DiarizacionNoDisponible(RuntimeError):
    """La diarización no se puede usar (falta pyannote, token o modelo)."""


def _ruta_recurso(*partes):
    base = getattr(sys, "_MEIPASS", None) or os.path.dirname(os.path.abspath(__file__))
    return os.path.join(base, *partes)


def leer_token():
    """Busca el token de Hugging Face en variables de entorno, fichero o caché."""
    for var in ("HF_TOKEN", "HUGGINGFACE_TOKEN", "HUGGING_FACE_HUB_TOKEN"):
        valor = os.environ.get(var)
        if valor and valor.strip():
            return valor.strip()

    candidatos = [
        _ruta_recurso(".hf_token"),
        os.path.join(os.path.expanduser("~"), ".hf_token"),
    ]
    for ruta in candidatos:
        try:
            if os.path.isfile(ruta):
                with open(ruta, "r", encoding="utf-8") as f:
                    token = f.read().strip()
                if token:
                    return token
        except OSError:
            pass

    # Caché de `huggingface-cli login`
    try:
        from huggingface_hub import get_token

        token = get_token()
        if token:
            return token
    except Exception:
        pass

    return None


def cargar_waveform(ruta_wav):
    """
    Lee un WAV PCM y devuelve (tensor [1, muestras], sample_rate).

    Se lee con el módulo `wave` de la stdlib a propósito: así pyannote recibe
    el audio ya decodificado y no necesita torchcodec/ffmpeg-shared, que en
    Windows suele fallar porque la build de winget es estática.
    """
    import numpy as np
    import torch

    with wave.open(ruta_wav, "rb") as w:
        n_canales = w.getnchannels()
        ancho = w.getsampwidth()
        sample_rate = w.getframerate()
        crudo = w.readframes(w.getnframes())

    tipos = {1: np.uint8, 2: np.int16, 4: np.int32}
    if ancho not in tipos:
        raise DiarizacionNoDisponible(f"Ancho de muestra WAV no soportado: {ancho} bytes")

    datos = np.frombuffer(crudo, dtype=tipos[ancho]).astype(np.float32)
    if ancho == 1:  # PCM de 8 bits es sin signo
        datos = (datos - 128.0) / 128.0
    else:
        datos = datos / float(np.iinfo(tipos[ancho]).max)

    if n_canales > 1:
        datos = datos.reshape(-1, n_canales).mean(axis=1)

    return torch.from_numpy(np.ascontiguousarray(datos)).unsqueeze(0), sample_rate


def cargar_pipeline(token=None):
    """Carga (y cachea) el pipeline de diarización. Lanza DiarizacionNoDisponible."""
    global _pipeline
    if _pipeline is not None:
        return _pipeline

    try:
        from pyannote.audio import Pipeline
    except ImportError as e:
        raise DiarizacionNoDisponible(
            "pyannote.audio no está instalado. Instálalo con:\n"
            "    .venv/Scripts/python.exe -m pip install pyannote.audio"
        ) from e

    token = token or leer_token()
    if not token:
        raise DiarizacionNoDisponible(
            "Falta el token de Hugging Face.\n"
            "  1) Crea un token de lectura en https://huggingface.co/settings/tokens\n"
            f"  2) Acepta las condiciones del modelo en https://huggingface.co/{MODELO_DIARIZACION}\n"
            "  3) Guárdalo en el fichero .hf_token del proyecto o en la variable HF_TOKEN"
        )

    print(f"Cargando modelo de diarización ({MODELO_DIARIZACION})...")
    try:
        _pipeline = Pipeline.from_pretrained(MODELO_DIARIZACION, token=token)
    except TypeError:
        # pyannote.audio 3.x usaba use_auth_token en vez de token
        _pipeline = Pipeline.from_pretrained(MODELO_DIARIZACION, use_auth_token=token)
    except Exception as e:
        raise DiarizacionNoDisponible(
            f"No se pudo cargar {MODELO_DIARIZACION}: {e}\n"
            "Comprueba que has aceptado las condiciones del modelo con la misma "
            "cuenta con la que generaste el token."
        ) from e

    if _pipeline is None:
        raise DiarizacionNoDisponible(
            f"Hugging Face devolvió un pipeline vacío para {MODELO_DIARIZACION}. "
            "Casi siempre significa que faltan por aceptar las condiciones del modelo."
        )

    print("Modelo de diarización cargado.")
    return _pipeline


def _extraer_turnos(resultado):
    """Normaliza la salida de pyannote (v3/v4) a [(inicio, fin, etiqueta), ...]."""
    anotacion = resultado
    # pyannote 4 puede devolver un objeto contenedor
    for attr in ("speaker_diarization", "diarization"):
        if not hasattr(anotacion, "itertracks") and hasattr(anotacion, attr):
            anotacion = getattr(anotacion, attr)

    if not hasattr(anotacion, "itertracks"):
        raise DiarizacionNoDisponible(
            f"Formato de salida de pyannote no reconocido: {type(resultado)}"
        )

    turnos = [
        (float(segmento.start), float(segmento.end), str(etiqueta))
        for segmento, _, etiqueta in anotacion.itertracks(yield_label=True)
    ]
    turnos.sort(key=lambda t: t[0])
    return turnos


def diarizar(ruta_wav, num_hablantes=None, min_hablantes=None, max_hablantes=None, token=None):
    """
    Devuelve los turnos de palabra de un WAV: [(inicio_seg, fin_seg, etiqueta), ...].

    num_hablantes: si se conoce el número exacto de personas, mejora bastante.
    """
    pipeline = cargar_pipeline(token=token)
    waveform, sample_rate = cargar_waveform(ruta_wav)

    kwargs = {}
    if num_hablantes:
        kwargs["num_speakers"] = int(num_hablantes)
    else:
        if min_hablantes:
            kwargs["min_speakers"] = int(min_hablantes)
        if max_hablantes:
            kwargs["max_speakers"] = int(max_hablantes)

    entrada = {"waveform": waveform, "sample_rate": sample_rate}

    print("Detectando hablantes... (puede tardar varios minutos en CPU)")
    try:
        from pyannote.audio.pipelines.utils.hook import ProgressHook

        with ProgressHook() as hook:
            resultado = pipeline(entrada, hook=hook, **kwargs)
    except Exception:
        resultado = pipeline(entrada, **kwargs)

    turnos = _extraer_turnos(resultado)
    etiquetas = sorted({t[2] for t in turnos})
    print(f"Hablantes detectados: {len(etiquetas)} ({', '.join(etiquetas)})")
    return turnos
