# TranscribirAudio

Transcribe archivos de audio a texto con **Whisper**, en local y sobre CPU, usando
[faster-whisper](https://github.com/SYSTRAN/faster-whisper) (el mismo modelo de OpenAI,
ejecutado con CTranslate2: varias veces más rápido y sin `torch`).

- **Rápido y con calidad a la vez**: un solo modo, sin menús de opciones. 10 min de audio en ~40 s
  en un i7-12700H (1 h de audio, unos 4 min).
- **Detección automática de idioma**, o idioma fijo si se prefiere.
- **Cualquier formato**: MP3, M4A, WAV, FLAC, OGG, OPUS, WMA, AAC, MP4… (sin ffmpeg aparte).
- **Marcas de tiempo** por párrafo.
- **Cola de audios**: se añaden los que quieras, también mientras transcribe.
- **Todo local**: el audio y el texto no salen del equipo. Solo se usa internet la primera
  vez, para descargar el modelo.

## Para quien recibe el programa

1. Descomprime `TranscribirAudio.zip` donde quieras (escritorio, Documentos…).
2. Doble clic en **`TranscribirAudio.exe`**.
   - **La primera vez** se abre la pestaña *Modelos* con el recomendado (`small`, ~460 MB)
     seleccionado: pulsa *Descargar*. No hace falta cuenta ni registrarse en ningún sitio.
   - **A partir de ahí**, al abrirla se pide directamente elegir los audios (varios con
     Ctrl/Mayús) y se ponen a transcribir.
3. El texto se va escribiendo en `<nombre>_transcripcion.txt`, junto a cada audio (o en la
   carpeta que elijas).

La ventana tiene dos pestañas:

- **Transcribir**: la cola de audios (se pueden añadir más mientras trabaja), el progreso
  con el tiempo que queda, y un registro donde aparece el texto según se transcribe. Doble
  clic en un audio terminado abre su transcripción. *Detener* para en la frase en curso: el
  `.txt` se queda con lo hecho, marcado como incompleto, y el audio vuelve a la cola.
- **Modelos**: qué modelos hay y cuáles están descargados. Desde aquí se descargan, se
  borran y se elige con ★ el que se usa.

El modelo, el idioma y la carpeta de destino se recuerdan en `TranscribirAudio.ini`.
También se pueden arrastrar audios sobre el `.exe`: entran directos en la cola.

## ⚡ Cómo transcribe

El audio se parte en trozos (cortando en los silencios, para no partir frases) y se
transcriben 4 a la vez, cada uno con 4 hilos. Con más hilos por trabajador se pierde más
sincronizándolos de lo que se gana.

La parte "con calidad" está en los parámetros de Whisper:

| Parámetro | Por qué |
|---|---|
| `beam_size=5` | Frente a la búsqueda voraz cuesta ~20 % más y se nota en el texto |
| Reintentos a temperatura 0.2 y 0.4 | Solo si un tramo sale mal (repetitivo o poco probable). En audio limpio casi nunca saltan |
| Filtro de voz (VAD) | Salta los silencios: acelera y evita frases inventadas en los huecos |
| Sin `condition_on_previous_text` | Es el causante de los bucles de repetición |

Medido sobre 10 minutos de audio en español (i7-12700H, 14 núcleos, sin GPU):

| | Tiempo | Notas |
|---|---|---|
| Versión anterior (openai-whisper, modo rápido) | 87 s | Búsqueda voraz, sin reintentos |
| **Ahora, `small`** | **38 s** | Mejor texto que antes |
| Ahora, `large-v3-turbo` | 98 s | Claramente mejor: puntuación, diálogos, palabras raras |

## 🧠 Modelos

Hay muchos modelos compatibles y se pueden intercambiar sin tocar nada más:

| Modelo | Descarga | Para qué |
|---|---|---|
| `tiny` / `base` | 75 / 145 MB | Muy rápidos, calidad pobre o justa |
| **`small`** *(por defecto)* | 460 MB | Equilibrio velocidad/calidad |
| `medium` | 1,5 GB | Mejor que small, bastante más lento |
| `large-v3-turbo` (o `turbo`) | 1,6 GB | Casi como large-v3; 2,5x más lento que small |
| `large-v3` | 3 GB | Máxima calidad, muy lento en CPU |

Los terminados en `.en` y los `distil-*` **solo sirven para inglés**.

