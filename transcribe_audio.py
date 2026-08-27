import argparse
import bisect
import multiprocessing
import os
import sys
import tempfile
import time
import wave


def _ruta_recurso(*partes):
    base = getattr(sys, "_MEIPASS", None) or os.path.dirname(os.path.abspath(__file__))
    return os.path.join(base, *partes)


if getattr(sys, "frozen", False):
    _ffmpeg_dir = _ruta_recurso("ffmpeg")
    if os.path.isdir(_ffmpeg_dir):
        os.environ["PATH"] = _ffmpeg_dir + os.pathsep + os.environ.get("PATH", "")
    _modelos_dir = _ruta_recurso("whisper_models")
else:
    _modelos_dir = None


import numpy as np  # noqa: E402
import torch  # noqa: E402
import whisper  # noqa: E402  — importar tras ajustar PATH
from pydub import AudioSegment  # noqa: E402

# Por defecto se identifica quién habla en cada momento (diarización).
DIARIZAR_POR_DEFECTO = os.environ.get("TRANSCRIBIR_DIARIZAR", "1") != "0"
MODELO_WHISPER = os.environ.get("WHISPER_MODEL", "small")

# Núcleos que torch usaría él solo (los físicos). Es el presupuesto a repartir
# entre los procesos que trabajan en paralelo.
HILOS_TOTALES = torch.get_num_threads()

# Hilos de cada trabajador (un proceso de Whisper, o el de pyannote). Ver la
# explicación y las medidas en _plan_de_trabajo(): subirlo es contraproducente.
HILOS_POR_TRABAJADOR = max(1, int(os.environ.get("TRANSCRIBIR_HILOS", "4")))

# Duración objetivo de cada trozo de audio en el modo paralelo. Whisper procesa
# ventanas de 30 s, así que trozos de 5 min amortizan de sobra la carga del
# modelo y dan grano fino para repartir entre procesos.
TROZO_SEGUNDOS = 300.0
MARGEN_CORTE_SEGUNDOS = 25.0

# Perfiles de ejecución: relación velocidad / calidad.
#   temperaturas: Whisper reintenta un bloque con temperatura más alta cuando
#     sospecha que ha salido mal. Cada reintento cuesta otra pasada completa,
#     así que recortar la lista es de lo que más acelera en audio difícil.
#   contexto: condition_on_previous_text. Da continuidad al texto, pero es el
#     causante de los bucles de repetición (y se pierde igual al trocear).
MODOS = {
    "rapido": {
        "diarizar": False,
        "temperaturas": (0.0,),
        "contexto": False,
        "descripcion": "sin identificar hablantes, decodificación sin reintentos",
    },
    "normal": {
        "diarizar": True,
        "temperaturas": (0.0, 0.2, 0.4),
        "contexto": False,
        "descripcion": "con hablantes, troceado en paralelo",
    },
    "preciso": {
        "diarizar": True,
        "temperaturas": (0.0, 0.2, 0.4, 0.6, 0.8, 1.0),
        "contexto": True,
        "descripcion": "con hablantes, un solo proceso y todos los reintentos",
    },
}

_modelo = None


def obtener_modelo(nombre=None):
    """Carga el modelo de Whisper la primera vez que hace falta."""
    global _modelo
    nombre = nombre or MODELO_WHISPER
    if _modelo is None:
        if _modelos_dir and os.path.isdir(_modelos_dir):
            _modelo = whisper.load_model(nombre, download_root=_modelos_dir)
        else:
            _modelo = whisper.load_model(nombre)
    return _modelo


def _formato_tiempo(segundos):
    s = int(segundos)
    return f"{s // 3600:02}:{(s % 3600) // 60:02}:{s % 60:02}"


def _formato_duracion(segundos):
    m, s = divmod(int(segundos), 60)
    return f"{m}m {s:02}s" if m else f"{s}s"


# --------------------------------------------------------------------------
# Audio
# --------------------------------------------------------------------------

