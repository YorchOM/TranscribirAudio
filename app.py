"""
Transcriptor de Audio: la aplicación de ventana.

Una sola ventana con dos pestañas:
  Transcribir  cola de audios, progreso y un log con el texto según sale.
  Modelos      descargar, borrar y elegir el modelo de Whisper por defecto.

Al abrirla, si el modelo por defecto ya está descargado, pide directamente
los audios y se pone a transcribir. Cada .txt se va escribiendo junto a su
audio (o en la carpeta elegida) mientras avanza.
"""

import os
import queue
import sys
import threading
import time
import tkinter as tk
from tkinter import filedialog, messagebox, ttk
from tkinter.scrolledtext import ScrolledText

# El .exe va sin consola, y entonces sys.stdout/stderr son None: cualquier
# librería que escriba en ellos fallaría. Se mandan a la nada.
for _flujo in ("stdout", "stderr"):
    if getattr(sys, _flujo) is None:
        setattr(sys, _flujo, open(os.devnull, "w", encoding="utf-8"))

import gestor_modelos as gm  # noqa: E402
import transcribe_audio as ta  # noqa: E402

TIPOS_AUDIO = [
    ("Archivos de audio", "*.mp3 *.wav *.ogg *.oga *.flac *.opus *.m4a *.m4b *.wma *.aac "
                          "*.amr *.mp4 *.webm"),
    ("Todos los archivos", "*.*"),
]

IDIOMAS = [
    ("", "Detectar automáticamente"),
    ("es", "Español"), ("en", "Inglés"), ("ca", "Catalán"), ("gl", "Gallego"),
    ("eu", "Euskera"), ("fr", "Francés"), ("pt", "Portugués"), ("it", "Italiano"),
    ("de", "Alemán"),
]

MAX_LINEAS_LOG = 5000


def _ruta_recurso(nombre):
    """Fichero empaquetado: en el .exe, dentro de _internal; si no, en assets/."""
    base = getattr(sys, "_MEIPASS", None)
    if base:
        return os.path.join(base, nombre)
    return os.path.join(os.path.dirname(os.path.abspath(__file__)), "assets", nombre)


def _duracion_de(ruta):
    """Duración leyendo sólo la cabecera (rápido), o None si no se sabe."""
    try:
        import av
        with av.open(ruta) as contenedor:
            return contenedor.duration / 1e6 if contenedor.duration else None
    except Exception:
        return None


def _abrir(ruta):
    try:
        os.startfile(ruta)  # noqa: S606 — abrir con el programa asociado (Windows)
    except (AttributeError, OSError) as e:
        messagebox.showerror("No se pudo abrir", str(e))


# --------------------------------------------------------------------------
# Pestaña Transcribir
# --------------------------------------------------------------------------

