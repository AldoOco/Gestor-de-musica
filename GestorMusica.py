import json
import os
import re
import shutil
import sqlite3
import threading
import queue
import time
import tkinter as tk
from datetime import datetime
from tkinter import ttk, filedialog, messagebox

import yt_dlp

# ----------------------------------------------------------------------------
# UBICACIÓN DE LA BASE DE DATOS
#   - Carpeta de la app:      ~/.yt_playlist_manager/
#   - Archivo de ajustes:     ~/.yt_playlist_manager/config.json  (guarda db_path)
#   - Base de datos (defecto): ~/.yt_playlist_manager/registro.db
# Puedes moverla desde la ventana "Ver registro" > "Cambiar ubicación de la BD".
# ----------------------------------------------------------------------------
APP_DIR = os.path.join(os.path.expanduser("~"), ".yt_playlist_manager")
os.makedirs(APP_DIR, exist_ok=True)
CONFIG_PATH = os.path.join(APP_DIR, "config.json")
DB_DEFECTO = os.path.join(APP_DIR, "registro.db")


def leer_db_path():
    try:
        with open(CONFIG_PATH, encoding="utf-8") as f:
            ruta = json.load(f).get("db_path")
            if ruta and os.path.isdir(os.path.dirname(ruta)):
                return ruta
    except (OSError, ValueError):
        pass
    return DB_DEFECTO


def guardar_db_path(ruta):
    with open(CONFIG_PATH, "w", encoding="utf-8") as f:
        json.dump({"db_path": ruta}, f)


def carpeta_musica():
    for n in ("Music", "Música"):
        p = os.path.join(os.path.expanduser("~"), n)
        if os.path.isdir(p):
            return p
    return os.path.expanduser("~")


def fmt_tiempo(seg):
    seg = int(max(0, seg))
    h, r = divmod(seg, 3600)
    m, s = divmod(r, 60)
    return f"{h:02d}:{m:02d}:{s:02d}"


# ---------------------------------------------------------------- Base de datos
class Registro:
    def __init__(self):
        self.db_path = leer_db_path()
        self._abrir()

    def _abrir(self):
        self.con = sqlite3.connect(self.db_path, check_same_thread=False)
        self.con.execute("""CREATE TABLE IF NOT EXISTS listas(
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            carpeta TEXT NOT NULL,
            ruta TEXT NOT NULL UNIQUE,
            url TEXT NOT NULL,
            fecha_creacion TEXT,
            fecha_actualizacion TEXT,
            fecha_exportacion TEXT,
            ruta_exportacion TEXT)""")
        self.con.execute("""CREATE TABLE IF NOT EXISTS config(
            clave TEXT PRIMARY KEY, valor TEXT)""")
        self.con.commit()

    def mover(self, carpeta_nueva):
        nuevo = os.path.join(carpeta_nueva, "registro.db")
        if os.path.abspath(nuevo) == os.path.abspath(self.db_path):
            return
        if os.path.exists(nuevo):
            raise RuntimeError(f"Ya existe un archivo registro.db en:\n{carpeta_nueva}")
        self.con.commit()
        self.con.close()
        shutil.move(self.db_path, nuevo)
        self.db_path = nuevo
        guardar_db_path(nuevo)
        self._abrir()

    def get_config(self, clave, defecto=""):
        r = self.con.execute("SELECT valor FROM config WHERE clave=?", (clave,)).fetchone()
        return r[0] if r else defecto

    def set_config(self, clave, valor):
        self.con.execute("INSERT OR REPLACE INTO config VALUES(?,?)", (clave, valor))
        self.con.commit()

    def agregar(self, carpeta, ruta, url):
        ahora = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        self.con.execute(
            "INSERT OR REPLACE INTO listas(carpeta,ruta,url,fecha_creacion,fecha_actualizacion)"
            " VALUES(?,?,?,?,?)", (carpeta, ruta, url, ahora, ahora))
        self.con.commit()

    def todas(self):
        return self.con.execute("SELECT id,carpeta,ruta,url FROM listas ORDER BY carpeta").fetchall()

    def tabla_completa(self):
        return self.con.execute(
            "SELECT id,carpeta,url,fecha_creacion,fecha_actualizacion,"
            "fecha_exportacion,ruta_exportacion,ruta FROM listas ORDER BY carpeta").fetchall()

    def obtener(self, id_):
        return self.con.execute("SELECT id,carpeta,ruta,url,ruta_exportacion FROM listas WHERE id=?",
                                (id_,)).fetchone()

    def marcar_actualizada(self, id_):
        self.con.execute("UPDATE listas SET fecha_actualizacion=? WHERE id=?",
                         (datetime.now().strftime("%Y-%m-%d %H:%M:%S"), id_))
        self.con.commit()

    def marcar_exportada(self, id_, destino):
        self.con.execute("UPDATE listas SET fecha_exportacion=?, ruta_exportacion=? WHERE id=?",
                         (datetime.now().strftime("%Y-%m-%d %H:%M:%S"), destino, id_))
        self.con.commit()

    def borrar(self, id_):
        self.con.execute("DELETE FROM listas WHERE id=?", (id_,))
        self.con.commit()