def leer_wav(ruta, inicio_seg=0.0, fin_seg=None):
    """
    Lee un tramo de un WAV PCM y lo devuelve como float32 en [-1, 1].

    Se usa el módulo `wave` de la stdlib en vez de whisper.load_audio() a
    propósito: aquél lanza un ffmpeg por llamada, y aquí se llama una vez por
    trozo. Además permite leer sólo el tramo pedido, sin cargar la hora entera
    de audio en cada proceso.
    """
    with wave.open(ruta, "rb") as w:
        sr = w.getframerate()
        ancho = w.getsampwidth()
        canales = w.getnchannels()
        total = w.getnframes()

        primera = max(0, int(inicio_seg * sr))
        ultima = total if fin_seg is None else min(total, int(fin_seg * sr))
        if ultima <= primera:
            return np.zeros(0, dtype=np.float32), sr

        w.setpos(primera)
        crudo = w.readframes(ultima - primera)

    tipos = {1: np.uint8, 2: np.int16, 4: np.int32}
    if ancho not in tipos:
        raise ValueError(f"Ancho de muestra WAV no soportado: {ancho} bytes")

    datos = np.frombuffer(crudo, dtype=tipos[ancho]).astype(np.float32)
    if ancho == 1:  # PCM de 8 bits es sin signo
        datos = (datos - 128.0) / 128.0
    else:
        datos = datos / float(np.iinfo(tipos[ancho]).max)

    if canales > 1:
        datos = datos.reshape(-1, canales).mean(axis=1)

    return np.ascontiguousarray(datos), sr


def calcular_trozos(muestras, sample_rate, objetivo=TROZO_SEGUNDOS,
                    margen=MARGEN_CORTE_SEGUNDOS):
    """
    Parte el audio en tramos de ~`objetivo` segundos cortando por los silencios.

    Cortar a ciegas cada N minutos parte palabras y frases; buscar el medio
    segundo más silencioso dentro de una ventana de ±`margen` alrededor del
    corte teórico deja las costuras en pausas reales.
    """
    total = len(muestras) / sample_rate
    if total <= objetivo * 1.5:
        return [(0.0, total)]

    paso = 0.5  # resolución del análisis de energía, en segundos
    ancho = int(paso * sample_rate)
    n_ventanas = len(muestras) // ancho
    energia = np.abs(muestras[:n_ventanas * ancho].reshape(n_ventanas, ancho)).mean(axis=1)

    cortes = [0.0]
    objetivo_actual = objetivo
    while objetivo_actual < total - objetivo * 0.5:
        lo = max(0, int((objetivo_actual - margen) / paso))
        hi = min(n_ventanas, int((objetivo_actual + margen) / paso))
        if hi <= lo:
            punto = objetivo_actual
        else:
            punto = (lo + int(np.argmin(energia[lo:hi])) + 0.5) * paso
        if punto <= cortes[-1] + objetivo * 0.5:  # red de seguridad
            punto = objetivo_actual
        cortes.append(punto)
        objetivo_actual = punto + objetivo

    cortes.append(total)
    return list(zip(cortes[:-1], cortes[1:]))


# --------------------------------------------------------------------------
# Procesos de trabajo (han de ser funciones de nivel de módulo: se serializan)
# --------------------------------------------------------------------------

def _worker_transcribir(tarea):
    """Transcribe un trozo del WAV y devuelve sus segmentos ya desplazados."""
    torch.set_num_threads(tarea["hilos"])
    inicio = tarea["inicio"]

    audio, _ = leer_wav(tarea["ruta"], inicio, tarea["fin"])
    t0 = time.monotonic()
    resultado = obtener_modelo(tarea["modelo"]).transcribe(
        audio,
        language=tarea["idioma"],
        word_timestamps=tarea["palabras"],
        temperature=tarea["temperaturas"],
        condition_on_previous_text=tarea["contexto"],
        verbose=None,  # sin barra ni texto: el progreso lo imprime el padre
        fp16=False,
    )

    segmentos = []
    for s in resultado["segments"]:
        palabras = [
            {
                "word": p["word"],
                "start": float(p["start"]) + inicio,
                "end": float(p["end"]) + inicio,
            }
            for p in (s.get("words") or [])
        ]
        segmentos.append({
            "start": float(s["start"]) + inicio,
            "end": float(s["end"]) + inicio,
            "text": s["text"],
            "words": palabras,
        })

    return {
        "indice": tarea["indice"],
        "idioma": resultado.get("language"),
        "segments": segmentos,
        "duracion": time.monotonic() - t0,
    }


def _worker_diarizar(tarea):
    """Ejecuta la diarización en su propio proceso, en paralelo con Whisper."""
    torch.set_num_threads(tarea["hilos"])
    try:
        from diarize import diarizar

        t0 = time.monotonic()
        turnos = diarizar(
            tarea["ruta"],
            num_hablantes=tarea["num_hablantes"],
            min_hablantes=tarea["min_hablantes"],
            max_hablantes=tarea["max_hablantes"],
        )
        return {"turnos": turnos, "duracion": time.monotonic() - t0}
    except Exception as e:  # se devuelve, no se lanza: la transcripción sigue
        return {"error": str(e)}