class App:
    def __init__(self, raiz, iniciales):
        self.raiz = raiz
        self.eventos = queue.Queue()        # del hilo de trabajo a la ventana
        self.cerrojo = threading.Lock()     # protege la cola de audios
        self.items = {}                     # id -> {ruta, estado, salida}
        self.orden = []                     # ids en orden de llegada
        self.contador = 0
        self.trabajando = False
        self.cancelar = threading.Event()
        self.cerrar_al_terminar = False
        self.modelo_en_memoria = (None, None)   # (repo, WhisperModel)
        self.trabajadores = ta.trabajadores_por_defecto()
        self.inicio_archivo = None

        config = gm.leer_configuracion()
        # Lo que lee el hilo de trabajo: variables normales, no de tkinter
        self.ajustes = {
            "modelo": gm.modelo_configurado(),
            "idioma": config.get("idioma", ""),
            "carpeta_salida": config.get("carpeta_salida", ""),
        }

        raiz.title("Transcriptor de Audio")
        try:
            raiz.iconbitmap(default=_ruta_recurso("icono.ico"))  # también en los diálogos
        except tk.TclError:
            pass  # sin icono no pasa nada
        # En píxeles reales: con la escala de Windows al 150 % hay que pedir más
        escala = max(1.0, raiz.winfo_fpixels("1i") / 96)
        raiz.geometry(f"{int(1040 * escala)}x{int(760 * escala)}")
        raiz.minsize(int(820 * escala), int(600 * escala))
        raiz.protocol("WM_DELETE_WINDOW", self.cerrar)

        self.pestanas = ttk.Notebook(raiz)
        self.pestanas.pack(fill="both", expand=True, padx=8, pady=8)
        self.tab_transcribir = ttk.Frame(self.pestanas, padding=10)
        self.tab_modelos = ttk.Frame(self.pestanas, padding=10)
        self.pestanas.add(self.tab_transcribir, text="  Transcribir  ")
        self.pestanas.add(self.tab_modelos, text="  Modelos  ")

        self._construir_transcribir(self.tab_transcribir)
        self.modelos = PestanaModelos(self.tab_modelos, self)

        self.refrescar_modelos()
        self.log("Modelo de Whisper local; el audio no sale de este equipo.", "info")
        self.raiz.after(100, self.atender_eventos)
        self.raiz.after(300, lambda: self.arranque(iniciales))

    # --- Construcción ------------------------------------------------------

    def _construir_transcribir(self, marco):
        # Barra de acciones
        barra = ttk.Frame(marco)
        barra.pack(fill="x")
        ttk.Button(barra, text="＋ Añadir audios…", command=self.pedir_archivos).pack(side="left")
        self.b_quitar = ttk.Button(barra, text="Quitar", command=self.quitar)
        self.b_quitar.pack(side="left", padx=(6, 0))
        ttk.Button(barra, text="Vaciar terminadas", command=self.vaciar_terminadas).pack(
            side="left", padx=(6, 0))
        self.b_detener = ttk.Button(barra, text="■ Detener", command=self.detener)
        self.b_detener.pack(side="right")
        self.b_empezar = ttk.Button(barra, text="▶ Transcribir", command=self.empezar)
        self.b_empezar.pack(side="right", padx=(0, 6))

        # Opciones
        opciones = ttk.LabelFrame(marco, text="Opciones", padding=8)
        opciones.pack(fill="x", pady=(10, 0))

        fila = ttk.Frame(opciones)
        fila.pack(fill="x")
        ttk.Label(fila, text="Modelo:").pack(side="left")
        self.combo_modelo = ttk.Combobox(fila, state="readonly", width=24)
        self.combo_modelo.bind("<<ComboboxSelected>>", self._cambiar_modelo)
        self.combo_modelo.pack(side="left", padx=(4, 4))
        self.aviso_modelo = ttk.Label(fila, foreground="#b35c00")
        self.aviso_modelo.pack(side="left", padx=(0, 16))
        ttk.Label(fila, text="Idioma:").pack(side="left")
        self.combo_idioma = ttk.Combobox(fila, state="readonly", width=24,
                                         values=[n for _, n in IDIOMAS])
        codigos = [c for c, _ in IDIOMAS]
        idioma = self.ajustes["idioma"]
        self.combo_idioma.current(codigos.index(idioma) if idioma in codigos else 0)
        self.combo_idioma.bind("<<ComboboxSelected>>", self._cambiar_idioma)
        self.combo_idioma.pack(side="left", padx=4)

        fila = ttk.Frame(opciones)
        fila.pack(fill="x", pady=(8, 0))
        ttk.Label(fila, text="Guardar las transcripciones:").pack(side="left")
        self.destino = tk.StringVar(value="carpeta" if self.ajustes["carpeta_salida"] else "junto")
        ttk.Radiobutton(fila, text="junto a cada audio", value="junto", variable=self.destino,
                        command=self._cambiar_destino).pack(side="left", padx=(6, 0))
        ttk.Radiobutton(fila, text="en la carpeta:", value="carpeta", variable=self.destino,
                        command=self._cambiar_destino).pack(side="left", padx=(10, 0))
        self.carpeta = tk.StringVar(value=self.ajustes["carpeta_salida"])
        ttk.Entry(fila, textvariable=self.carpeta, state="readonly").pack(
            side="left", fill="x", expand=True, padx=4)
        ttk.Button(fila, text="Elegir…", command=self._elegir_carpeta).pack(side="left")

        # Cola
        tabla = ttk.Frame(marco)
        tabla.pack(fill="both", expand=True, pady=(10, 0))
        self.lista = ttk.Treeview(tabla, columns=("duracion", "estado", "salida"),
                                  selectmode="extended", height=7)
        for col, texto, ancho, anclaje in (("#0", "Audio", 300, "w"),
                                           ("duracion", "Duración", 80, "center"),
                                           ("estado", "Estado", 170, "w"),
                                           ("salida", "Transcripción", 380, "w")):
            self.lista.heading(col, text=texto)
            self.lista.column(col, width=ancho, anchor=anclaje, stretch=col in ("#0", "salida"))
        barra_v = ttk.Scrollbar(tabla, orient="vertical", command=self.lista.yview)
        self.lista.configure(yscrollcommand=barra_v.set)
        self.lista.pack(side="left", fill="both", expand=True)
        barra_v.pack(side="right", fill="y")
        self.lista.bind("<Double-1>", lambda e: self._abrir_transcripcion())
        self.lista.bind("<Delete>", lambda e: self.quitar())
        self.lista.bind("<<TreeviewSelect>>", lambda e: self._actualizar_botones())
        self.lista.tag_configure("hecha", foreground="#1a7f37")
        self.lista.tag_configure("error", foreground="#c62828")
        self.lista.tag_configure("trabajando", font=("Segoe UI", 9, "bold"))

        acciones = ttk.Frame(marco)
        acciones.pack(fill="x", pady=(4, 0))
        ttk.Label(acciones, text="Doble clic en un audio terminado para abrir su transcripción.",
                  foreground="gray").pack(side="left")
        self.b_carpeta = ttk.Button(acciones, text="Abrir carpeta", command=self._abrir_carpeta)
        self.b_carpeta.pack(side="right")
        self.b_abrir = ttk.Button(acciones, text="Abrir transcripción",
                                  command=self._abrir_transcripcion)
        self.b_abrir.pack(side="right", padx=(0, 6))

        # Progreso
        self.texto_progreso = ttk.Label(marco, text="")
        self.texto_progreso.pack(anchor="w", pady=(8, 2))
        self.barra = ttk.Progressbar(marco, mode="determinate", maximum=1000)
        self.barra.pack(fill="x")

        # Log
        ttk.Label(marco, text="Registro (el texto aparece según se transcribe; los trozos van en "
                              "paralelo, el .txt sale ordenado):", foreground="gray").pack(
            anchor="w", pady=(8, 2))
        self.registro = ScrolledText(marco, height=12, wrap="word", font=("Consolas", 9),
                                     state="disabled", relief="solid", borderwidth=1)
        self.registro.pack(fill="both", expand=True)
        self.registro.tag_configure("info", foreground="#666666")
        self.registro.tag_configure("ok", foreground="#1a7f37")
        self.registro.tag_configure("error", foreground="#c62828")
        self.registro.tag_configure("titulo", font=("Consolas", 9, "bold"))
        self.registro.tag_configure("marca", foreground="#8a8a8a")

    # --- Arranque ----------------------------------------------------------

    def modelo_listo(self):
        try:
            return gm.esta_descargado(gm.resolver_repo(self.ajustes["modelo"]))
        except ValueError:
            return False

    def arranque(self, iniciales):
        if self.modelo_listo():
            if iniciales:
                self.anadir(iniciales)
            else:
                self.pedir_archivos()
        else:
            if iniciales:
                self.anadir(iniciales, empezar=False)
            self.pestanas.select(self.tab_modelos)
            self.modelos.bienvenida()

    def listo_para_empezar(self):
        """Lo llama la pestaña Modelos cuando el modelo por defecto queda descargado."""
        self.refrescar_modelos()
        self.pestanas.select(self.tab_transcribir)
        if self._pendientes():
            self.empezar()
        else:
            self.pedir_archivos()

    # --- Cola --------------------------------------------------------------

    def pedir_archivos(self):
        rutas = filedialog.askopenfilenames(parent=self.raiz, filetypes=TIPOS_AUDIO,
                                            title="Elegir audios (Ctrl o Mayús para varios)")
        if rutas:
            self.anadir(list(rutas))

    def anadir(self, rutas, empezar=True):
        nuevas = 0
        with self.cerrojo:
            ya = {os.path.normcase(os.path.abspath(i["ruta"])) for i in self.items.values()
                  if i["estado"] in ("cola", "trabajando")}
        for ruta in rutas:
            if not os.path.isfile(ruta):
                self.log(f"No existe: {ruta}", "error")
                continue
            clave = os.path.normcase(os.path.abspath(ruta))
            if clave in ya:
                continue
            ya.add(clave)
            duracion = _duracion_de(ruta)
            self.contador += 1
            iid = str(self.contador)
            with self.cerrojo:
                self.items[iid] = {"ruta": ruta, "estado": "cola", "salida": ""}
                self.orden.append(iid)
            self.lista.insert("", "end", iid=iid, text=os.path.basename(ruta),
                              values=(ta._formato_tiempo(duracion) if duracion else "—",
                                      "En cola", ""))
            nuevas += 1
        if nuevas:
            self.log(f"{nuevas} audio(s) añadidos a la cola.", "info")
        self._actualizar_botones()
        if nuevas and empezar and not self.trabajando:
            self.empezar()

    def _pendientes(self):
        with self.cerrojo:
            return [i for i in self.orden if self.items[i]["estado"] == "cola"]

    def quitar(self):
        quitar = []
        with self.cerrojo:
            for iid in self.lista.selection():
                if self.items[iid]["estado"] != "trabajando":
                    quitar.append(iid)
                    del self.items[iid]
                    self.orden.remove(iid)
        for iid in quitar:
            self.lista.delete(iid)
        self._actualizar_botones()

    def vaciar_terminadas(self):
        with self.cerrojo:
            fuera = [i for i in self.orden if self.items[i]["estado"] in ("hecha", "error")]
            for iid in fuera:
                del self.items[iid]
                self.orden.remove(iid)
        for iid in fuera:
            self.lista.delete(iid)
        self._actualizar_botones()

    def _siguiente(self):
        """Para el hilo de trabajo: el próximo audio en cola, ya marcado como en marcha."""
        with self.cerrojo:
            for iid in self.orden:
                if self.items[iid]["estado"] == "cola":
                    self.items[iid]["estado"] = "trabajando"
                    return iid, self.items[iid]["ruta"]
        return None, None

    def _seleccion_con_salida(self):
        for iid in self.lista.selection():
            salida = self.items.get(iid, {}).get("salida")
            if salida and os.path.isfile(salida):
                return salida
        return None

    def _abrir_transcripcion(self):
        salida = self._seleccion_con_salida()
        if salida:
            _abrir(salida)

    def _abrir_carpeta(self):
        salida = self._seleccion_con_salida()
        if salida:
            _abrir(os.path.dirname(salida))
        elif self.ajustes["carpeta_salida"]:
            _abrir(self.ajustes["carpeta_salida"])
        else:
            sel = self.lista.selection()
            if sel:
                _abrir(os.path.dirname(os.path.abspath(self.items[sel[0]]["ruta"])))

    # --- Opciones ----------------------------------------------------------

    def refrescar_modelos(self):
        """Rellena el desplegable con los modelos descargados."""
        descargados = [m["nombre"] for m in gm.catalogo() if m["descargado"]]
        self.combo_modelo["values"] = descargados
        actual = next((n for n in descargados if gm.mismo_modelo(n, self.ajustes["modelo"])), None)
        if actual:
            self.combo_modelo.set(actual)
            self.aviso_modelo.config(text="")
        else:
            self.combo_modelo.set("")
            self.aviso_modelo.config(
                text=f"'{self.ajustes['modelo']}' no está descargado: ve a la pestaña Modelos")
        self._actualizar_botones()

    def fijar_modelo(self, nombre):
        self.ajustes["modelo"] = nombre
        self._guardar(modelo=nombre)
        self.refrescar_modelos()
        self.modelos.rellenar()

    def _cambiar_modelo(self, _evento=None):
        nombre = self.combo_modelo.get()
        if nombre and not gm.mismo_modelo(nombre, self.ajustes["modelo"]):
            self.fijar_modelo(nombre)
            aviso = " (se aplica a partir del siguiente audio)" if self.trabajando else ""
            self.log(f"Modelo por defecto: {nombre}{aviso}", "info")

    def _cambiar_idioma(self, _evento=None):
        self.ajustes["idioma"] = IDIOMAS[self.combo_idioma.current()][0]
        self._guardar(idioma=self.ajustes["idioma"])

    def _elegir_carpeta(self):
        carpeta = filedialog.askdirectory(parent=self.raiz, title="Carpeta para las transcripciones",
                                          initialdir=self.carpeta.get() or None)
        if carpeta:
            self.carpeta.set(os.path.normpath(carpeta))
            self.destino.set("carpeta")
        elif not self.carpeta.get():
            self.destino.set("junto")  # canceló sin haber ninguna carpeta elegida
        self._aplicar_destino()

    def _cambiar_destino(self):
        """Al pulsar los botones de opción: 'en la carpeta' sin carpeta pide una."""
        if self.destino.get() == "carpeta" and not self.carpeta.get():
            self._elegir_carpeta()
        else:
            self._aplicar_destino()

    def _aplicar_destino(self):
        carpeta = self.carpeta.get() if self.destino.get() == "carpeta" else ""
        self.ajustes["carpeta_salida"] = carpeta
        self._guardar(carpeta_salida=carpeta)

    def _guardar(self, **cambios):
        try:
            gm.guardar_configuracion(**cambios)
        except OSError as e:
            self.log(f"No se pudo guardar la configuración: {e}", "error")

    def _actualizar_botones(self):
        pendientes = bool(self._pendientes())
        listo = self.modelo_listo()
        self.b_empezar.state(["!disabled"] if pendientes and listo and not self.trabajando
                             else ["disabled"])
        self.b_detener.state(["!disabled"] if self.trabajando and not self.cancelar.is_set()
                             else ["disabled"])
        hay_salida = self._seleccion_con_salida() is not None
        self.b_abrir.state(["!disabled"] if hay_salida else ["disabled"])

    # --- Trabajo -----------------------------------------------------------

    def empezar(self):
        if self.trabajando or not self._pendientes():
            return
        if not self.modelo_listo():
            self.pestanas.select(self.tab_modelos)
            self.modelos.bienvenida()
            return
        self.trabajando = True
        self.cancelar.clear()
        self._actualizar_botones()
        threading.Thread(target=self._trabajar, daemon=True).start()

    def detener(self):
        if self.trabajando:
            self.cancelar.set()
            self.log("Deteniendo en la frase en curso...", "info")
            self._actualizar_botones()

    def _obtener_modelo(self, nombre):
        repo = gm.resolver_repo(nombre)
        if self.modelo_en_memoria[0] != repo:
            self.modelo_en_memoria = (None, None)  # soltar el anterior antes de cargar otro
            self.eventos.put(("log", f"Cargando el modelo {nombre}...", "info"))
            modelo = ta.cargar_modelo(nombre, self.trabajadores, ta.HILOS_POR_TRABAJADOR)
            self.modelo_en_memoria = (repo, modelo)
        return self.modelo_en_memoria[1]

    def _trabajar(self):
        """Hilo de trabajo: vacía la cola de uno en uno."""
        poner = self.eventos.put
        while not self.cancelar.is_set():
            iid, ruta = self._siguiente()
            if iid is None:
                break
            ajustes = dict(self.ajustes)  # los cambios valen para el siguiente audio
            poner(("empieza", iid, ruta))
            try:
                modelo = self._obtener_modelo(ajustes["modelo"])
                resumen = ta.transcribir(
                    ruta, modelo, self.trabajadores,
                    idioma=ajustes["idioma"] or None,
                    carpeta_salida=ajustes["carpeta_salida"] or None,
                    al_segmento=lambda linea: poner(("texto", linea)),
                    al_progreso=lambda f, iid=iid: poner(("progreso", iid, f)),
                    avisar=lambda texto: poner(("log", texto, "info")),
                    cancelar=self.cancelar,
                )
                poner(("hecha", iid, resumen))
            except ta.Cancelado:
                poner(("detenida", iid, ta.ruta_de_salida(ruta, ajustes["carpeta_salida"] or None)))
                break
            except Exception as e:
                poner(("error", iid, str(e) or type(e).__name__))
        poner(("fin",))

    # --- Eventos del hilo de trabajo ---------------------------------------

    def atender_eventos(self):
        try:
            for _ in range(500):  # a tandas, para no congelar la ventana
                self._atender(self.eventos.get_nowait())
        except queue.Empty:
            pass
        self.raiz.after(100, self.atender_eventos)

    def _estado(self, iid, texto, etiqueta=None, salida=None):
        if not self.lista.exists(iid):
            return
        valores = list(self.lista.item(iid, "values"))
        valores[1] = texto
        if salida is not None:
            valores[2] = salida
        self.lista.item(iid, values=valores, tags=(etiqueta,) if etiqueta else ())

    def _atender(self, evento):
        tipo = evento[0]
        if tipo == "log":
            self.log(evento[1], evento[2])
        elif tipo == "texto":
            ini, fin, texto = evento[1]
            self.log(f"[{ta._formato_tiempo(ini)}] ", "marca", texto)
        elif tipo == "empieza":
            _, iid, ruta = evento
            self.inicio_archivo = time.monotonic()
            self._estado(iid, "Transcribiendo…", "trabajando")
            self.lista.see(iid)
            self.barra["value"] = 0
            self.log("")
            self.log(f"▶ {os.path.basename(ruta)}", "titulo")
            self._texto_progreso(iid, 0.0)
        elif tipo == "progreso":
            _, iid, fraccion = evento
            self.barra["value"] = 1000 * fraccion
            self._estado(iid, f"Transcribiendo {100 * fraccion:.0f}%", "trabajando")
            self._texto_progreso(iid, fraccion)
        elif tipo == "hecha":
            _, iid, r = evento
            with self.cerrojo:
                if iid in self.items:
                    self.items[iid].update(estado="hecha", salida=r["ruta"])
            velocidad = r["duracion"] / r["tiempo"] if r["tiempo"] else 0
            self._estado(iid, f"Hecha en {ta._formato_duracion(r['tiempo'])}", "hecha", r["ruta"])
            self.barra["value"] = 1000
            self.log(f"✔ Guardada en {r['ruta']} ({ta._formato_duracion(r['tiempo'])},"
                     f" {velocidad:.1f}x tiempo real)", "ok")
        elif tipo == "detenida":
            _, iid, salida = evento
            with self.cerrojo:
                if iid in self.items:
                    self.items[iid].update(estado="cola", salida=salida)
            self._estado(iid, "En cola (detenida)", None, salida)
            self.log("■ Detenida. El .txt tiene lo hecho hasta ahora; al volver a empezar se "
                     "rehace entero.", "info")
        elif tipo == "error":
            _, iid, mensaje = evento
            with self.cerrojo:
                if iid in self.items:
                    self.items[iid]["estado"] = "error"
            self._estado(iid, "Error", "error")
            self.log(f"✖ Error: {mensaje}", "error")
        elif tipo == "fin":
            self.trabajando = False
            self.inicio_archivo = None
            if self.cerrar_al_terminar:
                self.raiz.destroy()
                return
            if not self.cancelar.is_set():
                hechas = sum(1 for i in self.items.values() if i["estado"] == "hecha")
                self.texto_progreso.config(text=f"Cola terminada: {hechas} transcripción(es) hechas.")
            else:
                self.texto_progreso.config(text="Detenido.")
            self._actualizar_botones()

    def _texto_progreso(self, iid, fraccion):
        with self.cerrojo:
            total = len(self.orden)
            posicion = self.orden.index(iid) + 1 if iid in self.orden else 0
            pendientes = sum(1 for i in self.orden if self.items[i]["estado"] == "cola")
        texto = f"Audio {posicion} de {total}: {100 * fraccion:.0f}%"
        if pendientes:
            texto = f"{texto} ({pendientes} más en cola)"
        if self.inicio_archivo and fraccion > 0.03:
            transcurrido = time.monotonic() - self.inicio_archivo
            texto += f", quedan ~{ta._formato_duracion(transcurrido * (1 - fraccion) / fraccion)}"
        self.texto_progreso.config(text=texto)

    # --- Log ---------------------------------------------------------------

    def log(self, texto, etiqueta=None, resto=None):
        r = self.registro
        abajo = r.yview()[1] > 0.999  # sólo se autodesplaza si ya estaba abajo
        r.configure(state="normal")
        r.insert("end", texto, etiqueta or ())
        if resto is not None:
            r.insert("end", resto)
        r.insert("end", "\n")
        lineas = int(r.index("end-1c").split(".")[0])
        if lineas > MAX_LINEAS_LOG:
            r.delete("1.0", f"{lineas - MAX_LINEAS_LOG}.0")
        r.configure(state="disabled")
        if abajo:
            r.see("end")

    # --- Cierre ------------------------------------------------------------

    def cerrar(self):
        if self.modelos.descargando and not messagebox.askyesno(
                "Descarga en curso", "Hay una descarga de modelo a medias. Si cierras se "
                                     "cancela (se podrá repetir). ¿Cerrar?"):
            return
        if self.trabajando:
            if not messagebox.askyesno(
                    "Transcripción en curso", "Hay una transcripción en marcha. Si cierras, "
                                              "se detiene y su .txt queda con lo hecho hasta "
                                              "ahora. ¿Cerrar?"):
                return
            # Se espera a que el hilo pare y deje escrito el .txt parcial
            self.cerrar_al_terminar = True
            self.detener()
            return
        self.raiz.destroy()


