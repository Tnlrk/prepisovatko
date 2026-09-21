"""Přepisovátko — GUI (CustomTkinter + drag&drop).

Tenká vrstva nad core.py: editovatelná a přeskupitelná fronta souborů, drag&drop,
jazyk, diarizace (auto i pevný počet mluvčích), okamžité zastavení, světlý/tmavý
režim, nápověda. Veškerá logika přepisu je v core.py.
"""
from __future__ import annotations

import io
import sys

# Pod pythonw.exe (bez konzole) jsou sys.stdout/stderr None — jakýkoli výpis
# (např. warning customtkinteru o zablokovaném fontu na firemních PC) by pak
# shodil celou aplikaci na AttributeError ještě při importu. Podstrčíme
# bezpečné buffery DŘÍV, než se importuje cokoli, co by mohlo psát.
import faulthandler
import os
from pathlib import Path

# Trvalý log (%LOCALAPPDATA%\Prepisovatko\prepisovatko.log). faulthandler do něj
# zapíše i NATIVNÍ pád (onnxruntime, Tk…), po kterém by jinak aplikace jen
# beze slova zmizela — díky tomu jde pád u uživatele dohledat.
LOG_DIR = Path(os.environ.get("LOCALAPPDATA") or Path.home()) / "Prepisovatko"
LOG_FILE = LOG_DIR / "prepisovatko.log"
_logf = None
try:
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    if LOG_FILE.exists() and LOG_FILE.stat().st_size > 2_000_000:
        LOG_FILE.replace(LOG_FILE.with_suffix(".old.log"))
    _logf = open(LOG_FILE, "a", encoding="utf-8", buffering=1)
    faulthandler.enable(_logf)
except OSError:
    _logf = None

# Pod pythonw.exe (bez konzole) jsou sys.stdout/stderr None — jakýkoli výpis
# by pak shodil celou aplikaci na AttributeError ještě při importu. Přesměrujeme
# je do logu (nebo aspoň do paměti) DŘÍV, než se importuje cokoli dalšího.
if sys.stdout is None:
    sys.stdout = _logf or io.StringIO()
if sys.stderr is None:
    sys.stderr = _logf or io.StringIO()


def flog(msg: str) -> None:
    if _logf:
        try:
            _logf.write(f"{time.strftime('%Y-%m-%d %H:%M:%S')}  {msg}\n")
        except Exception:
            pass


import queue
import threading
import time
import traceback
from dataclasses import dataclass
from tkinter import filedialog

# Firemní politika „Untrusted Font Blocking" blokuje načtení CTk fontu pro
# zaoblené rohy → CTk při importu vypíše na stderr děsivý warning („rendering
# quality will be bad!"), který uživatelé hlásí jako chybu. Import proto
# obalíme (warning potichu zachytíme) a místo nouzového 'circle_shapes'
# přepneme na 'polygon_shapes' — font nepotřebuje a vypadá prakticky stejně
# jako plná kvalita.
_orig_stderr = sys.stderr
sys.stderr = io.StringIO()
try:
    import customtkinter as ctk
finally:
    sys.stderr = _orig_stderr
try:
    from customtkinter.windows.widgets.core_rendering import DrawEngine
    if DrawEngine.preferred_drawing_method == "circle_shapes":
        DrawEngine.preferred_drawing_method = "polygon_shapes"
except Exception:
    pass

from tkinterdnd2 import DND_FILES, TkinterDnD

try:
    import psutil  # patička s vytížením CPU/RAM; bez něj se jen nezobrazí
except ImportError:
    psutil = None

import core

__version__ = "1.2.2"
ICON_PATH = core.ROOT / "assets" / "icon.ico"  # core.ROOT funguje i v .app bundlu

# Jazyk → (whisper kód, slovo pro mluvčího ve výstupu)
LANGS: dict[str, tuple[str, str]] = {
    "Čeština": ("cs", "Mluvčí"),
    "English": ("en", "Speaker"),
    "Slovenčina": ("sk", "Rečník"),
    "Deutsch": ("de", "Sprecher"),
    "Français": ("fr", "Locuteur"),
    "Polski": ("pl", "Mówca"),
    "Auto-detekce": ("auto", "Speaker"),
}
SPEAKER_CHOICES = ["Automaticky"] + [str(i) for i in range(1, 11)]

