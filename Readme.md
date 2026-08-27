# TranscribirAudio

Aplicación para transcribir archivos de audio usando **Whisper de OpenAI** con detección
automática de idioma e **identificación de hablantes** (**pyannote.audio**).

## ✨ Características Principales

- **Detección automática de idioma**: Soporta inglés, chino, español y muchos más sin configuración
- **Quién dice cada cosa**: Identifica los hablantes y etiqueta cada línea (activado por defecto)
- **Audio multilingüe**: Maneja archivos con mezcla de idiomas (code-switching)
- **Compatibilidad de formatos**: Soporta WAV, MP3, M4A, FLAC, OGG, AAC, OPUS, WMA
- **Timestamps por segmento**: Cada línea de la transcripción incluye marca de tiempo inicio y fin
- **Procesamiento local**: No requiere conexión a internet (tras la descarga inicial de los modelos)
- **Guardado automático**: La transcripción se guarda en archivos .txt
- **Procesamiento por lotes**: Puede procesar varios archivos de audio de una vez
- **Tres modos de velocidad**: `rapido` / `normal` / `preciso` (ver más abajo)

## ⏱️ Modos de ejecución: rápido o fino

| Modo | Hablantes | Cómo transcribe | Cuándo usarlo |
|------|-----------|-----------------|---------------|
| `rapido` | ❌ no | Troceado en paralelo, sin reintentos de temperatura | Sacar el texto cuanto antes |
| `normal` *(por defecto)* | ✅ sí | Troceado en paralelo, diarización simultánea | El día a día |
| `preciso` | ✅ sí | Un solo proceso, todos los reintentos, con contexto | Si el modo normal se atraganta |

```powershell
python transcribe_audio.py "reunion.mp3" --rapido        # sin identificar hablantes
python transcribe_audio.py "reunion.mp3"                 # normal
python transcribe_audio.py "reunion.mp3" --modo preciso
```

Referencia medida en un i7-12700H (14 núcleos físicos, sin GPU), modelo `small`,
sobre una grabación real de reunión con 3 personas:

| | Antes (v1, en serie) | Ahora |
|---|---|---|
| 10 min de audio, sin hablantes | ~8-10 min | **1 min 35 s** |
| 10 min de audio, con hablantes | ~25 min | **5 min 34 s** |
| 1 h de audio, sin hablantes | ~50 min | **~10 min** |
| 1 h de audio, con hablantes | 2 h 37 *(medido)* | **~35 min** |

En el caso con hablantes el reloj lo marca **entero la diarización**: en la prueba de
10 minutos, Whisper tardó 2 min 24 y pyannote 5 min 30, y como van en paralelo el total
fue 5 min 34. Bajar de ahí exige renunciar a saber quién habla (`--rapido`).

De dónde sale la mejora:

1. **La diarización ya no espera a la transcripción.** Antes iban en serie (primero
   pyannote entero, después Whisper entero) y el total era la suma. Ahora se lanzan a la
   vez en procesos separados, así que el total es el del más lento.
2. **Troceado en paralelo.** Un solo proceso de Whisper escala mal con muchos núcleos:
   las matrices del modelo `small` son pequeñas y los hilos se pasan el rato esperando.
   El audio se parte en tramos de ~5 min y se transcriben varios a la vez, cada uno con
   menos hilos. Los cortes se buscan en los silencios (el medio segundo más flojo dentro
   de ±25 s del corte teórico) para no partir palabras.
3. **Menos reintentos de temperatura.** Cuando Whisper sospecha que un bloque le ha
   salido mal, lo repite hasta 6 veces con temperatura creciente. En audio de reunión
   (ruido, solapes, gente lejos del micro) eso salta continuamente y es lo que más
   dispara el reloj. `rapido` los quita, `normal` deja 3, `preciso` los mantiene todos.
4. **Sin `condition_on_previous_text`** salvo en `preciso`: es el causante de los bucles
   de repetición y obliga a rehacer bloques (y al trocear se pierde igualmente).

## 🗣️ Identificación de hablantes (diarización)

Va **activada por defecto**. Cada línea sale así:

```
[Idioma principal detectado: es]
[Hablantes identificados: 3 - Hablante 1, Hablante 2, Hablante 3]

[00:00:00 -> 00:00:04] Hablante 1: Buenos días, ¿empezamos?
[00:00:04 -> 00:00:09] Hablante 2: Sí, yo traigo el punto de presupuesto.
```

### Configuración inicial (una sola vez)

El modelo de pyannote es "gated": hace falta un token de Hugging Face para descargarlo.
Después queda cacheado en `~/.cache/huggingface` y ya funciona sin conexión.

1. Crea una cuenta (gratis) en <https://huggingface.co> si no la tienes.
2. Acepta las condiciones del modelo en
   <https://huggingface.co/pyannote/speaker-diarization-community-1>
   (botón *Agree and access repository*).
3. Genera un token de **lectura** en <https://huggingface.co/settings/tokens>.
4. Guarda el token en el fichero `.hf_token` dentro de esta carpeta
   (está en `.gitignore`, no se sube), o en la variable de entorno `HF_TOKEN`.