# --------------------------------------------------------------------------
# Atribución de hablantes
# --------------------------------------------------------------------------

class Turnos:
    """
    Turnos de pyannote indexados para buscar por tiempo sin recorrerlos todos.

    Con una hora de audio hay miles de turnos y decenas de miles de palabras;
    el recorrido lineal por palabra costaba minutos de CPU en Python puro.
    """

    def __init__(self, turnos):
        self.turnos = sorted(turnos, key=lambda t: t[0])
        self.inicios = [t[0] for t in self.turnos]
        self.max_duracion = max((t[1] - t[0] for t in self.turnos), default=0.0)

    def __bool__(self):
        return bool(self.turnos)

    def _ventana(self, inicio, fin, holgura=0.0):
        desde = bisect.bisect_left(self.inicios, inicio - self.max_duracion - holgura)
        hasta = bisect.bisect_right(self.inicios, fin + holgura)
        return max(0, desde), min(len(self.turnos), hasta)

    def etiqueta(self, inicio, fin):
        """Etiqueta del hablante cuyo turno solapa más con [inicio, fin]."""
        desde, hasta = self._ventana(inicio, fin)
        mejor, solape_max = None, 0.0
        for t_ini, t_fin, etq in self.turnos[desde:hasta]:
            solape = min(fin, t_fin) - max(inicio, t_ini)
            if solape > solape_max:
                solape_max, mejor = solape, etq
        if mejor is not None:
            return mejor

        # Sin solape (silencios, risas, solapamientos): el turno más cercano
        centro = (inicio + fin) / 2
        desde, hasta = self._ventana(inicio, fin, holgura=30.0)
        if desde == hasta:  # tramo sin turnos alrededor: se mira la lista entera
            desde, hasta = 0, len(self.turnos)
        mejor, distancia_min = None, float("inf")
        for t_ini, t_fin, etq in self.turnos[desde:hasta]:
            if t_ini <= centro <= t_fin:
                distancia = 0.0
            else:
                distancia = min(abs(centro - t_ini), abs(centro - t_fin))
            if distancia < distancia_min:
                distancia_min, mejor = distancia, etq
        return mejor


# Un cambio de hablante sólo se cree si trae texto suficiente detrás. Por
# debajo de esto casi siempre es ruido de los bordes de las marcas de tiempo.
MIN_PALABRAS_TURNO = 3
MIN_DURACION_TURNO = 1.0


def _mapa_nombres(lineas, nombres=None):
    """
    SPEAKER_xx -> 'Hablante 1' (o el nombre dado), por orden de aparición.

    Se calcula sobre las líneas YA montadas, no sobre los turnos de pyannote:
    esa lista contiene microturnos espurios que no llegan a producir texto y
    desordenaban la correspondencia con --nombres.
    """
    orden = []
    for _, _, etiqueta, _ in lineas:
        if etiqueta not in orden:
            orden.append(etiqueta)
    mapa = {}
    for i, etiqueta in enumerate(orden):
        if nombres and i < len(nombres):
            mapa[etiqueta] = nombres[i]
        else:
            mapa[etiqueta] = f"Hablante {i + 1}"
    return mapa


def _rachas(etiquetas, desde=0, hasta=None):
    """Tramos consecutivos con la misma etiqueta: [(inicio, fin_excluido), ...]."""
    hasta = len(etiquetas) if hasta is None else hasta
    rachas = []
    i = desde
    while i < hasta:
        j = i + 1
        while j < hasta and etiquetas[j] == etiquetas[i]:
            j += 1
        rachas.append((i, j))
        i = j
    return rachas


def _duracion(palabras, i, j):
    return palabras[j - 1]["end"] - palabras[i]["start"]