_AUDIO_EXT = "*.mp3 *.wav *.m4a *.flac *.ogg *.opus *.aac *.wma *.mka"
_VIDEO_EXT = "*.mp4 *.mkv *.mov *.avi *.webm *.m4v *.wmv *.flv *.ts *.mpg *.mpeg"
MEDIA_TYPES = [("Audio i video", f"{_AUDIO_EXT} {_VIDEO_EXT}"),
               ("Video", _VIDEO_EXT), ("Audio", _AUDIO_EXT),
               ("Všechny soubory", "*.*")]
MEDIA_EXTS = {e.lstrip("*") for e in (_AUDIO_EXT + " " + _VIDEO_EXT).split()}

GRAY = ("gray75", "gray25")  # (light, dark) pozadí karet/sekundárních prvků
# status → (glyph, (barva_světlý, barva_tmavý)). Barvy laděné na kontrast vůči
# GRAY pozadí; stav navíc nese i tvar glyfu (ne jen barvu) kvůli barvosleposti.
STATUS = {
    "pending": ("•", ("gray35", "gray70")),
    "processing": ("▶", ("#1f6aa5", "#5aa6e0")),
    "done": ("✓", ("#15703f", "#4cc38a")),
    "error": ("✗", ("#a8291b", "#e87060")),
    "cancelled": ("⊘", ("#7a4e0a", "#e0a44c")),
}


def _fmt(sec: float) -> str:
    sec = int(max(0, sec))
    return f"{sec // 60}:{sec % 60:02d}"


@dataclass
class Item:
    id: int
    path: Path
    status: str = "pending"
    info: str = ""
    row: ctk.CTkFrame | None = None
    glyph: ctk.CTkLabel | None = None
    info_lbl: ctk.CTkLabel | None = None
    move: ctk.CTkFrame | None = None      # rámeček s ↑/↓ (jen pending)
    del_btn: ctk.CTkButton | None = None  # ✕ (jen pending)


