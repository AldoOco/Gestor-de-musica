import os
import re
import shutil
import sqlite3
import threading
import queue
import tkinter as tk
from datetime import datetime
from tkinter import ttk, filedialog, messagebox

import yt_dlp

APP_DIR = os.path.join(os.path.expanduser("~"), ".yt_playlist_manager")
os.makedirs(APP_DIR, exist_ok=True)
DB_PATH = os.path.join(APP_DIR, "registro.db")


def carpeta_musica():
    for n in ("Music", "Música"):
        p = os.path.join(os.path.expanduser("~"), n)
        if os.path.isdir(p):
            return p
    return os.path.expanduser("~")


# ---------------------------------------------------------------- Base de datos
class Registro:
    def __init__(self):
        self.con = sqlite3.connect(DB_PATH, check_same_thread=False)
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


# ---------------------------------------------------------------- Lógica yt-dlp
def limpiar_nombre(n):
    return re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", n).strip().rstrip(".") or "lista"


def opts_base(carpeta=None, archivo_registro=None):
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


def descargar_lista(url, carpeta):
    os.makedirs(carpeta, exist_ok=True)
    with yt_dlp.YoutubeDL(opts_base(carpeta, os.path.join(carpeta, ".archivo_descargas.txt"))) as ydl:
        ydl.download([url])


def sincronizar(url, carpeta):
    info = info_lista(url)
    remotos = ids_remotos(info)
    locales = ids_locales(carpeta)
    arch = os.path.join(carpeta, ".archivo_descargas.txt")
    for vid, ruta in locales.items():
        if vid not in remotos:
            try:
                os.remove(ruta)
            except OSError:
                pass
    if os.path.exists(arch):
        with open(arch, encoding="utf-8") as f:
            lineas = [l for l in f if l.split()[-1:] and l.split()[-1] in remotos]
        with open(arch, "w", encoding="utf-8") as f:
            f.writelines(lineas)
    descargar_lista(url, carpeta)


def exportar_lista(origen, destino_base):
    destino = os.path.join(destino_base, os.path.basename(origen))
    os.makedirs(destino, exist_ok=True)
    src = {f for f in os.listdir(origen) if f.lower().endswith(".mp3")}
    dst = {f for f in os.listdir(destino) if f.lower().endswith(".mp3")}
    for f in dst - src:
        os.remove(os.path.join(destino, f))
    for f in src - dst:
        shutil.copy(os.path.join(origen, f), os.path.join(destino, f))
    return destino


# ---------------------------------------------------------------- Interfaz
class App(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title("Gestor de listas de música")
        self.geometry("760x520")
        self.db = Registro()
        self.q = queue.Queue()
        self.seleccion = set()
        self.celdas = []
        self.ocupado = False

        top = ttk.Frame(self, padding=8)
        top.pack(fill="x")
        self.var_dir = tk.StringVar(value=self.db.get_config("destino", carpeta_musica()))
        ttk.Entry(top, textvariable=self.var_dir, state="readonly").pack(
            side="left", fill="x", expand=True)
        ttk.Button(top, text="Seleccionar carpeta", command=self.elegir_carpeta).pack(
            side="left", padx=(6, 0))

        bar = ttk.Frame(self, padding=(8, 0))
        bar.pack(fill="x")
        for txt, cmd in [("Nueva lista", self.nueva_lista), ("Actualizar", self.actualizar),
                         ("Exportar", self.exportar), ("Borrar", self.borrar)]:
            ttk.Button(bar, text=txt, command=cmd).pack(side="left", padx=(0, 6), pady=6)

        self.estado = tk.StringVar(value="Listo")
        ttk.Label(self, textvariable=self.estado, relief="sunken", anchor="w").pack(
            side="bottom", fill="x")

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
        self.after(150, self.procesar_cola)

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

    def tarea(self, texto, fn, mensaje_ok):
        if self.ocupado:
            messagebox.showwarning("Ocupado", "Hay otra tarea en curso.")
            return
        self.ocupado = True
        self.estado.set(texto)

        def run():
            try:
                fn()
                self.q.put(("ok", mensaje_ok))
            except Exception as ex:
                self.q.put(("error", str(ex)))
        threading.Thread(target=run, daemon=True).start()

    def procesar_cola(self):
        try:
            while True:
                tipo, msg = self.q.get_nowait()
                self.ocupado = False
                self.estado.set("Listo")
                self.refrescar()
                if tipo == "ok":
                    messagebox.showinfo("Éxito", msg)
                else:
                    messagebox.showerror("Error", msg)
        except queue.Empty:
            pass
        self.after(150, self.procesar_cola)

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

        def fn():
            info = info_lista(url)
            nombre = limpiar_nombre(info.get("title") or "lista")
            ruta = os.path.join(base, nombre)
            descargar_lista(url, ruta)
            self.db.agregar(nombre, ruta, url)
        self.tarea("Descargando lista…", fn, "Lista descargada con éxito.")

    def _seleccionadas(self):
        if not self.seleccion:
            messagebox.showwarning("Aviso", "Selecciona al menos una lista.")
            return None
        return [self.db.obtener(i) for i in self.seleccion]

    def actualizar(self):
        sel = self._seleccionadas()
        if not sel:
            return

        def fn():
            for id_, carpeta, ruta, url, _ in sel:
                sincronizar(url, ruta)
                self.db.marcar_actualizada(id_)
        self.tarea("Actualizando…", fn, "Listas actualizadas con éxito.")

    def exportar(self):
        sel = self._seleccionadas()
        if not sel:
            return
        d = filedialog.askdirectory(title="Destino de la exportación")
        if not d:
            return

        def fn():
            for id_, carpeta, ruta, url, _ in sel:
                destino = exportar_lista(ruta, d)
                self.db.marcar_exportada(id_, destino)
        self.tarea("Exportando…", fn, "Listas exportadas con éxito.")

    def borrar(self):
        sel = self._seleccionadas()
        if not sel:
            return
        if not messagebox.askyesno("Confirmar",
                                   "¿Borrar las listas seleccionadas y sus archivos descargados?"):
            return

        def fn():
            for id_, carpeta, ruta, url, _ in sel:
                shutil.rmtree(ruta, ignore_errors=True)
                self.db.borrar(id_)
            self.seleccion.clear()
        self.tarea("Borrando…", fn, "Listas borradas con éxito.")


if __name__ == "__main__":
    App().mainloop()