def _suavizar(palabras, etiquetas, desde, hasta):
    """
    Borra los cambios de hablante que no se sostienen DENTRO de una misma frase.

    Las marcas de tiempo por palabra de Whisper son imprecisas justo en los
    bordes, así que una palabra suelta se cuela en el turno del hablante de al
    lado. Un cambio que sólo aporta una o dos palabras dentro de la misma frase
    casi nunca es real: esa racha se reasigna al vecino.

    Se limita a una frase de Whisper a propósito: un "sí, claro" que ocupa una
    frase entera SÍ suele ser un hablante distinto, y ése no se toca.

    Se repite hasta que no cambie nada: reasignar una racha puede dejar a su
    vecina convertida a su vez en un fragmento.
    """
    cambiado = True
    while cambiado:
        cambiado = False
        rachas = _rachas(etiquetas, desde, hasta)
        if len(rachas) < 2:
            break
        for k, (i, j) in enumerate(rachas):
            if not (j - i < MIN_PALABRAS_TURNO and _duracion(palabras, i, j) < MIN_DURACION_TURNO):
                continue
            anterior = rachas[k - 1] if k > 0 else None
            siguiente = rachas[k + 1] if k + 1 < len(rachas) else None

            if anterior and siguiente and etiquetas[anterior[0]] == etiquetas[siguiente[0]]:
                nueva = etiquetas[anterior[0]]  # intercalada entre dos del mismo
            else:
                candidatas = [r for r in (anterior, siguiente) if r]
                if not candidatas:
                    continue
                # se la queda el vecino con más peso
                mejor = max(candidatas, key=lambda r: _duracion(palabras, *r))
                nueva = etiquetas[mejor[0]]

            if nueva == etiquetas[i]:
                continue
            for x in range(i, j):
                etiquetas[x] = nueva
            cambiado = True
            break

    return etiquetas


FIN_DE_FRASE = (".", "?", "!", "…")
PREMIO_PUNTUACION = 0.15   # segundos de pausa que "vale" un punto final
MARGEN_MOVER = 0.10        # hay que ganar por esto para mover la frontera


def _ajustar_fronteras(palabras, etiquetas, ventana=3):
    """
    Mueve cada cambio de hablante a la pausa más cercana.

    pyannote acierta con QUIÉN habla, pero el instante exacto del relevo lo
    ponen las marcas de tiempo por palabra de Whisper, que se desvían décimas
    de segundo. El resultado son frases partidas por la mitad:

        Hablante 1: ¿Tengo una reunión a la... Nos ha
        Hablante 2: dicho Roberto que...

    La gente no se releva a mitad de palabra: se releva en una pausa. Así que
    la frontera se busca en las `ventana` palabras de alrededor y se planta en
    el hueco más largo (con una ayudita si viene detrás de un signo de
    puntuación). Nunca se vacía una racha: siempre queda al menos una palabra
    a cada lado, así que esto reetiqueta pero no borra turnos.
    """
    rachas = _rachas(etiquetas)
    for k in range(1, len(rachas)):
        frontera = rachas[k][0]
        anterior, nueva = etiquetas[frontera - 1], etiquetas[frontera]
        lo = max(rachas[k - 1][0] + 1, frontera - ventana)
        hi = min(rachas[k][1] - 1, frontera + ventana)
        if hi < lo:
            continue

        def peso_de(j):
            p = palabras[j]["start"] - palabras[j - 1]["end"]
            if palabras[j - 1]["word"].strip().endswith(FIN_DE_FRASE):
                p += PREMIO_PUNTUACION
            return p

        # La frontera sólo se mueve si el sitio nuevo es claramente mejor: ante
        # la duda manda pyannote, que es quien de verdad oye las voces.
        umbral = peso_de(frontera) + MARGEN_MOVER
        mejor, mejor_peso = frontera, umbral
        for j in range(lo, hi + 1):
            peso = peso_de(j)
            if peso > mejor_peso:
                mejor_peso, mejor = peso, j

        if mejor < frontera:  # el relevo era antes: esas palabras son del nuevo
            for x in range(mejor, frontera):
                etiquetas[x] = nueva
        elif mejor > frontera:  # era después: siguen siendo del anterior
            for x in range(frontera, mejor):
                etiquetas[x] = anterior

    return etiquetas


