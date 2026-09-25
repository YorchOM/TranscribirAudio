"""
Transcribe audio a texto con faster-whisper, en local y sobre CPU.

Es el motor: lo usa la aplicación de ventana (app.py) y también funciona
solo, por línea de comandos (así lo usa el vigilante de Docker).

Si el modelo elegido no está descargado, lo descarga la primera vez (público,
sin cuenta ni token) a la carpeta `modelos/`, junto al programa. A partir de
ahí funciona sin red.
"""

import argparse
import os
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed

import numpy as np
from faster_whisper import WhisperModel, decode_audio

import gestor_modelos as gm

SAMPLE_RATE = 16000

# Hilos de cada trabajador. Medido en un i7-12700H sobre 10 minutos de audio:
# 4 trabajadores x 4 hilos -> 39 s; 5 x 3 -> 42 s. Con más hilos por trabajador
# se pierde más en sincronizarlos que lo que se gana.
HILOS_POR_TRABAJADOR = max(1, int(os.environ.get("TRANSCRIBIR_HILOS", "4")))

# Parámetros de decodificación: la parte de "con calidad".
#   beam_size=5: frente a la búsqueda voraz (1) cuesta ~20% más y se nota en
#     el texto ("leyó" en vez de "lello", palabras largas bien escritas).
#   temperaturas: si un tramo sale mal (repetitivo o poco probable) se
#     reintenta con más temperatura. En audio limpio casi nunca salta.
#   vad_filter: salta los silencios antes de transcribir. Acelera y evita que
#     Whisper se invente frases en los huecos.
#   condition_on_previous_text=False: es el causante de los bucles de
#     repetición, y al trocear se pierde igualmente.
OPCIONES_WHISPER = {
    "beam_size": 5,
    "temperature": (0.0, 0.2, 0.4),
    "vad_filter": True,
    "condition_on_previous_text": False,
}

# Duración máxima de cada trozo. Los trozos se transcriben a la vez en
# trabajadores independientes; cinco minutos dan grano fino para repartir.
TROZO_SEGUNDOS = 300.0
TROZO_MINIMO_SEGUNDOS = 60.0
MARGEN_CORTE_SEGUNDOS = 25.0


def _formato_tiempo(segundos):
    s = int(segundos)
    return f"{s // 3600:02}:{(s % 3600) // 60:02}:{s % 60:02}"


def _formato_duracion(segundos):
    m, s = divmod(int(segundos), 60)
    return f"{m}m {s:02}s" if m else f"{s}s"


# --------------------------------------------------------------------------
# Modelo
# --------------------------------------------------------------------------

def cargar_modelo(nombre, trabajadores, hilos):
    """
    Carga el modelo (descargándolo si hace falta).

    num_workers permite que varios hilos de Python transcriban a la vez con el
    mismo modelo: CTranslate2 suelta el GIL mientras calcula.
    """
    return WhisperModel(
        gm.descargar_modelo(nombre),
        device="cpu",
        compute_type="int8",
        cpu_threads=hilos,
        num_workers=trabajadores,
    )


# --------------------------------------------------------------------------
# Audio
# --------------------------------------------------------------------------

def calcular_trozos(muestras, trabajadores, sample_rate=SAMPLE_RATE,
                    margen=MARGEN_CORTE_SEGUNDOS):
    """
    Parte el audio en tramos cortando por los silencios.

    El tamaño se elige para que haya al menos un trozo por trabajador (un
    audio de 10 minutos con 4 trabajadores sale en 4 trozos de 2,5 min), sin
    pasar de TROZO_SEGUNDOS ni bajar de TROZO_MINIMO_SEGUNDOS.

    Cortar a ciegas parte palabras y frases; buscar el medio segundo más
    silencioso dentro de ±`margen` alrededor del corte teórico deja las
    costuras en pausas reales.
    """
    total = len(muestras) / sample_rate
    objetivo = max(TROZO_MINIMO_SEGUNDOS, min(TROZO_SEGUNDOS, total / trabajadores))
    if trabajadores <= 1 or total <= objetivo * 1.5:
        return [(0.0, total)]
    margen = min(margen, objetivo / 4)

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
# Transcripción
# --------------------------------------------------------------------------