# ---------------------------------------------------------------- Progreso
class Progreso:
    """Estado compartido entre el hilo de trabajo y la interfaz."""

    def __init__(self):
        self.prefijo = ""
        self.etapa("Iniciando…")

    def etapa(self, fase, total=0):
        self.fase = fase
        self.total = total
        self.hechas = 0
        self.frac = 0.0
        self.detalle = ""
        self.t0 = time.time()

    def avanzar(self, detalle=""):
        self.hechas += 1
        self.frac = 0.0
        if detalle:
            self.detalle = detalle

    def porcentaje(self):
        if self.total <= 0:
            return None
        return min(100.0, (self.hechas + self.frac) / self.total * 100)

    def texto_estado(self, unidad="canciones"):
        t = f"{self.prefijo}{self.fase}"
        if self.total > 0:
            t += f": {self.hechas} de {self.total} {unidad}"
        if self.detalle:
            t += f" — {self.detalle}"
        return t

    def texto_tiempo(self):
        transcurrido = time.time() - self.t0
        t = f"Transcurrido: {fmt_tiempo(transcurrido)}"
        hecho = self.hechas + self.frac
        if self.total > 0 and hecho > 0.05:
            restante = transcurrido / hecho * (self.total - hecho)
            t += f"   |   Tiempo estimado restante: {fmt_tiempo(restante)}"
        elif self.total > 0:
            t += "   |   Tiempo estimado restante: calculando…"
        return t


# ---------------------------------------------------------------- Lógica yt-dlp
def limpiar_nombre(n):
    return re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", n).strip().rstrip(".") or "lista"


def opts_base(carpeta=None, archivo_registro=None, prog=None):
    o = {
        "format": "bestaudio/best",
        "postprocessors": [
            {"key": "FFmpegExtractAudio", "preferredcodec": "mp3", "preferredquality": "0"},
            {"key": "FFmpegMetadata"},
        ],
        "ignoreerrors": True,
        "noplaylist": False,
        "quiet": True,
        "no_warnings": True,
        "windowsfilenames": True,
    }
    if carpeta:
        o["outtmpl"] = os.path.join(carpeta, "%(title)s [%(id)s].%(ext)s")
    if archivo_registro:
        o["download_archive"] = archivo_registro
    if prog:
        def hook_descarga(d):
            titulo = (d.get("info_dict") or {}).get("title") or ""
            if d["status"] == "downloading":
                total = d.get("total_bytes") or d.get("total_bytes_estimate") or 0
                if total:
                    prog.frac = min(0.85, d.get("downloaded_bytes", 0) / total * 0.85)
                prog.detalle = f"{titulo} (descargando)"
            elif d["status"] == "finished":
                prog.frac = 0.9
                prog.detalle = f"{titulo} (convirtiendo a MP3)"

        def hook_pp(d):
            if d.get("postprocessor") == "FFmpegExtractAudio" and d["status"] == "finished":
                titulo = (d.get("info_dict") or {}).get("title") or ""
                prog.avanzar(f"{titulo} (listo)")
        o["progress_hooks"] = [hook_descarga]
        o["postprocessor_hooks"] = [hook_pp]
    return o