class App(ctk.CTk, TkinterDnD.DnDWrapper):
    def __init__(self) -> None:
        super().__init__()
        self.TkdndVersion = TkinterDnD._require(self)

        self.title("Přepisovátko")
        self.geometry("620x840")
        self.minsize(560, 740)
        ctk.set_appearance_mode("system")
        ctk.set_default_color_theme("blue")

        self.items: list[Item] = []
        self._next_id = 0
        self.lock = threading.Lock()
        self.worker: threading.Thread | None = None
        self.cancel = threading.Event()
        self.stop_all = threading.Event()
        self.msgq: queue.Queue = queue.Queue()
        self.last_out_dir: str | None = None
        self.out_dir: Path | None = None
        self.cur: dict = {}
        # výjimky v Tk callbackách do logu (jinak by zmizely v neviditelné konzoli)
        self.report_callback_exception = (
            lambda *exc: flog("TK CALLBACK ERROR:\n"
                              + "".join(traceback.format_exception(*exc))))
        flog(f"=== Přepisovátko {__version__} start | Python {sys.version.split()[0]} "
             f"| {sys.platform} | CPU {os.cpu_count()} ===")

        self._build()
        self._apply_icon(self)        # hned…
        self.after(300, lambda: self._apply_icon(self))   # …a po CTk defaultu
        self._center_and_raise()
        self.drop_target_register(DND_FILES)
        self.dnd_bind("<<Drop>>", self._on_drop)
        # klik kamkoli mimo pole počtu mluvčích mu sebere fokus (kurzor nebliká)
        self.bind_all("<Button-1>", self._click_anywhere, add="+")
        self.after(100, self._drain)
        self.after(250, self._tick)
        if psutil:
            psutil.cpu_percent(None)  # první čtení jen nastartuje měření
            self.after(1000, self._sys_tick)

    @staticmethod
    def _apply_icon(window) -> None:
        # .ico umí jen Windows; macOS .app má ikonu z bundlu (icon.icns)
        if core.IS_WIN and ICON_PATH.exists():
            try:
                window.iconbitmap(str(ICON_PATH))
            except Exception:
                pass

    def _center_and_raise(self) -> None:
        """Otevřít uprostřed obrazovky a navrch nad ostatními okny."""
        self.update_idletasks()
        w, h = 620, 840
        x = max(0, (self.winfo_screenwidth() - w) // 2)
        y = max(0, (self.winfo_screenheight() - h) // 2 - 20)
        self.geometry(f"{w}x{h}+{x}+{y}")
        self.lift()
        self.focus_force()
        # topmost jen krátce při startu — vynese okno navrch, ale pak
        # nepřekáží trvale nad vším
        self.attributes("-topmost", True)
        self.after(1500, lambda: self.attributes("-topmost", False))

    def _click_anywhere(self, event) -> None:
        f = self.focus_get()
        if f is None:
            return
        nspk_path = str(self.nspk)
        # fokus má vnitřní entry comboboxu a klik šel jinam → defokus
        if str(f).startswith(nspk_path) and not str(event.widget).startswith(nspk_path):
            self.focus()

    # ----------------------------------------------------------- helpers UI
    @staticmethod
    def _section(parent, title: str) -> ctk.CTkFrame:
        frame = ctk.CTkFrame(parent)
        frame.pack(fill="x", padx=18, pady=(0, 12))
        ctk.CTkLabel(frame, text=title, font=("", 11, "bold"),
                     text_color=("gray25", "gray75")).pack(anchor="w", padx=14, pady=(10, 0))
        return frame

    # ----------------------------------------------------------------- UI
    def _build(self) -> None:
        # ── Hlavička ──────────────────────────────────────────────
        top = ctk.CTkFrame(self, fg_color="transparent")
        top.pack(fill="x", padx=18, pady=(14, 12))
        head = ctk.CTkFrame(top, fg_color="transparent")
        head.pack(side="left")
        ctk.CTkLabel(head, text="Přepisovátko", font=("", 26, "bold")).pack(anchor="w")
        ctk.CTkLabel(head, text="Lokální přepis nahrávek s detekcí mluvčích",
                     text_color=("gray25", "gray75")).pack(anchor="w")
        ctk.CTkButton(top, text="?", width=36, height=36, command=self._help).pack(
            side="right")
        self._dark = ctk.get_appearance_mode() == "Dark"
        self.theme_btn = ctk.CTkButton(top, text="☀" if self._dark else "☾",
                                       width=36, height=36, fg_color=GRAY,
                                       font=("Segoe UI Symbol", 16),
                                       hover_color=("gray60", "gray40"),
                                       text_color=("gray10", "gray90"),
                                       command=self._toggle_theme)
        self.theme_btn.pack(side="right", padx=(0, 8))

        # ── Nahrávky (fronta) ─────────────────────────────────────
        files = self._section(self, "NAHRÁVKY")
        bar = ctk.CTkFrame(files, fg_color="transparent")
        bar.pack(fill="x", padx=14, pady=(6, 4))
        ctk.CTkButton(bar, text="Přidat soubory…", width=150,
                      command=self._pick_files).pack(side="left")
        ctk.CTkButton(bar, text="Vyčistit hotové", width=120, fg_color=GRAY,
                      text_color=("gray10", "gray90"), hover_color=("gray60", "gray40"),
                      command=self._clear_done).pack(side="left", padx=8)
        ctk.CTkButton(bar, text="Vyčistit vše", width=100, fg_color=GRAY,
                      text_color=("gray10", "gray90"), hover_color=("gray60", "gray40"),
                      command=self._clear_all).pack(side="left")
        self.count_lbl = ctk.CTkLabel(bar, text="", text_color=("gray25", "gray75"))
        self.count_lbl.pack(side="right")
        self.list = ctk.CTkScrollableFrame(files, height=200, fg_color="transparent")
        self.list.pack(fill="both", expand=True, padx=14, pady=(0, 12))
        self.hint = ctk.CTkLabel(
            self.list,
            text="Přetáhni sem audio nebo video soubory\n"
                 "(nebo použij tlačítko Přidat soubory)",
            text_color=("gray25", "gray75"), justify="center")
        self.hint.pack(pady=44)

        # ── Nastavení ─────────────────────────────────────────────
        opt = self._section(self, "NASTAVENÍ")
        r1 = ctk.CTkFrame(opt, fg_color="transparent")
        r1.pack(fill="x", padx=14, pady=(6, 4))
        ctk.CTkLabel(r1, text="Jazyk nahrávky:").pack(side="left")
        self.lang = ctk.CTkOptionMenu(r1, values=list(LANGS), width=150)
        self.lang.set("Čeština")
        self.lang.pack(side="left", padx=(6, 0))
        r2 = ctk.CTkFrame(opt, fg_color="transparent")
        r2.pack(fill="x", padx=14, pady=4)
        self.diar = ctk.CTkCheckBox(r2, text="Detekce mluvčích (diarizace)",
                                    command=self._toggle_diar)
        self.diar.select()
        self.diar.pack(side="left")
        r2b = ctk.CTkFrame(opt, fg_color="transparent")
        r2b.pack(fill="x", padx=14, pady=4)
        ctk.CTkLabel(r2b, text="Počet mluvčích:").pack(side="left")
        self.nspk = ctk.CTkComboBox(r2b, values=SPEAKER_CHOICES, width=135,
                                    command=lambda _v: self._nspk_changed())
        self.nspk.set("Automaticky")
        self.nspk.pack(side="left", padx=(6, 10))
        # bindy přímo na vnitřní entry comboboxu (CTk wrapper je nepředává vždy)
        entry = getattr(self.nspk, "_entry", self.nspk)
        entry.bind("<FocusIn>", lambda e: self._nspk_focus_in())
        entry.bind("<FocusOut>", lambda e: self._nspk_focus_out())
        entry.bind("<KeyRelease>", lambda e: self._nspk_changed())
        entry.bind("<Return>", lambda e: self.focus())  # Enter = potvrdit
        self.nspk_info = ctk.CTkLabel(r2b, text="", width=24, font=("", 15, "bold"))
        self.nspk_info.pack(side="left")
        r3 = ctk.CTkFrame(opt, fg_color="transparent")
        r3.pack(fill="x", padx=14, pady=(4, 12))
        ctk.CTkButton(r3, text="Výstupní složka…", width=150, fg_color=GRAY,
                      text_color=("gray10", "gray90"), hover_color=("gray60", "gray40"),
                      command=self._pick_out).pack(side="left")
        self.out_lbl = ctk.CTkLabel(r3, text="vedle vstupních souborů",
                                    text_color=("gray25", "gray75"))
        self.out_lbl.pack(side="left", padx=12)

        # ── Akce ──────────────────────────────────────────────────
        act = ctk.CTkFrame(self, fg_color="transparent")
        act.pack(fill="x", padx=18, pady=(0, 2))
        self.start_btn = ctk.CTkButton(act, text="Spustit přepis", height=42, width=170,
                                       font=("", 15, "bold"),
                                       text_color_disabled=("gray85", "gray60"),
                                       command=self._start)
        self.start_btn.pack(side="left")
        self.stop_btn = ctk.CTkButton(act, text="Zastavit", height=42, width=110,
                                      text_color_disabled=("gray40", "gray60"),
                                      command=self._stop)
        self.stop_btn.pack(side="left", padx=8)
        self._stop_enabled(False)
        self.open_btn = ctk.CTkButton(act, text="Otevřít výstup", height=42, width=140,
                                      fg_color=GRAY, text_color=("gray10", "gray90"),
                                      text_color_disabled=("gray45", "gray55"),
                                      hover_color=("gray60", "gray40"),
                                      state="disabled", command=self._open_out)
        self.open_btn.pack(side="right")

        # ── Progress + stav + log ─────────────────────────────────
        self.progress = ctk.CTkProgressBar(self, height=14)
        self.progress.set(0)
        self.progress.pack(fill="x", padx=18, pady=(14, 4))
        self.status = ctk.CTkLabel(self, text="Připraveno", anchor="w")
        self.status.pack(fill="x", padx=18)
        # ── Patička: vytížení systému (kotví se dolů PŘED logem,
        #    aby ji log při těsném okně nevytlačil) ─────────────────
        if psutil:
            self.sys_lbl = ctk.CTkLabel(self, text="", font=("", 11), anchor="e",
                                        text_color=("gray25", "gray75"))
            self.sys_lbl.pack(side="bottom", fill="x", padx=18, pady=(0, 8))

        self.log = ctk.CTkTextbox(self, height=100, fg_color=GRAY)
        self.log.pack(fill="both", expand=False, padx=18, pady=(8, 6))
        self.log.configure(state="disabled")

    # ----------------------------------------------------------- fronta
    def _pick_files(self) -> None:
        self._add_paths(filedialog.askopenfilenames(
            title="Vyber audio nebo video soubory", filetypes=MEDIA_TYPES))

    def _on_drop(self, event) -> None:
        self._add_paths(self.tk.splitlist(event.data))

    def _add_paths(self, paths) -> None:
        have = {it.path for it in self.items}
        added = 0
        for p in paths:
            pp = Path(str(p))
            if not pp.is_file() or pp in have or pp.suffix.lower() not in MEDIA_EXTS:
                continue
            it = Item(id=self._next_id, path=pp)
            self._next_id += 1
            with self.lock:
                self.items.append(it)
            self._make_row(it)
            have.add(pp)
            added += 1
        if added:
            self.hint.pack_forget()
        self._refresh_count()

    def _make_row(self, it: Item) -> None:
        row = ctk.CTkFrame(self.list, fg_color=GRAY, corner_radius=8)
        row.pack(fill="x", pady=3, padx=2)
        it.row = row
        # přeskupení (jen pro pending)
        mv = ctk.CTkFrame(row, fg_color="transparent")
        mv.pack(side="left", padx=(6, 2))
        ctk.CTkButton(mv, text="▲", width=24, height=18, fg_color=("gray60", "gray40"),
                      hover_color=("gray50", "gray50"),
                      text_color=("gray10", "gray90"),
                      command=lambda i=it: self._move(i, -1)).pack()
        ctk.CTkButton(mv, text="▼", width=24, height=18, fg_color=("gray60", "gray40"),
                      hover_color=("gray50", "gray50"),
                      text_color=("gray10", "gray90"),
                      command=lambda i=it: self._move(i, +1)).pack(pady=(2, 0))
        it.move = mv
        g, color = STATUS[it.status]
        it.glyph = ctk.CTkLabel(row, text=g, text_color=color, width=18)
        it.glyph.pack(side="left", padx=(4, 4))
        ctk.CTkLabel(row, text=it.path.name, anchor="w").pack(
            side="left", fill="x", expand=True, pady=6)
        it.info_lbl = ctk.CTkLabel(row, text="", text_color=("gray25", "gray75"))
        it.info_lbl.pack(side="right", padx=6)
        it.del_btn = ctk.CTkButton(row, text="✕", width=28, fg_color=("gray60", "gray40"),
                                   hover_color="#a23b2d",
                                   text_color=("gray10", "gray90"),
                                   command=lambda i=it: self._remove(i))
        it.del_btn.pack(side="right", padx=(0, 8))

    def _move(self, it: Item, delta: int) -> None:
        if it.status != "pending":
            return
        with self.lock:
            i = self.items.index(it)
            j = i + delta
            if 0 <= j < len(self.items):
                self.items[i], self.items[j] = self.items[j], self.items[i]
        self._repack_rows()

    def _repack_rows(self) -> None:
        for it in self.items:
            if it.row:
                it.row.pack_forget()
        for it in self.items:
            if it.row:
                it.row.pack(fill="x", pady=3, padx=2)

    def _remove(self, it: Item) -> None:
        if it.status != "pending":
            return
        with self.lock:
            if it in self.items:
                self.items.remove(it)
        if it.row:
            it.row.destroy()
        self._refresh_count()
        if not self.items:
            self.hint.pack(pady=44)

    def _set_status(self, item_id: int, status: str, info: str) -> None:
        it = next((x for x in self.items if x.id == item_id), None)
        if not it:
            return
        it.status, it.info = status, info
        g, color = STATUS[status]
        if it.glyph:
            it.glyph.configure(text=g, text_color=color)
        if it.info_lbl:
            it.info_lbl.configure(text=info)
        if status != "pending":  # zpracovávané/hotové už nejdou přesouvat ani mazat
            for w in (it.move, it.del_btn):
                if w:
                    w.destroy()
            it.move = it.del_btn = None

    def _clear_done(self) -> None:
        for it in [x for x in self.items if x.status in ("done", "error", "cancelled")]:
            with self.lock:
                self.items.remove(it)
            if it.row:
                it.row.destroy()
        self._refresh_count()
        if not self.items:
            self.hint.pack(pady=44)

    def _clear_all(self) -> None:
        if self.worker and self.worker.is_alive():
            return
        for it in list(self.items):
            if it.row:
                it.row.destroy()
        with self.lock:
            self.items.clear()
        self.hint.pack(pady=44)
        self._refresh_count()

    def _refresh_count(self) -> None:
        n = len(self.items)
        done = sum(1 for x in self.items if x.status == "done")
        self.count_lbl.configure(text=f"{done}/{n} hotovo" if n else "")

    def _parse_nspk(self) -> tuple[int | None, bool]:
        """→ (počet | None pro automat, je_vstup_platný)"""
        raw = self.nspk.get().strip()
        if not raw or raw.lower().startswith("auto"):
            return None, True
        if raw.isdigit() and 1 <= int(raw) <= 99:
            return int(raw), True
        return None, False

    def _nspk_focus_in(self) -> None:
        """Klik do pole s „Automaticky" → vyprázdnit a čekat na zadání."""
        if self.nspk.get().strip().lower().startswith("auto"):
            self.nspk.set("")
            self._nspk_changed()

    def _nspk_focus_out(self) -> None:
        """Odchod z prázdného pole → vrátit „Automaticky"."""
        if not self.nspk.get().strip():
            self.nspk.set("Automaticky")
        self._nspk_changed()

    def _nspk_changed(self) -> None:
        n, ok = self._parse_nspk()
        if not ok:
            self.nspk_info.configure(text="✗", text_color=STATUS["error"][1])
        elif n is None:
            self.nspk_info.configure(text="")
        else:
            self.nspk_info.configure(text="✓", text_color=STATUS["done"][1])

    def _toggle_diar(self) -> None:
        on = bool(self.diar.get())
        self.nspk.configure(state="normal" if on else "disabled")
        if on:
            self._nspk_changed()
        else:
            self.nspk_info.configure(text="")

    def _pick_out(self) -> None:
        d = filedialog.askdirectory(title="Výstupní složka")
        if d:
            self.out_dir = Path(d)
            self.out_lbl.configure(text=str(self.out_dir))

    def _log(self, msg: str) -> None:
        self.log.configure(state="normal")
        self.log.insert("end", msg + "\n")
        self.log.see("end")
        self.log.configure(state="disabled")

    # ----------------------------------------------------------- běh
    def _start(self) -> None:
        if self.worker and self.worker.is_alive():
            return
        if not any(it.status == "pending" for it in self.items):
            self.status.configure(text="Žádné soubory ke zpracování. Přidej nějaké.")
            return
        self.stop_all.clear()
        self.cancel.clear()
        self.start_btn.configure(state="disabled")
        self._stop_enabled(True)
        code, word = LANGS[self.lang.get()]
        nspk, _ = self._parse_nspk()
        opts = dict(do_diarize=bool(self.diar.get()), lang=code, speaker_word=word,
                    num_speakers=nspk)
        self.worker = threading.Thread(target=self._run, args=(opts,), daemon=True)
        self.worker.start()

    def _stop(self) -> None:
        self.stop_all.set()
        self.cancel.set()
        self.status.configure(text="Zastavuji…")
        self._stop_enabled(False)

    def _next_pending(self) -> Item | None:
        with self.lock:
            for it in self.items:
                if it.status == "pending":
                    it.status = "processing"
                    return it
        return None

    def _run(self, opts: dict) -> None:
        used_stems: set[tuple[str, str]] = set()  # (out_dir, stem) — kolize v dávce
        while not self.stop_all.is_set():
            it = self._next_pending()
            if it is None:
                break
            self.cancel.clear()
            out_dir = self.out_dir or it.path.parent
            # dva vstupy stejného jména do téže složky → druhý dostane „ (2)"
            stem, k = it.path.stem, 2
            while (str(out_dir), stem) in used_stems:
                stem = f"{it.path.stem} ({k})"
                k += 1
            used_stems.add((str(out_dir), stem))
            self.msgq.put(("file", (it.id, it.path.name)))
            try:
                size_mb = it.path.stat().st_size / 2**20
            except OSError:
                size_mb = -1
            flog(f"START {it.path} ({size_mb:.1f} MB) opts={opts}")
            try:
                def cb(msg: str, frac: float, eta=None, _id=it.id):
                    self.msgq.put(("progress", (_id, msg, frac, eta)))
                res = core.process(it.path, out_dir, do_diarize=opts["do_diarize"],
                                   num_speakers=opts["num_speakers"], lang=opts["lang"],
                                   speaker_word=opts["speaker_word"], out_stem=stem,
                                   cb=cb, cancel=self.cancel)
                self.last_out_dir = str(out_dir)
                if res.get("diar_error"):
                    info = f"bez mluvčích ⚠ · {_fmt(res['elapsed'])}"
                    self.msgq.put(("log", f"⚠ {it.path.name} — mluvčí se nepodařilo "
                                          "rozpoznat, přepis je uložen bez nich."))
                    flog(f"DIAR FAIL {it.path.name}: {res['diar_error']}")
                elif opts["do_diarize"]:
                    info = f"{res['speakers']} mluvčích · {_fmt(res['elapsed'])}"
                else:
                    info = _fmt(res["elapsed"])
                self.msgq.put(("status", (it.id, "done", info)))
                self.msgq.put(("log", f"✓ {it.path.name} — {info}"))
                flog(f"DONE {it.path.name}: {info}, audio {res.get('duration', 0):.0f}s")
            except core.Cancelled:
                self.msgq.put(("status", (it.id, "cancelled", "zrušeno")))
                self.msgq.put(("log", f"⊘ {it.path.name} — zrušeno"))
                flog(f"CANCELLED {it.path.name}")
            except Exception:
                tb = traceback.format_exc()
                self.msgq.put(("status", (it.id, "error", "chyba")))
                self.msgq.put(("log", f"✗ {it.path.name} — CHYBA:\n" + tb))
                flog(f"ERROR {it.path.name}:\n{tb}")
        self.msgq.put(("done", None))

    # ------------------------------------------------- UI aktualizace
    def _drain(self) -> None:
        try:
            while True:
                kind, payload = self.msgq.get_nowait()
                if kind == "file":
                    item_id, name = payload
                    self.cur = {"name": name, "phase": "Připravuji…",
                                "t0": time.perf_counter(), "eta": None, "eta_t": 0.0}
                    self._set_status(item_id, "processing", "")
                    self.progress.configure(mode="indeterminate")
                    self.progress.start()
                elif kind == "progress":
                    _id, msg, frac, eta = payload
                    self.cur["phase"] = msg
                    if frac >= 0:
                        self.progress.stop()
                        self.progress.configure(mode="determinate")
                        self.progress.set(frac)
                    if eta is not None:
                        self.cur["eta"] = eta
                        self.cur["eta_t"] = time.perf_counter()
                elif kind == "status":
                    item_id, status, info = payload
                    self._set_status(item_id, status, info)
                    self._refresh_count()
                elif kind == "log":
                    self._log(payload)
                elif kind == "done":
                    self._finish()
        except queue.Empty:
            pass
        except Exception:
            traceback.print_exc()  # smyčka obnovy UI musí přežít cokoli
        finally:
            self.after(100, self._drain)

    def _tick(self) -> None:
        if self.cur and self.worker and self.worker.is_alive():
            el = time.perf_counter() - self.cur["t0"]
            eta_txt = ""
            if self.cur.get("eta") is not None:
                # odhad od core plynule odpočítáváme mezi aktualizacemi
                remaining = self.cur["eta"] - (time.perf_counter() - self.cur["eta_t"])
                eta_txt = f" · zbývá ~{_fmt(remaining)}"
            self.status.configure(
                text=f"{self.cur['name']} · {self.cur['phase']} · "
                     f"uplynulo {_fmt(el)}{eta_txt}")
        self.after(250, self._tick)

    def _finish(self) -> None:
        self.progress.stop()
        self.progress.configure(mode="determinate")
        self.progress.set(0)
        self.cur = {}
        self.start_btn.configure(state="normal")
        self._stop_enabled(False)
        done = sum(1 for x in self.items if x.status == "done")
        self.status.configure(text=f"Hotovo. Zpracováno {done} souborů.")
        if self.last_out_dir:
            self.open_btn.configure(state="normal")

    # ----------------------------------------------------------- ostatní
    def _sys_tick(self) -> None:
        """Každou sekundu obnoví patičku s vytížením CPU a RAM."""
        try:
            cpu = psutil.cpu_percent(None)
            vm = psutil.virtual_memory()
            self.sys_lbl.configure(
                text=f"CPU {cpu:.0f} %   ·   "
                     f"RAM {vm.used / 2**30:.1f} / {vm.total / 2**30:.1f} GB "
                     f"({vm.percent:.0f} %)")
        except Exception:
            pass
        self.after(1000, self._sys_tick)

    def _stop_enabled(self, on: bool) -> None:
        """Zastavit je červené jen když je opravdu aktivní — zakázané splyne
        do šedé, aby nemátlo (přístupnost: stav nese barva i vzhled)."""
        if on:
            self.stop_btn.configure(state="normal", fg_color="#a8291b",
                                    hover_color="#852f24", text_color="white")
        else:
            self.stop_btn.configure(state="disabled", fg_color=GRAY)

    def _toggle_theme(self) -> None:
        self._dark = not self._dark
        ctk.set_appearance_mode("dark" if self._dark else "light")
        self.theme_btn.configure(text="☀" if self._dark else "☾")

    def _open_out(self) -> None:
        if not self.last_out_dir:
            return
        try:
            if core.IS_WIN:
                os.startfile(self.last_out_dir)  # noqa: S606
            elif sys.platform == "darwin":
                import subprocess
                subprocess.run(["open", self.last_out_dir], check=False)
            else:
                import subprocess
                subprocess.run(["xdg-open", self.last_out_dir], check=False)
        except OSError:
            self.status.configure(text="Výstupní složku se nepodařilo otevřít "
                                       "(byla přesunuta nebo smazána?).")

    def _help(self) -> None:
        win = ctk.CTkToplevel(self)
        win.title("O aplikaci")
        # vycentrovat nad hlavní okno (ne na výchozí pozici bokem)
        ww, wh = 560, 560
        self.update_idletasks()
        x = self.winfo_rootx() + (self.winfo_width() - ww) // 2
        y = self.winfo_rooty() + (self.winfo_height() - wh) // 2
        win.geometry(f"{ww}x{wh}+{max(0, x)}+{max(0, y)}")
        win.transient(self)        # vždy nad hlavním oknem
        win.resizable(False, False)
        win.after(60, win.grab_set)
        win.after(60, win.lift)
        win.after(80, win.focus_force)
        win.after(300, lambda: self._apply_icon(win))
        ctk.CTkLabel(win, text="Přepisovátko", font=("", 22, "bold")).pack(
            anchor="w", padx=22, pady=(20, 0))
        ctk.CTkLabel(win, text=f"verze {__version__}", text_color=("gray25", "gray75")).pack(
            anchor="w", padx=22)
        txt = ctk.CTkTextbox(win, wrap="word")
        txt.pack(fill="both", expand=True, padx=22, pady=16)
        txt.insert("1.0",
            "Lokální (offline) přepis nahrávek z porad s detekcí mluvčích.\n"
            "Nic se neposílá na internet — vše běží na tomto počítači.\n\n"
            "JAK NA TO\n"
            "1. Přidej soubory tlačítkem nebo je přetáhni do okna.\n"
            "2. Pořadí ve frontě můžeš měnit šipkami ▲▼, soubory odebrat křížkem.\n"
            "3. Vyber jazyk nahrávky a zda chceš detekci mluvčích.\n"
            "   Počet mluvčích nech na „Automaticky“, nebo zadej ručně, pokud ho znáš.\n"
            "4. Spusť přepis. Frontu lze upravovat i během běhu.\n"
            "5. Výsledek najdeš vedle vstupu nebo ve zvolené výstupní složce:\n"
            "   • soubor .txt — čistý přepis textu\n"
            "   • soubor .srt — titulky k videu (s časy, lze načíst v přehrávači)\n\n"
            "PODPOROVANÉ VSTUPY\n"
            "Audio: mp3, wav, m4a, flac, ogg, opus, aac…\n"
            "Video: mp4, mkv, mov, avi, webm… (vytáhne se zvuková stopa)\n\n"
            "DŮLEŽITÉ — VÝKON\n"
            "Během přepisu je procesor vytížený naplno a počítač může být po tu dobu\n"
            "pomalý a hůř použitelný pro jinou práci. U delších nahrávek to může trvat\n"
            "i desítky minut (přepis zhruba 0,5–0,7× délky nahrávky, diarizace přidá\n"
            "~0,1×). Tlačítkem Zastavit lze zpracování kdykoli okamžitě přerušit.\n\n"
            "PŘI POTÍŽÍCH\n"
            "Aplikace si vede záznam (log) — při hlášení chyby pošli správci soubor:\n"
            f"{LOG_FILE}\n\n"
            "Technologie: whisper.cpp (přepis) + sherpa-onnx (diarizace), CPU-only.\n\n"
            "S pomocí AI vytvořil Antonín Lerek v roce 2026\n"
            "tnlrk@tnlrk.cz")
        txt.configure(state="disabled")
        ctk.CTkButton(win, text="Zavřít", command=win.destroy).pack(pady=(0, 18))


if __name__ == "__main__":
    App().mainloop()