La lista sale de la propia faster-whisper, así que al actualizarla aparecen los modelos
nuevos que incorpore. Además vale **cualquier modelo Whisper en formato CTranslate2**
publicado en Hugging Face (se buscan en
<https://huggingface.co/models?library=ctranslate2&search=whisper>), indicándolo como
`usuario/modelo` en la pestaña *Modelos* o en el `.ini`. El programa comprueba que sea compatible
(que traiga `model.bin` y `tokenizer.json`) antes de descargarlo.

Cada modelo se guarda en `modelos/<usuario>--<modelo>/`. Se descarga fichero a fichero por
HTTPS, sin la caché de `huggingface_hub`, a propósito: las rutas de esa caché pasan con
facilidad de los 260 caracteres que admite Windows y la descarga fallaba.

## 🔧 Línea de comandos

El motor (`transcribe_audio.py`) también funciona sin ventana. Así lo usa el vigilante de Docker:

```powershell
.venv\Scripts\python.exe transcribe_audio.py "C:\ruta\reunion.m4a"
.venv\Scripts\python.exe transcribe_audio.py --modelo turbo --salida D:\Actas reunion.m4a
.venv\Scripts\python.exe transcribe_audio.py --listar-modelos
```

| Opción | Para qué sirve |
|--------|----------------|
| `--modelo NOMBRE` | Modelo para esta ejecución: nombre corto (`small`, `turbo`…) o `usuario/modelo` |
| `--idioma es` | Fuerza el idioma en vez de detectarlo |
| `--salida CARPETA` | Deja los `.txt` ahí en vez de junto a cada audio |
| `--procesos N` | Trozos transcritos a la vez (por defecto, según los núcleos) |
| `--sin-agrupar` | Una línea por frase, sin unirlas en párrafos |
| `--listar-modelos` | Muestra los modelos, cuál está en uso y cuáles están descargados |

Prioridad del modelo: `--modelo` > variable `WHISPER_MODEL` > `TranscribirAudio.ini` > `small`.
Otras variables: `TRANSCRIBIR_HILOS` (hilos por trabajador, 4) y `TRANSCRIBIR_MODELOS`
(otra carpeta para los modelos).

## 🛠️ Desarrollo

```powershell
py -3 -m venv .venv
.venv\Scripts\python.exe -m pip install -r requirements.txt

.\TranscribirAudio.bat      # abre la aplicación (acepta audios arrastrados encima)
```

### Generar el ejecutable

```powershell
.\build.ps1          # dist\TranscribirAudio\TranscribirAudio.exe
.\build.ps1 -Clean   # borrando antes build\ y dist\
.\build.ps1 -Zip     # además, dist\TranscribirAudio.zip para repartir (~95 MB)
```

- El build **conserva** la carpeta `modelos` y el `.ini` que haya en `dist\TranscribirAudio`,
  para no volver a descargar cientos de MB en cada compilación.
- El ZIP va **sin modelos** y con la configuración de fábrica: cada persona se baja el
  suyo la primera vez.

## 📝 Uso del resultado

Adjunta el `.txt` a tu IA de confianza con un prompt como este:

```
Resume el texto adjunto y redáctalo como acta de una reunión, hablando directamente de lo
que se habló que tenga que ver con cada punto del orden del día:

Estos son los puntos y el minuto del audio en el que comienza a hablarse de ello:
+ Título del tema
```

## ⚠️ Notas

- **Sin GPU**: funciona en CPU con cuantización int8. Con una NVIDIA iría más rápido, pero
  habría que añadir las librerías de CUDA.
- **`vigilante/`**: versión en Docker que transcribe sola lo que aparezca en una carpeta de
  Nextcloud. Ver [vigilante/LEEME.md](vigilante/LEEME.md).

## 📁 Archivos del proyecto

- `app.py`: la aplicación de ventana (cola, progreso, registro y modelos)
- `transcribe_audio.py`: el motor de transcripción, usable también por línea de comandos
- `gestor_modelos.py`: catálogo, descarga y configuración, compartidos por los dos
- `TranscribirAudio.ini`: configuración de fábrica (modelo, idioma, carpeta de destino)
- `TranscribirAudio.bat`: lanzador para desarrollo
- `TranscribirAudio.spec` / `build.ps1`: empaquetado con PyInstaller
- `modelos/`: modelos descargados (no se sube al repo)