def info_lista(url):
    o = {"quiet": True, "extract_flat": True, "skip_download": True, "ignoreerrors": True}
    with yt_dlp.YoutubeDL(o) as ydl:
        info = ydl.extract_info(url, download=False)
    if not info:
        raise RuntimeError("No se pudo obtener información de la URL.")
    return info


def ids_remotos(info):
    return {e["id"]: e for e in (info.get("entries") or []) if e and e.get("id")}


def ids_locales(carpeta):
    res = {}
    if not os.path.isdir(carpeta):
        return res
    for f in os.listdir(carpeta):
        m = re.search(r"\[([^\[\]]+)\]\.mp3$", f)
        if m:
            res[m.group(1)] = os.path.join(carpeta, f)
    return res


def descargar_lista(url, carpeta, prog):
    os.makedirs(carpeta, exist_ok=True)
    arch = os.path.join(carpeta, ".archivo_descargas.txt")
    with yt_dlp.YoutubeDL(opts_base(carpeta, arch, prog)) as ydl:
        ydl.download([url])


def sincronizar(url, carpeta, prog):
    """Devuelve (descargadas, eliminadas, no_disponibles)."""
    prog.etapa("Consultando la lista en YouTube…")
    remotos = ids_remotos(info_lista(url))
    locales = ids_locales(carpeta)

    a_borrar = [v for v in locales if v not in remotos]
    prog.etapa("Eliminando canciones removidas", len(a_borrar))
    for vid in a_borrar:
        prog.detalle = os.path.basename(locales[vid])
        try:
            os.remove(locales[vid])
        except OSError:
            pass
        prog.avanzar()

    arch = os.path.join(carpeta, ".archivo_descargas.txt")
    if os.path.exists(arch):
        with open(arch, encoding="utf-8") as f:
            lineas = [l for l in f if l.split()[-1:] and l.split()[-1] in remotos]
        with open(arch, "w", encoding="utf-8") as f:
            f.writelines(lineas)

    nuevas = [v for v in remotos if v not in locales]
    descargadas = 0
    if nuevas:
        prog.etapa("Descargando canciones nuevas", len(nuevas))
        descargar_lista(url, carpeta, prog)
        descargadas = prog.hechas
    return descargadas, len(a_borrar), len(nuevas) - descargadas


def exportar_lista(origen, destino_base, prog):
    destino = os.path.join(destino_base, os.path.basename(origen))
    os.makedirs(destino, exist_ok=True)
    prog.etapa("Comparando carpetas…")
    src = {f for f in os.listdir(origen) if f.lower().endswith(".mp3")}
    dst = {f for f in os.listdir(destino) if f.lower().endswith(".mp3")}
    sobrantes, nuevas = sorted(dst - src), sorted(src - dst)

    prog.etapa("Eliminando canciones removidas del destino", len(sobrantes))
    for f in sobrantes:
        prog.detalle = f
        os.remove(os.path.join(destino, f))
        prog.avanzar()

    prog.etapa("Copiando canciones", len(nuevas))
    for f in nuevas:
        prog.detalle = f
        shutil.copy(os.path.join(origen, f), os.path.join(destino, f))
        prog.avanzar()
    return destino