def _lineas_con_hablante(segmentos, turnos):
    """
    Reparte el texto de Whisper entre los hablantes de pyannote.
    Devuelve [(inicio, fin, etiqueta_pyannote, texto), ...].
    """
    palabras, de_segmento, sin_palabras = [], [], []
    for n, segmento in enumerate(segmentos):
        propias = segmento.get("words") or []
        if not propias:
            # Sin marcas por palabra no hay nada que repartir: la frase entera
            # se la lleva el hablante que más solape con ella.
            texto = segmento["text"].strip()
            if texto:
                sin_palabras.append((
                    segmento["start"],
                    segmento["end"],
                    turnos.etiqueta(segmento["start"], segmento["end"]),
                    texto,
                ))
            continue
        for p in propias:
            palabras.append({
                "word": p["word"],
                "start": float(p["start"]),
                "end": float(p["end"]),
            })
            de_segmento.append(n)

    if not palabras:
        return sorted(sin_palabras)

    etiquetas = [turnos.etiqueta(p["start"], p["end"]) for p in palabras]

    # 1) Limpiar el ruido dentro de cada frase, 2) cuadrar los relevos con las
    # pausas. En este orden: si no, el suavizado deshace lo que acaba de cuadrar
    # el ajuste de fronteras.
    inicio = 0
    for i in range(1, len(palabras) + 1):
        if i == len(palabras) or de_segmento[i] != de_segmento[inicio]:
            _suavizar(palabras, etiquetas, inicio, i)
            inicio = i
    _ajustar_fronteras(palabras, etiquetas)

    # Una línea por racha de hablante, cortando también al cambiar de frase
    # para no perder el detalle de las marcas de tiempo (agrupar_lineas las
    # vuelve a unir después en párrafos legibles).
    lineas = []
    actual = []
    for i, palabra in enumerate(palabras):
        corta = actual and (etiquetas[i] != etiquetas[i - 1] or de_segmento[i] != de_segmento[i - 1])
        if corta:
            lineas.append(_montar_linea(palabras, etiquetas, actual))
            actual = []
        actual.append(i)
    if actual:
        lineas.append(_montar_linea(palabras, etiquetas, actual))

    lineas = [l for l in lineas if l[3]]
    return sorted(lineas + sin_palabras)


def _montar_linea(palabras, etiquetas, indices):
    texto = "".join(palabras[i]["word"] for i in indices).strip()
    return (palabras[indices[0]]["start"], palabras[indices[-1]]["end"],
            etiquetas[indices[0]], texto)


def _lineas_sin_hablante(segmentos):
    salida = []
    for s in segmentos:
        texto = s["text"].strip()
        if texto:
            salida.append((s["start"], s["end"], None, texto))
    return salida


def agrupar_lineas(lineas, hueco_max=2.0, duracion_max=60.0):
    """
    Une líneas consecutivas del mismo hablante en párrafos.

    Whisper corta un segmento por frase, así que sin esto la transcripción sale
    a línea por frase y cuesta leerla. El párrafo se corta cuando cambia el
    hablante, cuando hay una pausa larga o cuando se hace demasiado largo (para
    que la marca de tiempo del principio siga siendo útil).
    """
    salida = []
    for ini, fin, etiqueta, texto in lineas:
        if salida:
            p_ini, p_fin, p_etq, p_texto = salida[-1]
            if (p_etq == etiqueta and ini - p_fin <= hueco_max
                    and fin - p_ini <= duracion_max):
                salida[-1] = (p_ini, fin, etiqueta, f"{p_texto} {texto}".strip())
                continue
        salida.append((ini, fin, etiqueta, texto))
    return salida


# --------------------------------------------------------------------------
# Orquestación
# --------------------------------------------------------------------------