class Cancelado(Exception):
    """Se pidió detener la transcripción."""


def _transcribir_trozo(modelo, muestras, inicio, fin, idioma, lineas, al_segmento, cancelar):
    """
    Transcribe un tramo, dejando sus frases (con el tiempo absoluto) en `lineas`.

    transcribe() devuelve un generador: el trabajo se hace al recorrerlo, frase
    a frase. Por eso se puede avisar de cada una según sale y parar entre dos.
    """
    if cancelar is not None and cancelar.is_set():
        raise Cancelado()
    tramo = muestras[int(inicio * SAMPLE_RATE):int(fin * SAMPLE_RATE)]
    segmentos, _ = modelo.transcribe(tramo, language=idioma, **OPCIONES_WHISPER)
    for s in segmentos:
        if cancelar is not None and cancelar.is_set():
            raise Cancelado()
        linea = (s.start + inicio, s.end + inicio, s.text.strip())
        lineas.append(linea)
        al_segmento(linea)


def detectar_idioma(modelo, muestras):
    """Detecta el idioma una vez para todo el audio, mirando varios tramos."""
    idioma, probabilidad, _ = modelo.detect_language(
        muestras, vad_filter=True, language_detection_segments=3)
    return idioma, probabilidad


def agrupar_lineas(lineas, hueco_max=1.0, duracion_max=30.0):
    """
    Une frases seguidas en párrafos.

    Whisper corta un segmento por frase, así que sin esto la transcripción sale
    a línea por frase. El párrafo se corta tras una pausa o cuando se hace
    largo, para que la marca de tiempo del principio siga siendo útil.
    """
    salida = []
    for ini, fin, texto in lineas:
        if salida:
            p_ini, p_fin, p_texto = salida[-1]
            if ini - p_fin <= hueco_max and fin - p_ini <= duracion_max:
                salida[-1] = (p_ini, fin, f"{p_texto} {texto}".strip())
                continue
        salida.append((ini, fin, texto))
    return salida