# ---------------------------------------------------------------- Interfaz
class App(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title("Gestor de listas de música")
        self.geometry("820x580")
        self.db = Registro()
        self.q = queue.Queue()
        self.seleccion = set()
        self.celdas = []
        self.ocupado = False
        self.prog = None

        top = ttk.Frame(self, padding=8)
        top.pack(fill="x")
        top.columnconfigure(0, weight=1)
        self.var_dir = tk.StringVar(value=self.db.get_config("destino", carpeta_musica()))
        ttk.Entry(top, textvariable=self.var_dir, state="readonly").grid(
            row=0, column=0, sticky="ew")
        ttk.Button(top, text="Seleccionar carpeta", command=self.elegir_carpeta).grid(
            row=0, column=1, padx=(6, 0))
        ttk.Button(top, text="Ver registro", command=self.ver_registro).grid(
            row=1, column=1, padx=(6, 0), pady=(4, 0), sticky="ew")

        bar = ttk.Frame(self, padding=(8, 0))
        bar.pack(fill="x")
        for txt, cmd in [("Nueva lista", self.nueva_lista), ("Actualizar", self.actualizar),
                         ("Exportar", self.exportar), ("Borrar", self.borrar)]:
            ttk.Button(bar, text=txt, command=cmd).pack(side="left", padx=(0, 6), pady=6)

        # Panel inferior: estado, barra de progreso y tiempo estimado
        inferior = ttk.Frame(self)
        inferior.pack(side="bottom", fill="x")
        self.estado = tk.StringVar(value="Listo")
        self.tiempo = tk.StringVar(value="Tiempo estimado: —")
        ttk.Label(inferior, textvariable=self.estado, relief="sunken", anchor="w").pack(fill="x")
        self.barra = ttk.Progressbar(inferior, mode="determinate", maximum=100)
        self.barra.pack(fill="x")
        ttk.Label(inferior, textvariable=self.tiempo, relief="sunken", anchor="w").pack(fill="x")

        cont = ttk.Frame(self)
        cont.pack(fill="both", expand=True, padx=8, pady=4)
        self.canvas = tk.Canvas(cont, bg="white", highlightthickness=1)
        sb = ttk.Scrollbar(cont, orient="vertical", command=self.canvas.yview)
        self.canvas.configure(yscrollcommand=sb.set)
        sb.pack(side="right", fill="y")
        self.canvas.pack(side="left", fill="both", expand=True)
        self.canvas.bind("<Configure>", lambda e: self.refrescar())
        self.canvas.bind("<Button-1>", self.click)
        self.canvas.bind("<MouseWheel>", lambda e: self.canvas.yview_scroll(-1 * (e.delta // 120), "units"))
        self.canvas.bind("<Button-4>", lambda e: self.canvas.yview_scroll(-1, "units"))
        self.canvas.bind("<Button-5>", lambda e: self.canvas.yview_scroll(1, "units"))

        self.refrescar()
        self.after(500, self.tick)

    # -- dibujo
    def dibujar_cd(self, c, x, y, sel):
        c.create_oval(x, y, x + 64, y + 64, fill="#7fb3ff" if sel else "#c9d6df",
                      outline="#444", width=2)
        c.create_oval(x + 8, y + 8, x + 56, y + 56, outline="#888")
        c.create_oval(x + 24, y + 24, x + 40, y + 40, fill="white", outline="#444")

    def refrescar(self):
        c = self.canvas
        c.delete("all")
        ancho = max(c.winfo_width(), 200)
        cols = max(1, ancho // 130)
        self.celdas = []
        for i, (id_, carpeta, ruta, url) in enumerate(self.db.todas()):
            x = 20 + (i % cols) * 130
            y = 15 + (i // cols) * 120
            sel = id_ in self.seleccion
            if sel:
                c.create_rectangle(x - 10, y - 8, x + 114, y + 104, fill="#e6f0ff", outline="#4a8af4")
            self.dibujar_cd(c, x + 15, y, sel)
            txt = carpeta if len(carpeta) <= 18 else carpeta[:16] + "…"
            c.create_text(x + 47, y + 82, text=txt, width=115)
            self.celdas.append((x - 10, y - 8, x + 114, y + 104, id_))
        filas = (len(self.celdas) + cols - 1) // cols
        c.configure(scrollregion=(0, 0, ancho, 30 + filas * 120))

    def click(self, e):
        x, y = self.canvas.canvasx(e.x), self.canvas.canvasy(e.y)
        for x1, y1, x2, y2, id_ in self.celdas:
            if x1 <= x <= x2 and y1 <= y <= y2:
                self.seleccion ^= {id_}
                self.refrescar()
                return

    def elegir_carpeta(self):
        d = filedialog.askdirectory(title="Carpeta de descargas")
        if d:
            self.var_dir.set(d)
            self.db.set_config("destino", d)
            messagebox.showinfo("Éxito", "Carpeta de descargas configurada correctamente.")

    # -- ventana de registro
    def ver_registro(self):
        w = tk.Toplevel(self)
        w.title("Registro de listas")
        w.geometry("1100x380")
        w.transient(self)

        var_db = tk.StringVar(value=f"Base de datos: {self.db.db_path}")
        ttk.Label(w, textvariable=var_db).pack(anchor="w", padx=8, pady=(8, 2))

        cols = [("id", 40), ("carpeta", 150), ("url", 260), ("creación", 130),
                ("actualización", 130), ("exportación", 130),
                ("ruta de exportación", 200), ("ruta local", 260)]
        marco = ttk.Frame(w)
        marco.pack(fill="both", expand=True, padx=8, pady=4)
        tabla = ttk.Treeview(marco, columns=[c for c, _ in cols], show="headings")
        for c, ancho in cols:
            tabla.heading(c, text=c.capitalize())
            tabla.column(c, width=ancho, anchor="w")
        sy = ttk.Scrollbar(marco, orient="vertical", command=tabla.yview)
        sx = ttk.Scrollbar(w, orient="horizontal", command=tabla.xview)
        tabla.configure(yscrollcommand=sy.set, xscrollcommand=sx.set)
        sy.pack(side="right", fill="y")
        tabla.pack(fill="both", expand=True)
        sx.pack(fill="x", padx=8)

        def cargar():
            tabla.delete(*tabla.get_children())
            for fila in self.db.tabla_completa():
                tabla.insert("", "end", values=[v if v is not None else "—" for v in fila])

        def cambiar_bd():
            if self.ocupado:
                messagebox.showwarning("Ocupado", "Espera a que termine la tarea en curso.", parent=w)
                return
            d = filedialog.askdirectory(title="Nueva carpeta para la base de datos", parent=w)
            if not d:
                return
            try:
                self.db.mover(d)
            except Exception as ex:
                messagebox.showerror("Error", str(ex), parent=w)
                return
            var_db.set(f"Base de datos: {self.db.db_path}")
            cargar()
            messagebox.showinfo("Éxito", "Base de datos movida correctamente.", parent=w)

        botones = ttk.Frame(w)
        botones.pack(fill="x", padx=8, pady=8)
        ttk.Button(botones, text="Refrescar", command=cargar).pack(side="left")
        ttk.Button(botones, text="Cambiar ubicación de la BD", command=cambiar_bd).pack(
            side="left", padx=6)
        ttk.Button(botones, text="Cerrar", command=w.destroy).pack(side="right")
        cargar()

    # -- tareas en segundo plano
    def tarea(self, fn, mensaje_ok):
        """fn recibe un objeto Progreso y puede devolver texto extra para el aviso final."""
        if self.ocupado:
            messagebox.showwarning("Ocupado", "Hay otra tarea en curso.")
            return
        self.ocupado = True
        self.prog = Progreso()

        def run():
            try:
                extra = fn(self.prog) or ""
                self.q.put(("ok", mensaje_ok + extra))
            except Exception as ex:
                self.q.put(("error", str(ex)))
        threading.Thread(target=run, daemon=True).start()

    def tick(self):
        if self.ocupado and self.prog:
            p = self.prog
            self.estado.set(p.texto_estado())
            self.tiempo.set(p.texto_tiempo())
            pct = p.porcentaje()
            if pct is None:
                self.barra.configure(mode="indeterminate")
                self.barra.step(5)
            else:
                self.barra.configure(mode="determinate")
                self.barra["value"] = pct
        try:
            while True:
                tipo, msg = self.q.get_nowait()
                self.ocupado = False
                self.prog = None
                self.estado.set("Listo")
                self.tiempo.set("Tiempo estimado: —")
                self.barra.configure(mode="determinate")
                self.barra["value"] = 0
                self.refrescar()
                if tipo == "ok":
                    messagebox.showinfo("Éxito", msg)
                else:
                    messagebox.showerror("Error", msg)
        except queue.Empty:
            pass
        self.after(500, self.tick)

    # -- botones
    def nueva_lista(self):
        w = tk.Toplevel(self)
        w.title("Nueva lista")
        w.geometry("480x110")
        w.transient(self)
        ttk.Label(w, text="URL de la lista de reproducción:").pack(anchor="w", padx=10, pady=(10, 2))
        var = tk.StringVar()
        ent = ttk.Entry(w, textvariable=var)
        ent.pack(fill="x", padx=10)
        ent.focus()

        def aceptar():
            url = var.get().strip()
            if not url:
                messagebox.showerror("Error", "Ingresa una URL.", parent=w)
                return
            w.destroy()
            self.descargar_nueva(url)
        ttk.Button(w, text="Descargar", command=aceptar).pack(pady=10)

    def descargar_nueva(self, url):
        base = self.var_dir.get()

        def fn(prog):
            prog.etapa("Consultando la lista en YouTube…")
            info = info_lista(url)
            total = len(ids_remotos(info))
            nombre = limpiar_nombre(info.get("title") or "lista")
            ruta = os.path.join(base, nombre)
            prog.prefijo = f"[{nombre}] "
            prog.etapa("Descargando canciones", total)
            descargar_lista(url, ruta, prog)
            self.db.agregar(nombre, ruta, url)
            omitidas = total - prog.hechas
            return f"\n{prog.hechas} de {total} canciones descargadas." + (
                f"\n{omitidas} no estaban disponibles y se omitieron." if omitidas > 0 else "")
        self.tarea(fn, "Lista descargada con éxito.")

    def _seleccionadas(self):
        if not self.seleccion:
            messagebox.showwarning("Aviso", "Selecciona al menos una lista.")
            return None
        return [self.db.obtener(i) for i in self.seleccion]

    def actualizar(self):
        sel = self._seleccionadas()
        if not sel:
            return

        def fn(prog):
            nuevas = borradas = omitidas = 0
            for n, (id_, carpeta, ruta, url, _) in enumerate(sel, 1):
                prog.prefijo = f"[Lista {n}/{len(sel)}: {carpeta}] "
                d, b, o = sincronizar(url, ruta, prog)
                nuevas, borradas, omitidas = nuevas + d, borradas + b, omitidas + o
                self.db.marcar_actualizada(id_)
            return (f"\n{nuevas} canciones nuevas, {borradas} eliminadas"
                    + (f", {omitidas} no disponibles." if omitidas else "."))
        self.tarea(fn, "Listas actualizadas con éxito.")

    def exportar(self):
        sel = self._seleccionadas()
        if not sel:
            return
        d = filedialog.askdirectory(title="Destino de la exportación")
        if not d:
            return

        def fn(prog):
            for n, (id_, carpeta, ruta, url, _) in enumerate(sel, 1):
                prog.prefijo = f"[Lista {n}/{len(sel)}: {carpeta}] "
                destino = exportar_lista(ruta, d, prog)
                self.db.marcar_exportada(id_, destino)
        self.tarea(fn, "Listas exportadas con éxito.")

    def borrar(self):
        sel = self._seleccionadas()
        if not sel:
            return
        if not messagebox.askyesno("Confirmar",
                                   "¿Borrar las listas seleccionadas y sus archivos descargados?"):
            return

        def fn(prog):
            prog.etapa("Borrando listas", len(sel))
            for id_, carpeta, ruta, url, _ in sel:
                prog.detalle = carpeta
                shutil.rmtree(ruta, ignore_errors=True)
                self.db.borrar(id_)
                prog.avanzar()
            self.seleccion.clear()
        self.tarea(fn, "Listas borradas con éxito.")


if __name__ == "__main__":
    App().mainloop()