def procesos_por_defecto():
    """Procesos de Whisper en paralelo, dejando aire para la diarización."""
    return max(1, min(4, (os.cpu_count() or 4) // 5))


def _plan_de_trabajo(procesos_pedidos, con_diarizacion, n_trozos):
    """
    Reparte los núcleos. Devuelve (procesos, hilos_whisper, hilos_diarizacion).

    Ni Whisper ni pyannote escalan bien con muchos hilos: son muchas
    operaciones sobre tensores pequeños, y a partir de cierto punto los hilos
    pierden más tiempo sincronizándose que trabajando. Medido en un i7-12700H
    (14 núcleos físicos) sobre 10 minutos de reunión:

        pyannote  4 hilos ->  5m 30s      |  Whisper (trozo de 5 min, 4 hilos) -> 2m 20s
        pyannote  7 hilos -> 14m 13s      |  los mismos trozos con 3 hilos     -> 6m 40s

    O sea: repartir en más trabajadores de pocos hilos gana, y pasarse de
    hilos por trabajador se paga carísimo. De ahí el tamaño fijo de
    trabajador y el tope de procesos por lo que cabe en la máquina.
    """
    presupuesto = HILOS_TOTALES - (HILOS_POR_TRABAJADOR if con_diarizacion else 0)
    caben = max(1, presupuesto // HILOS_POR_TRABAJADOR)
    procesos = max(1, min(procesos_pedidos, caben, n_trozos))
    # Un proceso solo (audio corto o --procesos 1) sí puede quedarse con todo
    hilos = HILOS_POR_TRABAJADOR if procesos > 1 else max(HILOS_POR_TRABAJADOR, presupuesto)
    return procesos, hilos, (HILOS_POR_TRABAJADOR if con_diarizacion else 0)


def _transcribir_en_paralelo(ruta_wav, trozos, opciones, hilos, ejecutor):
    """Lanza un proceso por trozo y va recogiendo resultados según terminan."""
    from concurrent.futures import as_completed

    futuros = {}
    for i, (inicio, fin) in enumerate(trozos):
        tarea = dict(opciones, ruta=ruta_wav, indice=i, inicio=inicio, fin=fin, hilos=hilos)
        futuros[ejecutor.submit(_worker_transcribir, tarea)] = (inicio, fin)

    resultados = []
    t0 = time.monotonic()
    for futuro in as_completed(futuros):
        inicio, fin = futuros[futuro]
        resultados.append(futuro.result())
        transcurrido = time.monotonic() - t0
        hechos = len(resultados)
        restante = transcurrido / hechos * (len(trozos) - hechos)
        print(f"  [{hechos}/{len(trozos)}] trozo {_formato_tiempo(inicio)}-{_formato_tiempo(fin)}"
              f" listo · lleva {_formato_duracion(transcurrido)}"
              + (f", quedan ~{_formato_duracion(restante)}" if hechos < len(trozos) else ""),
              flush=True)

    resultados.sort(key=lambda r: r["indice"])
    return resultados


def transcribir_archivo(
    ruta_archivo_audio,
    diarizar_audio=None,
    num_hablantes=None,
    min_hablantes=None,
    max_hablantes=None,
    nombres=None,
    modelo_whisper=None,
    modo="normal",
    procesos=None,
    idioma=None,
    agrupar=True,
    preguntar_segmento=None,  # compatibilidad con la firma antigua
):
    """
    Transcribe un archivo de audio con Whisper e identifica quién dice cada cosa.
    """
    tmp_path = None
    try:
        perfil = MODOS[modo]
        if diarizar_audio is None:
            diarizar_audio = perfil["diarizar"] and DIARIZAR_POR_DEFECTO

        if procesos is None:
            procesos = 1 if modo == "preciso" else procesos_por_defecto()
        procesos = max(1, int(procesos))

        nombre_archivo = os.path.basename(ruta_archivo_audio)
        directorio_padre = os.path.dirname(os.path.abspath(ruta_archivo_audio))

        print(f"Procesando archivo: {nombre_archivo}")
        arranque = time.monotonic()

        audio = AudioSegment.from_file(ruta_archivo_audio)
        duracion_total = len(audio) / 1000

        # Convertir a WAV mono 16 kHz: lo usan tanto Whisper como pyannote
        audio_mono = audio.set_channels(1).set_frame_rate(16000)
        del audio
        with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as tmp:
            tmp_path = tmp.name
            audio_mono.export(tmp_path, format="wav")
        muestras = np.array(audio_mono.get_array_of_samples(), dtype=np.float32)
        del audio_mono

        trozos = calcular_trozos(muestras, 16000) if procesos > 1 else [(0.0, duracion_total)]
        del muestras

        en_paralelo, hilos, hilos_diarizacion = _plan_de_trabajo(
            procesos, diarizar_audio, len(trozos))
        print("Archivo cargado:")
        print(f"  - Duración total: {duracion_total / 60:.1f} minutos ({duracion_total:.0f} segundos)")
        print(f"  - Modo '{modo}': {perfil['descripcion']}")
        print(f"  - Modelo {modelo_whisper or MODELO_WHISPER}, {len(trozos)} trozo(s),"
              f" {en_paralelo} proceso(s) x {hilos} hilos"
              + (f" (+{hilos_diarizacion} hilos para los hablantes)" if diarizar_audio else ""))

        nombre_base = os.path.splitext(nombre_archivo)[0]
        nombre_archivo_transcripcion = nombre_base + "_transcripcion.txt"
        ruta_archivo_transcripcion = os.path.join(directorio_padre, nombre_archivo_transcripcion)
        print(f"Archivo de salida: {nombre_archivo_transcripcion}")

        opciones = {
            "modelo": modelo_whisper,
            "idioma": idioma,
            "palabras": bool(diarizar_audio),  # sólo hacen falta para cortar por hablante
            "temperaturas": perfil["temperaturas"],
            "contexto": perfil["contexto"],
        }

        turnos, aviso_diarizacion, segmentos, idiomas = [], None, [], []

        if en_paralelo > 1 or diarizar_audio:
            # La diarización y la transcripción son independientes: se lanzan a
            # la vez y cada una se queda con su parte de los núcleos. En serie,
            # una hora de audio costaba la suma de las dos.
            from concurrent.futures import ProcessPoolExecutor

            trabajadores = en_paralelo + (1 if diarizar_audio else 0)
            with ProcessPoolExecutor(max_workers=trabajadores) as ejecutor:
                futuro_diarizacion = None
                if diarizar_audio:
                    print("\nIdentificando hablantes en paralelo (pyannote)...")
                    futuro_diarizacion = ejecutor.submit(_worker_diarizar, {
                        "ruta": tmp_path,
                        "num_hablantes": num_hablantes,
                        "min_hablantes": min_hablantes,
                        "max_hablantes": max_hablantes,
                        "hilos": hilos_diarizacion,
                    })

                print("\nIniciando transcripción con Whisper...")
                for r in _transcribir_en_paralelo(tmp_path, trozos, opciones, hilos, ejecutor):
                    segmentos.extend(r["segments"])
                    if r["idioma"]:
                        idiomas.append(r["idioma"])

                if futuro_diarizacion:
                    if not futuro_diarizacion.done():
                        print("Esperando a que termine la identificación de hablantes...")
                    salida = futuro_diarizacion.result()
                    if "error" in salida:
                        aviso_diarizacion = salida["error"]
                        print("\n[!] No se pudo identificar hablantes, se continúa sin ellos:")
                        print(f"    {aviso_diarizacion}\n")
                    else:
                        turnos = salida["turnos"]
                        print(f"Hablantes identificados en {_formato_duracion(salida['duracion'])}.")
        else:
            # Un solo proceso: se transcribe del tirón, con el progreso de Whisper
            print("\nIniciando transcripción con Whisper...")
            audio_np, _ = leer_wav(tmp_path)
            resultado = obtener_modelo(modelo_whisper).transcribe(
                audio_np,
                language=idioma,
                verbose=True,
                word_timestamps=opciones["palabras"],
                temperature=opciones["temperaturas"],
                condition_on_previous_text=opciones["contexto"],
                fp16=False,
            )
            segmentos = resultado["segments"]
            if resultado.get("language"):
                idiomas.append(resultado["language"])

        # Volcado
        indice = Turnos(turnos) if turnos else None
        lineas = _lineas_con_hablante(segmentos, indice) if indice else _lineas_sin_hablante(segmentos)
        if agrupar:
            # Sin hablantes no hay nada que corte el párrafo cuando cambia la voz,
            # así que se agrupa mucho más corto para no mezclar a todo el mundo.
            if indice:
                lineas = agrupar_lineas(lineas, hueco_max=2.0, duracion_max=60.0)
            else:
                lineas = agrupar_lineas(lineas, hueco_max=1.0, duracion_max=30.0)
        mapa = _mapa_nombres(lineas, nombres) if indice else {}

        idioma_detectado = max(set(idiomas), key=idiomas.count) if idiomas else "desconocido"
        with open(ruta_archivo_transcripcion, "w", encoding="utf-8") as f:
            f.write(f"[Idioma principal detectado: {idioma_detectado}]\n")
            if mapa:
                f.write(f"[Hablantes identificados: {len(mapa)} - {', '.join(mapa.values())}]\n")
            elif aviso_diarizacion:
                f.write(f"[Sin identificacion de hablantes: {aviso_diarizacion.splitlines()[0]}]\n")
            f.write("\n")

            for ini, fin, etiqueta, texto in lineas:
                marca = f"[{_formato_tiempo(ini)} -> {_formato_tiempo(fin)}]"
                if mapa:
                    f.write(f"{marca} {mapa.get(etiqueta, '?')}: {texto}\n")
                else:
                    f.write(f"{marca} {texto}\n")

        total = time.monotonic() - arranque
        print(f"\nTranscripción completa guardada en: {ruta_archivo_transcripcion}")
        print(f"Idioma principal detectado: {idioma_detectado}")
        if mapa:
            print(f"Hablantes: {', '.join(mapa.values())}")
        print(f"Tiempo total: {_formato_duracion(total)}"
              f" ({duracion_total / total:.1f}x tiempo real)")
        return True

    except Exception as e:
        print(f"Error al procesar el archivo {ruta_archivo_audio}: {e}")
        return False
    finally:
        if tmp_path and os.path.exists(tmp_path):
            try:
                os.remove(tmp_path)
            except OSError:
                pass


def seleccionar_archivos():
    import tkinter as tk
    from tkinter import filedialog

    root = tk.Tk()
    root.withdraw()
    rutas = filedialog.askopenfilenames(
        title="Seleccionar archivo(s) de audio (Ctrl/Shift para múltiples)",
        filetypes=[
            ("Archivos de audio", "*.mp3 *.wav *.ogg *.flac *.opus *.m4a *.wma *.aac"),
            ("Todos los archivos", "*.*"),
        ],
    )
    root.destroy()
    return list(rutas)


def _parsear_argumentos(argv=None):
    p = argparse.ArgumentParser(
        description="Transcribe audio con Whisper e identifica quién habla (pyannote).",
    )
    p.add_argument("audios", nargs="*", help="Rutas de audio. Sin argumentos abre un diálogo.")
    p.add_argument("--modo", choices=sorted(MODOS), default="normal",
                   help="rapido (sin hablantes) | normal (por defecto) | preciso (lento).")
    p.add_argument("--rapido", action="store_true", help="Atajo de --modo rapido.")
    p.add_argument("--sin-hablantes", action="store_true",
                   help="Desactiva la identificación de hablantes (acelera mucho).")
    p.add_argument("--con-hablantes", action="store_true",
                   help="Fuerza la identificación de hablantes también en modo rápido.")
    p.add_argument("--hablantes", type=int, metavar="N",
                   help="Número exacto de personas (mejora bastante la precisión).")
    p.add_argument("--min-hablantes", type=int, metavar="N")
    p.add_argument("--max-hablantes", type=int, metavar="N")
    p.add_argument("--nombres", metavar="A,B,C",
                   help="Nombres reales por orden de aparición.")
    p.add_argument("--modelo", metavar="NOMBRE",
                   help="Modelo de Whisper (tiny/base/small/medium/large).")
    p.add_argument("--procesos", type=int, metavar="N",
                   help="Trozos de audio transcritos a la vez (por defecto, automático).")
    p.add_argument("--idioma", metavar="XX",
                   help="Fuerza el idioma (es, en...) en vez de detectarlo por trozo.")
    p.add_argument("--sin-agrupar", action="store_true",
                   help="Una línea por frase, sin unir las del mismo hablante.")
    return p.parse_args(argv)


def main(argv=None):
    args = _parsear_argumentos(argv)
    modo = "rapido" if args.rapido else args.modo

    # Sin esto, al redirigir la salida a un fichero (lo normal en ejecuciones
    # largas en segundo plano) el progreso no aparece hasta el final.
    try:
        sys.stdout.reconfigure(line_buffering=True)
    except (AttributeError, OSError):
        pass

    print("=== Transcriptor de Audio (Whisper + pyannote) ===")
    print(f"Modo '{modo}': {MODOS[modo]['descripcion']}")

    archivos = args.audios or seleccionar_archivos()
    if not archivos:
        print("No se seleccionó ningún archivo. Saliendo...")
        return 1

    nombres = [n.strip() for n in args.nombres.split(",")] if args.nombres else None

    diarizar_audio = None
    if args.sin_hablantes:
        diarizar_audio = False
    elif args.con_hablantes:
        diarizar_audio = True

    total = len(archivos)
    print(f"Se seleccionaron {total} archivo(s).")

    exitosos = 0
    for i, ruta in enumerate(archivos, 1):
        print(f"\n{'=' * 60}")
        print(f"[{i}/{total}] {os.path.basename(ruta)}")
        print(f"{'=' * 60}")
        if transcribir_archivo(
            ruta,
            diarizar_audio=diarizar_audio,
            num_hablantes=args.hablantes,
            min_hablantes=args.min_hablantes,
            max_hablantes=args.max_hablantes,
            nombres=nombres,
            modelo_whisper=args.modelo,
            modo=modo,
            procesos=args.procesos,
            idioma=args.idioma,
            agrupar=not args.sin_agrupar,
        ):
            exitosos += 1

    print(f"\nResumen: {exitosos}/{total} transcripciones completadas correctamente.")
    return 0 if exitosos == total else 1


if __name__ == "__main__":
    multiprocessing.freeze_support()  # necesario para el .exe de PyInstaller
    sys.exit(main())