def trabajadores_por_defecto():
    """Trabajadores en paralelo según los núcleos, con un tope de 4."""
    return max(1, min(4, (os.cpu_count() or 4) // HILOS_POR_TRABAJADOR))


def ruta_de_salida(ruta_audio, carpeta_salida=None):
    """<nombre>_transcripcion.txt junto al audio, o en `carpeta_salida`."""
    nombre = os.path.splitext(os.path.basename(ruta_audio))[0] + "_transcripcion.txt"
    carpeta = carpeta_salida or os.path.dirname(os.path.abspath(ruta_audio))
    return os.path.join(carpeta, nombre)


def _escribir(ruta, idioma, lineas, agrupar, aviso=None):
    """
    (Re)escribe el .txt con las frases dadas, ordenadas.

    Se escribe en un temporal y se renombra: quien tenga el .txt abierto
    mientras avanza nunca lo ve a medio escribir.
    """
    lineas = sorted(l for l in lineas if l[2])
    if agrupar:
        lineas = agrupar_lineas(lineas)
    temporal = ruta + ".tmp"
    with open(temporal, "w", encoding="utf-8") as f:
        f.write(f"[Idioma principal detectado: {idioma}]\n")
        if aviso:
            f.write(f"[{aviso}]\n")
        f.write("\n")
        for ini, fin, texto in lineas:
            f.write(f"[{_formato_tiempo(ini)} -> {_formato_tiempo(fin)}] {texto}\n")
    os.replace(temporal, ruta)


def transcribir(ruta_audio, modelo, trabajadores, idioma=None, agrupar=True,
                carpeta_salida=None, al_segmento=None, al_progreso=None,
                avisar=print, cancelar=None):
    """
    Transcribe un archivo de audio a su .txt. Devuelve un resumen (dict).

    El .txt se va escribiendo sobre la marcha: cada vez que se completa un
    tramo inicial seguido del audio, se vuelca. Si se detiene (`cancelar`, un
    threading.Event) o falla, se queda lo hecho, marcado como incompleto, y
    se lanza Cancelado o el error.

    al_segmento(linea): cada frase según sale, (inicio, fin, texto). Los trozos
        van en paralelo, así que llegan mezcladas en el tiempo; el .txt no.
    al_progreso(fraccion): de 0 a 1.
    avisar(texto): mensajes de estado.
    """
    import threading

    ruta_salida = ruta_de_salida(ruta_audio, carpeta_salida)
    if carpeta_salida:
        os.makedirs(carpeta_salida, exist_ok=True)
    arranque = time.monotonic()

    avisar("Leyendo el audio...")
    # PyAV decodifica cualquier formato y lo deja en mono a 16 kHz
    muestras = decode_audio(ruta_audio, sampling_rate=SAMPLE_RATE)
    duracion = len(muestras) / SAMPLE_RATE
    trozos = calcular_trozos(muestras, trabajadores)
    avisar(f"Duración: {_formato_duracion(duracion)}, {len(trozos)} trozo(s),"
           f" {min(trabajadores, len(trozos))} a la vez")

    if not idioma:
        idioma, probabilidad = detectar_idioma(modelo, muestras)
        avisar(f"Idioma detectado: {idioma} ({100 * probabilidad:.0f}%)")

    cerrojo = threading.Lock()
    avance = [0.0] * len(trozos)          # segundos hechos de cada trozo
    lineas = [[] for _ in trozos]         # frases de cada trozo
    completos = [False] * len(trozos)

    def receptor(i):
        inicio = trozos[i][0]

        def recibir(linea):
            with cerrojo:
                avance[i] = max(avance[i], linea[1] - inicio)
                hecho = sum(avance)
            if al_segmento:
                al_segmento(linea)
            if al_progreso and duracion:
                al_progreso(min(1.0, hecho / duracion))
        return recibir

    def prefijo():
        """Frases de los trozos completos seguidos desde el principio, y hasta dónde llegan."""
        hechas, hasta = [], 0.0
        for i, completo in enumerate(completos):
            if not completo:
                return hechas + lineas[i], (lineas[i][-1][1] if lineas[i] else hasta), False
            hechas += lineas[i]
            hasta = trozos[i][1]
        return hechas, hasta, True

    ejecutor = ThreadPoolExecutor(max_workers=trabajadores)
    try:
        futuros = {
            ejecutor.submit(_transcribir_trozo, modelo, muestras, ini, fin, idioma,
                            lineas[i], receptor(i), cancelar): i
            for i, (ini, fin) in enumerate(trozos)
        }
        for futuro in as_completed(futuros):
            i = futuros[futuro]
            futuro.result()
            completos[i] = True
            with cerrojo:
                avance[i] = trozos[i][1] - trozos[i][0]
            hechas, hasta, terminado = prefijo()
            if not terminado:
                _escribir(ruta_salida, idioma, hechas, agrupar,
                          f"Transcripción en curso: hecho hasta {_formato_tiempo(hasta)}"
                          f" de {_formato_tiempo(duracion)}")
    except BaseException as e:
        if cancelar is not None:
            cancelar.set()  # que paren también los demás trozos
        ejecutor.shutdown(wait=True, cancel_futures=True)
        hechas, hasta, _ = prefijo()
        motivo = "detenida" if isinstance(e, Cancelado) else "interrumpida por un error"
        if hechas:
            _escribir(ruta_salida, idioma, hechas, agrupar,
                      f"Transcripción INCOMPLETA: {motivo} en {_formato_tiempo(hasta)}"
                      f" de {_formato_tiempo(duracion)}")
        raise
    ejecutor.shutdown(wait=True)

    _escribir(ruta_salida, idioma, [l for grupo in lineas for l in grupo], agrupar)
    if al_progreso:
        al_progreso(1.0)
    tiempo = time.monotonic() - arranque
    return {"ruta": ruta_salida, "idioma": idioma, "duracion": duracion, "tiempo": tiempo}


# --------------------------------------------------------------------------
# Línea de comandos (la usan el vigilante de Docker y el desarrollo; la
# aplicación de ventana es app.py)
# --------------------------------------------------------------------------

def listar_modelos(actual):
    print("Modelos para español y otros idiomas (se descargan solos la primera vez):")
    print()
    ingles = []
    for m in gm.catalogo():
        if m["solo_ingles"]:
            ingles.append(m["nombre"])
            continue
        marcas = [x for x, si in (("EN USO", gm.mismo_modelo(m["nombre"], actual)),
                                  ("descargado", m["descargado"])) if si]
        print(f"  {m['nombre']:16} {m['nota']:48} {', '.join(marcas)}")
    print()
    print(f"Solo inglés: {', '.join(ingles)}")
    print()
    print(f"El de por defecto se elige en la aplicación (pestaña Modelos) o en {gm.ruta_ini()}")


def _parsear_argumentos(argv=None):
    p = argparse.ArgumentParser(description="Transcribe audio a texto con Whisper, en local.")
    p.add_argument("audios", nargs="+", help="Rutas de audio.")
    p.add_argument("--modelo", metavar="NOMBRE",
                   help="Modelo de Whisper: nombre corto (small, medium, turbo...) o repositorio"
                        f" 'usuario/modelo'. Por defecto, el de {gm.NOMBRE_INI}.")
    p.add_argument("--idioma", metavar="XX",
                   help="Fuerza el idioma (es, en...) en vez de detectarlo.")
    p.add_argument("--salida", metavar="CARPETA",
                   help="Carpeta para los .txt (por defecto, junto a cada audio).")
    p.add_argument("--procesos", type=int, metavar="N",
                   help="Trozos transcritos a la vez (por defecto, según los núcleos).")
    p.add_argument("--sin-agrupar", action="store_true",
                   help="Una línea por frase, sin unirlas en párrafos.")
    return p.parse_args(argv)


def main(argv=None):
    argv = sys.argv[1:] if argv is None else argv
    if "--listar-modelos" in argv:
        listar_modelos(gm.modelo_configurado())
        return 0
    args = _parsear_argumentos(argv)

    # Sin esto, al redirigir la salida a un fichero el progreso no aparece
    # hasta el final.
    try:
        sys.stdout.reconfigure(line_buffering=True)
    except (AttributeError, OSError):
        pass

    # Prioridad: línea de comandos > variable de entorno > .ini > por defecto
    config = gm.leer_configuracion()
    nombre_modelo = args.modelo or gm.modelo_configurado()
    idioma = args.idioma or config.get("idioma") or None
    salida = args.salida or config.get("carpeta_salida") or None

    trabajadores = max(1, args.procesos or trabajadores_por_defecto())
    print(f"Modelo {nombre_modelo}, {trabajadores} trabajador(es) x {HILOS_POR_TRABAJADOR} hilos")
    modelo = cargar_modelo(nombre_modelo, trabajadores, HILOS_POR_TRABAJADOR)

    exitosos = 0
    for i, ruta in enumerate(args.audios, 1):
        print(f"\n[{i}/{len(args.audios)}] {os.path.basename(ruta)}")
        visto = [-1]

        def progreso(fraccion):
            decena = int(fraccion * 10)
            if decena > visto[0]:
                visto[0] = decena
                print(f"  {decena * 10}%", flush=True)

        try:
            r = transcribir(ruta, modelo, trabajadores, idioma=idioma,
                            agrupar=not args.sin_agrupar, carpeta_salida=salida,
                            al_progreso=progreso)
        except Exception as e:
            print(f"Error al procesar {ruta}: {e}")
            continue
        print(f"Guardado en: {r['ruta']}")
        print(f"Tiempo: {_formato_duracion(r['tiempo'])}"
              f" ({r['duracion'] / r['tiempo']:.1f}x tiempo real)")
        exitosos += 1

    print(f"\nResumen: {exitosos}/{len(args.audios)} transcripciones completadas.")
    return 0 if exitosos == len(args.audios) else 1


if __name__ == "__main__":
    sys.exit(main())