Si falta el token, la aplicación **avisa y sigue** transcribiendo sin hablantes.

Conviene crear un token **fine-grained** con permiso de solo lectura sobre ese único
repositorio, y no un token clásico (que puede leer todos tus repos privados). El fichero
`.hf_token` está en texto plano en el disco, así que cuanto menos permita, mejor.

### 🔒 Privacidad: qué sale de este equipo

**El audio, la transcripción y los nombres de fichero nunca salen del equipo.** Todo el
procesado es local. Lo único que viaja es:

| Momento | Qué se comparte | Con quién |
|---|---|---|
| Aceptar las condiciones del modelo | Tu nombre de usuario y tu **email** | pyannoteAI (autores del repo) |
| Descarga del modelo (solo la 1ª vez) | Identidad de la cuenta vía token, repo pedido, IP, versiones de librerías/Python | Hugging Face |
| ~~Cada ejecución~~ | ~~Duración del audio, nº de hablantes, versión, id de sesión~~ | ~~pyannoteAI~~ — **desactivado** |

Ese último canal es la telemetría propia de pyannote.audio 4, que viene **activada de
fábrica** (`pyannote/audio/telemetry/config.yaml` → `otel.pyannote.ai`) y envía un perfil de
uso en cada audio procesado. `diarize.py` la desactiva poniendo `PYANNOTE_METRICS_ENABLED=false`
antes de importar pyannote. Para reactivarla no hace falta tocar código: basta con exportar
`PYANNOTE_METRICS_ENABLED=true`.

Si quieres cortar también el contacto con Hugging Face una vez descargado el modelo, exporta
`HF_HUB_OFFLINE=1` (no se pone por defecto porque impediría la descarga inicial).

### Consejos de precisión

- Pasar `--hablantes N` cuando se sabe cuántas personas hay mejora bastante el resultado.
- Micrófono cercano y poco solapamiento entre voces son los dos factores que más pesan.
- Con `--nombres` se sustituye "Hablante 1/2/3" por los nombres reales, por orden de aparición.

### Cómo se reparte el texto entre hablantes

pyannote dice *quién* habla y *cuándo*; Whisper dice *qué* se dice y en qué instante. Casar
las dos cosas tiene dos trampas, y las dos están tratadas:

- **Los bordes.** Las marcas de tiempo por palabra de Whisper son imprecisas justo al
  empezar y al acabar una frase, así que la última palabra se cuela en el turno del
  siguiente hablante y parte la frase en dos (`Hablante 1: Nos ha` / `Hablante 3: dicho
  que...`). Un cambio de hablante que sólo aporta una o dos palabras dentro de la misma
  frase casi nunca es real: se absorbe en el bloque vecino, y se repite hasta que no
  quedan fragmentos.
- **La lectura.** Whisper corta un segmento por frase, así que la transcripción salía a
  línea por frase. Ahora las frases seguidas del mismo hablante se unen en un párrafo
  (se corta al cambiar de voz, tras una pausa larga o al llegar a un minuto). Con
  `--sin-agrupar` se recupera el comportamiento antiguo.

## 🎵 Recomendaciones de Audio
Preferible convertir los audios a WAV (mejor mono y a 44100 Hz) con Audacity para mejores resultados de transcripción.

## 📝 Uso del Resultado
Adjunta el resultado en ChatGPT con un prompt similar a este:
```
Resume el texto adjunto y redáctalo como acta de una reunión, hablando directamente de lo que se habló que tenga que ver con cada punto del orden del día:

Estos son los puntos y el minuto del audio en el que comienza a hablarse de ello:
+ Título del tema
```

## 🚀 Cómo usar la aplicación

1. **Ejecuta la aplicación** desde la línea de comandos
2. **Selecciona**: Elige un archivo de audio individual o una carpeta completa
3. **Espera**: Whisper procesará el audio automáticamente
4. **Resultado**: El archivo `_transcripcion.txt` se guarda junto al audio original

## 🔧 Opciones de Ejecución

### Opción 1: Ejecutar directamente
```powershell
# Activar entorno virtual
.venv\Scripts\Activate.ps1

# Diálogo para elegir archivos (con identificación de hablantes)
python transcribe_audio.py

# Directamente por línea de comandos (sin ventanas)
python transcribe_audio.py "C:\ruta\reunion.m4a"

# Sabiendo que son 3 personas, y con sus nombres
python transcribe_audio.py "C:\ruta\reunion.m4a" --hablantes 3 --nombres "Ana,Luis,Pedro"

# Solo transcribir, sin identificar hablantes (más rápido)
python transcribe_audio.py "C:\ruta\audio.mp3" --sin-hablantes
```

Opciones disponibles:

| Opción | Para qué sirve |
|--------|----------------|
| `--modo rapido\|normal\|preciso` | Perfil de velocidad / calidad (por defecto `normal`) |
| `--rapido` | Atajo de `--modo rapido` |
| `--hablantes N` | Número exacto de personas. Mejora bastante la precisión |
| `--min-hablantes N` / `--max-hablantes N` | Acotar el rango si no se sabe el número exacto |
| `--nombres "A,B,C"` | Nombres reales por orden de aparición |
| `--sin-hablantes` | Desactiva la diarización en esa ejecución |
| `--con-hablantes` | La fuerza aunque el modo sea `rapido` |
| `--modelo NOMBRE` | Modelo de Whisper (`tiny`/`base`/`small`/`medium`/`large`) |
| `--procesos N` | Trozos transcritos a la vez. Por defecto, automático según los núcleos |
| `--idioma es` | Fuerza el idioma en vez de detectarlo en cada trozo |
| `--sin-agrupar` | Una línea por frase, sin unir las seguidas del mismo hablante |

También por variables de entorno: `TRANSCRIBIR_DIARIZAR=0` desactiva los hablantes de forma
permanente, `WHISPER_MODEL` cambia el modelo por defecto y `PYANNOTE_MODEL` el de diarización.

### Opción 2: Configuración desde cero
```powershell
# Crear entorno virtual
python -m venv .venv

# Activar entorno
.venv\Scripts\Activate.ps1

# Instalar PyTorch (CPU)
pip install torch torchvision torchaudio --index-url https://download.pytorch.org/whl/cpu

# Instalar dependencias
pip install -r requirements.txt

# Ejecutar aplicación
python transcribe_audio.py
```

### Requisito externo: ffmpeg
Whisper necesita `ffmpeg` instalado y en el PATH:
```powershell
# Con winget
winget install Gyan.FFmpeg

# O con chocolatey
choco install ffmpeg
```

## 📋 Dependencias
- **openai-whisper**: Motor de transcripción con detección automática de idioma
- **pyannote.audio**: Diarización, es decir, quién habla en cada momento
- **pydub 0.25.1**: Procesamiento y manipulación de audio
- **torch + torchaudio**: Backend de inferencia para Whisper y pyannote
- **ffmpeg**: Requerido externamente para decodificación de audio

## 🧠 Modelo de Whisper

El script usa el modelo `small` por defecto (~461 MB). Se descarga automáticamente la primera vez.

| Modelo | Tamaño | Velocidad (CPU) | Precisión |
|--------|--------|-----------------|-----------|
| `tiny` | ~75 MB | Muy rápida | Básica |
| `base` | ~142 MB | Rápida | Buena |
| `small` | ~461 MB | Media | Muy buena |
| `medium` | ~1.5 GB | Lenta | Excelente |
| `large` | ~2.9 GB | Muy lenta | Máxima |

Para cambiar el modelo, usa `--modelo medium` o la variable `WHISPER_MODEL`.

## ⚠️ Notas Técnicas

- **Python 3.12**: Es la versión del `.venv` actual
- **Ejecución local**: No requiere conexión a internet una vez descargados los modelos
- **GPU opcional**: Funciona en CPU; con GPU NVIDIA (CUDA) es significativamente más rápido
- **Coste de la diarización**: Es la parte más lenta y **no se puede trocear** (necesita el
  audio entero para decidir que la voz del minuto 3 y la del minuto 40 son la misma persona).
  Corre en paralelo con la transcripción, así que ya no se suma; pero marca el suelo del
  tiempo total. Si hace falta ir rápido, `--rapido` la quita. Además, al activarla Whisper
  calcula marcas de tiempo por palabra (necesarias para cortar las frases donde cambia el
  hablante), lo que también lo ralentiza algo
- **Reparto de núcleos**: Ni Whisper ni pyannote escalan bien con muchos hilos (muchas
  operaciones sobre tensores pequeños: pasado cierto punto los hilos pierden más tiempo
  sincronizándose que trabajando). Medido sobre los mismos 10 minutos de audio:

  | | 4 hilos | 7 hilos |
  |---|---|---|
  | pyannote (audio entero) | 5 min 30 | **14 min 13** |
  | Whisper (trozo de 5 min) | 2 min 20 | 6 min 40 *(con 3 hilos y pyannote comiéndose 7)* |

  Por eso cada trabajador tiene un tamaño fijo de **4 hilos** (`TRANSCRIBIR_HILOS`) y el
  número de procesos sale de lo que quepa en la máquina, no al revés. Con `--procesos N`
  se pide menos; `--procesos 1` vuelve al comportamiento de un único proceso, que en ese
  caso sí se queda con todos los núcleos
- **Sin torchcodec**: El audio se decodifica con pydub/ffmpeg y se pasa a pyannote ya como
  forma de onda, para evitar que torchcodec busque las DLL de ffmpeg (la build de winget es
  estática y no las trae)
- **Limpieza automática**: Los archivos temporales se eliminan automáticamente

## 📁 Archivos del Proyecto

- `TranscribirAudio.bat` - Lanzador para doble clic (también acepta arrastrar y soltar audios)
- `transcribe_audio.py` - Aplicación principal de línea de comandos
- `diarize.py` - Identificación de hablantes con pyannote.audio
- `requirements.txt` - Dependencias del proyecto
- `.hf_token` - Token de Hugging Face (lo creas tú; no se sube al repo)
