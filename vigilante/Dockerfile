# Imagen del vigilante: el transcriptor de siempre + el bucle de WebDAV.
# Se construye desde la raíz del proyecto (context: ..), para poder copiar
# transcribe_audio.py y gestor_modelos.py sin duplicarlos.
FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    TRANSCRIBIR_MODELOS=/modelos \
    HF_HUB_DISABLE_TELEMETRY=1

WORKDIR /app

# faster-whisper no necesita torch ni ffmpeg: CTranslate2 hace la inferencia y
# PyAV (con sus propias librerías de ffmpeg dentro) decodifica el audio.
COPY requirements.txt /app/requirements.txt
RUN pip install -r /app/requirements.txt requests

COPY transcribe_audio.py gestor_modelos.py /app/
COPY vigilante/vigilante.py /app/

CMD ["python", "-u", "/app/vigilante.py"]