# --------------------------------------------------------------------------
# Pestaña Modelos
# --------------------------------------------------------------------------

class PestanaModelos:
    def __init__(self, marco, app):
        self.app = app
        self.eventos = queue.Queue()
        self.descargando = False

        ttk.Label(marco, text="Modelos de Whisper", font=("Segoe UI", 12, "bold")).pack(anchor="w")
        self.intro = ttk.Label(marco, wraplength=900, text=(
            "El marcado con ★ es el que se usa para transcribir. Cada modelo se descarga una "
            "sola vez (sin cuenta ni registro) y a partir de ahí funciona sin internet."))
        self.intro.pack(anchor="w", pady=(2, 8))

        tabla = ttk.Frame(marco)
        tabla.pack(fill="both", expand=True)
        self.lista = ttk.Treeview(tabla, columns=("nota", "estado"), selectmode="browse")
        self.lista.heading("#0", text="Modelo")
        self.lista.heading("nota", text="Descripción")
        self.lista.heading("estado", text="Estado")
        self.lista.column("#0", width=240)
        self.lista.column("nota", width=440)
        self.lista.column("estado", width=120, anchor="center")
        barra = ttk.Scrollbar(tabla, orient="vertical", command=self.lista.yview)
        self.lista.configure(yscrollcommand=barra.set)
        self.lista.pack(side="left", fill="both", expand=True)
        barra.pack(side="right", fill="y")
        self.lista.bind("<<TreeviewSelect>>", lambda e: self.actualizar_botones())
        self.lista.bind("<Double-1>", lambda e: self.usar_por_defecto())

        self.ver_ingles = tk.BooleanVar(value=False)
        ttk.Checkbutton(marco, text="Mostrar también los modelos que solo sirven para inglés",
                        variable=self.ver_ingles, command=self.rellenar).pack(anchor="w", pady=4)

        botones = ttk.Frame(marco)
        botones.pack(fill="x", pady=(4, 8))
        self.b_defecto = ttk.Button(botones, text="★ Usar este", command=self.usar_por_defecto)
        self.b_bajar = ttk.Button(botones, text="Descargar", command=self.descargar_seleccionado)
        self.b_borrar = ttk.Button(botones, text="Borrar", command=self.borrar)
        for b in (self.b_defecto, self.b_bajar, self.b_borrar):
            b.pack(side="left", padx=(0, 6))

        otro = ttk.Frame(marco)
        otro.pack(fill="x", pady=(0, 8))
        ttk.Label(otro, text="Otro modelo compatible (usuario/modelo):").pack(side="left")
        self.externo = tk.StringVar()
        entrada = ttk.Entry(otro, textvariable=self.externo, width=44)
        entrada.pack(side="left", padx=6)
        entrada.bind("<Return>", lambda e: self.anadir_externo())
        self.b_externo = ttk.Button(otro, text="Descargar", command=self.anadir_externo)
        self.b_externo.pack(side="left")

        self.progreso = ttk.Progressbar(marco, mode="determinate", maximum=100)
        self.progreso.pack(fill="x")
        self.estado = ttk.Label(marco, text=f"Carpeta de modelos: {gm.carpeta_modelos()}",
                                foreground="gray")
        self.estado.pack(anchor="w", pady=(4, 0))

        self.rellenar()
        marco.after(100, self.atender_eventos)

    def bienvenida(self):
        """Primera vez: aún no hay modelo. Se señala el de por defecto."""
        self.intro.config(foreground="#b35c00", text=(
            "Para empezar hace falta descargar un modelo (una sola vez, sin cuenta ni registro). "
            f"Está seleccionado '{self.app.ajustes['modelo']}', el recomendado: pulsa "
            "«Descargar». Cuando termine se abrirá la selección de audios."))
        nombre = next((m["nombre"] for m in gm.catalogo()
                       if gm.mismo_modelo(m["nombre"], self.app.ajustes["modelo"])), None)
        if nombre and self.lista.exists(nombre):
            self.lista.selection_set(nombre)
            self.lista.see(nombre)
            self.lista.focus(nombre)

    # --- Tabla -------------------------------------------------------------

    def rellenar(self):
        seleccion = self.seleccionado()
        defecto = self.app.ajustes["modelo"]
        self.lista.delete(*self.lista.get_children())
        for m in gm.catalogo():
            es_defecto = gm.mismo_modelo(m["nombre"], defecto)
            if m["solo_ingles"] and not self.ver_ingles.get() and not es_defecto:
                continue
            nota = m["nota"] + (" (solo inglés)" if m["solo_ingles"] else "")
            self.lista.insert("", "end", iid=m["nombre"],
                              text=("★ " if es_defecto else "    ") + m["nombre"],
                              values=(nota, "descargado" if m["descargado"] else "—"))
        if "/" in defecto and not self.lista.exists(defecto):
            self.lista.insert("", "end", iid=defecto, text="★ " + defecto,
                              values=("modelo externo", "—"))
        if seleccion and self.lista.exists(seleccion):
            self.lista.selection_set(seleccion)
        self.actualizar_botones()

    def seleccionado(self):
        sel = self.lista.selection()
        return sel[0] if sel else None

    def _en_uso(self, nombre):
        """El que tiene cargado la transcripción en marcha: no se puede borrar."""
        return self.app.trabajando and self.app.modelo_en_memoria[0] == gm.resolver_repo(nombre)

    def actualizar_botones(self):
        nombre = self.seleccionado()
        bajado = bool(nombre) and gm.esta_descargado(gm.resolver_repo(nombre))
        libre = not self.descargando
        self.b_defecto.state(["!disabled"] if nombre else ["disabled"])
        self.b_bajar.state(["!disabled"] if nombre and not bajado and libre else ["disabled"])
        self.b_borrar.state(["!disabled"] if nombre and bajado and libre and not self._en_uso(nombre)
                            else ["disabled"])
        self.b_externo.state(["!disabled"] if libre else ["disabled"])

    # --- Acciones ----------------------------------------------------------

    def usar_por_defecto(self):
        nombre = self.seleccionado()
        if not nombre:
            return
        self.app.fijar_modelo(nombre)
        self.app.log(f"Modelo por defecto: {nombre}", "info")
        if not gm.esta_descargado(gm.resolver_repo(nombre)) and not self.descargando:
            if messagebox.askyesno("Descargar ahora",
                                   f"'{nombre}' todavía no está descargado. ¿Lo descargo ahora?"):
                self.descargar(nombre)

    def descargar_seleccionado(self):
        if self.seleccionado():
            self.descargar(self.seleccionado())

    def anadir_externo(self):
        repo = self.externo.get().strip().strip("/")
        if repo.startswith("https://huggingface.co/"):
            repo = repo[len("https://huggingface.co/"):]
        if repo.count("/") != 1:
            messagebox.showwarning("Modelo externo", "Escríbelo como usuario/modelo, por ejemplo:\n"
                                                     "Systran/faster-whisper-medium")
            return
        self.descargar(repo)

    def borrar(self):
        nombre = self.seleccionado()
        if not nombre or self._en_uso(nombre):
            return
        aviso = ("\n\nEs el que está marcado para transcribir: habrá que volver a descargarlo "
                 "o elegir otro." if gm.mismo_modelo(nombre, self.app.ajustes["modelo"]) else "")
        if not messagebox.askyesno("Borrar modelo", f"¿Borro '{nombre}' del disco?{aviso}"):
            return
        if self.app.modelo_en_memoria[0] == gm.resolver_repo(nombre):
            self.app.modelo_en_memoria = (None, None)  # soltarlo para poder borrar sus ficheros
        try:
            gm.borrar_modelo(nombre)
        except OSError as e:
            messagebox.showerror("No se pudo borrar", str(e))
        self.rellenar()
        self.app.refrescar_modelos()

    # --- Descarga en segundo plano -----------------------------------------

    def descargar(self, nombre):
        self.descargando = True
        self.actualizar_botones()
        self.progreso["value"] = 0
        self.estado.config(text=f"Preparando la descarga de {nombre}...")

        def trabajo():
            try:
                gm.descargar_modelo(
                    nombre,
                    progreso=lambda f, hecho, total: self.eventos.put(("avance", f, hecho, total)),
                    avisar=lambda texto: self.eventos.put(("texto", texto)),
                )
                self.eventos.put(("fin", nombre, None))
            except Exception as e:
                self.eventos.put(("fin", nombre, str(e)))

        threading.Thread(target=trabajo, daemon=True).start()

    def atender_eventos(self):
        try:
            while True:
                evento = self.eventos.get_nowait()
                if evento[0] == "texto":
                    self.estado.config(text=evento[1])
                elif evento[0] == "avance":
                    _, fichero, hecho, total = evento
                    self.progreso["value"] = 100 * hecho / total
                    self.estado.config(text=f"Descargando {fichero}: {hecho >> 20} de "
                                            f"{total >> 20} MB")
                elif evento[0] == "fin":
                    self._fin_descarga(*evento[1:])
        except queue.Empty:
            pass
        self.lista.after(100, self.atender_eventos)

    def _fin_descarga(self, nombre, error):
        self.descargando = False
        if error:
            self.progreso["value"] = 0
            self.estado.config(text="La descarga falló.")
            messagebox.showerror("Descarga", error)
            self.rellenar()
            return
        self.progreso["value"] = 100
        self.estado.config(text=f"'{nombre}' descargado.")
        self.app.log(f"Modelo '{nombre}' descargado.", "ok")
        if "/" in nombre:
            self.externo.set("")
        self.rellenar()
        if self.lista.exists(nombre):
            self.lista.selection_set(nombre)
            self.lista.see(nombre)
        self.app.refrescar_modelos()
        if gm.mismo_modelo(nombre, self.app.ajustes["modelo"]):
            # Era el que faltaba para empezar
            self.intro.config(foreground="", text=(
                "El marcado con ★ es el que se usa para transcribir. Cada modelo se descarga una "
                "sola vez (sin cuenta ni registro) y a partir de ahí funciona sin internet."))
            if not self.app.trabajando:
                self.app.listo_para_empezar()


def main():
    try:
        from ctypes import windll
        # Antes de crear la ventana: texto nítido en pantallas con escala
        windll.shcore.SetProcessDpiAwareness(1)
        # Identidad propia en la barra de tareas: si no, al lanzarla con
        # pythonw se agrupa con Python y muestra su icono en vez del nuestro
        windll.shell32.SetCurrentProcessExplicitAppUserModelID("YorchOM.TranscribirAudio")
    except Exception:
        pass
    raiz = tk.Tk()
    iniciales = [a for a in sys.argv[1:] if not a.startswith("-")]  # arrastrados sobre el .exe
    App(raiz, iniciales)
    raiz.mainloop()


if __name__ == "__main__":
    main()
