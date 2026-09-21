"""Přepisovátko — jádro orchestrace (bez GUI).

Pipeline: vstupní audio → (ffmpeg) 16 kHz mono WAV → whisper.cpp přepis
         + sherpa-onnx diarizace (kdo kdy mluví) → merge → TXT/SRT.

Vše offline, CPU-only. Importovatelné: GUI volá process(). Spustitelné z CLI
pro testování.

CLI:
    python core.py <audio> [--out-dir DIR] [--no-diarize]
                   [--threshold 0.7] [--lang cs] [--threads N]
"""
from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import tempfile
import threading
import time
import wave
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Optional

import numpy as np

IS_WIN = sys.platform == "win32"

# Kořen aplikace: ve zdrojích = složka tohoto souboru; v PyInstaller bundlu
# (macOS .app) = rozbalené datové soubory (_MEIPASS).
if getattr(sys, "frozen", False):
    ROOT = Path(getattr(sys, "_MEIPASS", Path(sys.executable).parent))
else:
    ROOT = Path(__file__).resolve().parent
BIN = ROOT / "bin"
MODELS = ROOT / "models"

# Binárky: preferuj přibalené v bin/, fallback na systémové (vývoj).
_EXE = ".exe" if IS_WIN else ""
WHISPER_EXE = BIN / f"whisper-cli{_EXE}"
_ff = BIN / f"ffmpeg{_EXE}"
FFMPEG_EXE = _ff if _ff.exists() else "ffmpeg"

WHISPER_MODEL = MODELS / "ggml-large-v3-turbo-q5_0.bin"
SEG_MODEL = MODELS / "diarize" / "sherpa-onnx-pyannote-segmentation-3-0" / "model.onnx"
EMB_MODEL = MODELS / "diarize" / "nemo_en_titanet_small.onnx"

# Windows: ať subprocessy neproblikávají konzolovým oknem (běh pod pythonw).
_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)
_PROGRESS_RE = re.compile(r"progress\s*=\s*(\d+)%")

# Diarizace — produkční defaulty (ověřeno na zaznam.wav, 2 mluvčí).
DEFAULT_THRESHOLD = 0.7      # FastClustering: stabilní dominantní mluvčí, neslučuje
MIN_SPEAKER_FRAC = 0.03      # mluvčí pod 3 % času...
MIN_SPEAKER_SEC = 30.0       # ...A zároveň pod 30 s → šum, přiřadit nejbližšímu
# Diarizace neškáluje s vlákny lineárně — sweet spot ~8, výš se kvůli režii
# zpomaluje (změřeno na OmniBook: 8t=8.4s, 22t=9.9s na 90s audia). Whisper
# naproti tomu vlákna rád využije, proto má vlastní (vyšší) počet.
DIARIZE_MAX_THREADS = 8

ProgressCb = Optional[Callable[[str, float], None]]


class Cancelled(Exception):
    """Zpracování bylo přerušeno uživatelem (Stop)."""


@dataclass
class Seg:
    start: float          # sekundy
    end: float
    text: str = ""
    speaker: int = -1      # 0-indexovaný reálný mluvčí, -1 = nepřiřazeno


def _default_threads() -> int:
    return max(1, (os.cpu_count() or 4) - 2)


def _log(cb: ProgressCb, msg: str, frac: float = -1.0) -> None:
    if cb:
        cb(msg, frac)
    else:
        print(msg, flush=True)


# --------------------------------------------------------------------------- #
# 1) Audio → 16 kHz mono WAV (jeden převod pro whisper i diarizaci)
# --------------------------------------------------------------------------- #
def to_wav16k(src: str | Path, dst_wav: str | Path) -> None:
    cmd = [str(FFMPEG_EXE), "-v", "error", "-y", "-i", str(src),
           "-ac", "1", "-ar", "16000", "-c:a", "pcm_s16le", str(dst_wav)]
    proc = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                          creationflags=_NO_WINDOW)
    if proc.returncode != 0:
        raise RuntimeError(f"ffmpeg selhal: {proc.stderr.decode(errors='replace')}")


def read_wav16k_mono(wav_path: str | Path) -> np.ndarray:
    with wave.open(str(wav_path), "rb") as wf:
        if wf.getframerate() != 16000 or wf.getnchannels() != 1:
            raise RuntimeError(
                f"Neočekávaný formát WAV ({wf.getframerate()} Hz, "
                f"{wf.getnchannels()} kanálů) — ffmpeg konverze selhala?")
        frames = wf.readframes(wf.getnframes())
    return np.frombuffer(frames, dtype=np.int16).astype(np.float32) / 32768.0


def wav_duration(wav_path: str | Path) -> float:
    with wave.open(str(wav_path), "rb") as wf:
        return wf.getnframes() / float(wf.getframerate())


def _rc_str(rc: int) -> str:
    """Návratový kód čitelně; nativní pád na Windows je např. 0xC0000005."""
    if IS_WIN and (rc < 0 or rc > 0xFFFF):
        return f"kód 0x{rc & 0xFFFFFFFF:08X}"
    return f"kód {rc}"


def _stream_proc(cmd: list[str], on_line: Callable[[str], bool],
                 cancel: Optional[threading.Event], *, read_stderr: bool,
                 env: Optional[dict] = None) -> tuple[int, list[str]]:
    """Spustí proces, každý řádek výstupu předá on_line (True = zpracováno,
    jinak se řádek uloží do „ocasu" pro chybové hlášení). Cancel proces zabije.
    Vrací (returncode, posledních ~20 nezpracovaných řádků)."""
    proc = subprocess.Popen(
        cmd, stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL if read_stderr else subprocess.PIPE,
        stderr=subprocess.PIPE if read_stderr else subprocess.STDOUT,
        text=True, encoding="utf-8", errors="replace",
        creationflags=_NO_WINDOW, env=env)
    stream = proc.stderr if read_stderr else proc.stdout
    # Hlídací vlákno: čtení výstupu blokuje mezi řádky, proto cancel řešíme
    # nezávisle — pollujeme každých 0,2 s a proces rovnou zabijeme.
    stop_watch = threading.Event()

    def _watch():
        while not stop_watch.wait(0.2):
            if cancel is not None and cancel.is_set():
                proc.kill()
                return

    watcher = threading.Thread(target=_watch, daemon=True) if cancel else None
    if watcher:
        watcher.start()
    tail: list[str] = []
    try:
        for line in stream:  # type: ignore[union-attr]
            if not on_line(line) and line.strip():
                tail.append(line)
                del tail[:-20]
        proc.wait()
    finally:
        stop_watch.set()
        if watcher:
            watcher.join(timeout=1)
    if cancel is not None and cancel.is_set():
        raise Cancelled()
    return proc.returncode, tail


# --------------------------------------------------------------------------- #
# 2) Přepis (whisper.cpp jako subprocess, JSON výstup)
# --------------------------------------------------------------------------- #
def transcribe(wav_path: str | Path, lang: str = "cs",
               threads: Optional[int] = None, cb: ProgressCb = None,
               cancel: Optional[threading.Event] = None) -> list[Seg]:
    threads = threads or _default_threads()
    with tempfile.TemporaryDirectory() as td:
        prefix = Path(td) / "out"
        cmd = [str(WHISPER_EXE), "-m", str(WHISPER_MODEL), "-l", lang,
               "-t", str(threads), "-pp", "-oj", "-of", str(prefix), "-f", str(wav_path)]
        _log(cb, "Přepisuji…")
        last = [-2]

        def _on_line(line: str) -> bool:
            m = _PROGRESS_RE.search(line)
            if not m:
                return False
            pct = int(m.group(1))
            if pct >= last[0] + 2 or pct == 100:
                last[0] = pct
                _log(cb, f"Přepisuji… {pct} %", pct / 100.0)
            return True

        # whisper-cli píše průběh (-pp) i timings na stderr → streamujeme a parsujeme.
        rc, tail = _stream_proc(cmd, _on_line, cancel, read_stderr=True)
        if rc != 0:
            raise RuntimeError(f"whisper selhal ({_rc_str(rc)}):\n" + "".join(tail))
        data = json.loads(Path(f"{prefix}.json").read_text(encoding="utf-8"))

    segs: list[Seg] = []
    for t in data.get("transcription") or []:
        text = t["text"].strip()
        if not text:
            continue
        segs.append(Seg(start=t["offsets"]["from"] / 1000.0,
                        end=t["offsets"]["to"] / 1000.0, text=text))
    return segs


# --------------------------------------------------------------------------- #
# 3) Diarizace (sherpa-onnx) + post-filtr šumových mluvčích
# --------------------------------------------------------------------------- #
def diarize(samples: np.ndarray, threshold: float = DEFAULT_THRESHOLD,
            num_speakers: Optional[int] = None,
            threads: Optional[int] = None, cb: ProgressCb = None,
            cancel: Optional[threading.Event] = None) -> list[Seg]:
    """num_speakers=None → automat (threshold + post-filtr šumu).
    num_speakers>=1 → pevný počet mluvčích (uživatel ho zná) — žádné zahazování,
    jen přečíslování. Při pevném počtu je threshold ignorován."""
    import sherpa_onnx
    threads = min(threads or _default_threads(), DIARIZE_MAX_THREADS)
    fixed = bool(num_speakers and num_speakers >= 1)
    num_clusters = num_speakers if fixed else -1
    cfg = sherpa_onnx.OfflineSpeakerDiarizationConfig(
        segmentation=sherpa_onnx.OfflineSpeakerSegmentationModelConfig(
            pyannote=sherpa_onnx.OfflineSpeakerSegmentationPyannoteModelConfig(
                model=str(SEG_MODEL)),
            num_threads=threads),
        embedding=sherpa_onnx.SpeakerEmbeddingExtractorConfig(
            model=str(EMB_MODEL), num_threads=threads),
        clustering=sherpa_onnx.FastClusteringConfig(
            num_clusters=num_clusters, threshold=threshold),
        min_duration_on=0.3, min_duration_off=0.5,
    )
    if not cfg.validate():
        raise RuntimeError("Chybná diarizační konfigurace — zkontroluj cesty k modelům")
    sd = sherpa_onnx.OfflineSpeakerDiarization(cfg)

    last_pct = [-5]

    def _cb(done, total):
        if cancel is not None and cancel.is_set():
            return 1  # nenulový návrat = pokus o přerušení diarizace
        if total <= 0:
            return 0
        pct = int(done / total * 100)
        if pct >= last_pct[0] + 5 or done == total:  # throttle: po 5 %
            last_pct[0] = pct
            _log(cb, f"Rozpoznávám mluvčí… {pct} %", done / total)
        return 0

    _log(cb, "Rozpoznávám mluvčí…")
    if cancel is not None and cancel.is_set():
        raise Cancelled()
    raw = sd.process(samples, callback=_cb).sort_by_start_time()
    if cancel is not None and cancel.is_set():
        raise Cancelled()
    segs = [Seg(start=r.start, end=r.end, speaker=r.speaker) for r in raw]
    # Pevný počet → výsledek je autoritativní, jen přečíslovat. Automat → filtr šumu.
    return _renumber(segs) if fixed else _filter_and_renumber(segs)


class DiarizationFailed(RuntimeError):
    """Diarizace selhala (vč. nativního pádu) — přepis lze uložit bez mluvčích."""


def _worker_python() -> Optional[Path]:
    """Python pro podproces: pod pythonw.exe vezmeme sourozence python.exe
    (spolehlivý stdout; okno konzole potlačí CREATE_NO_WINDOW). V PyInstaller
    bundlu samostatný python není → None (diarizace poběží v procesu)."""
    if getattr(sys, "frozen", False):
        return None
    exe = Path(sys.executable)
    if IS_WIN and exe.name.lower() == "pythonw.exe":
        alt = exe.with_name("python.exe")
        if alt.exists():
            return alt
    return exe


def diarize_isolated(wav_path: str | Path, threshold: float = DEFAULT_THRESHOLD,
                     num_speakers: Optional[int] = None,
                     threads: Optional[int] = None, cb: ProgressCb = None,
                     cancel: Optional[threading.Event] = None) -> list[Seg]:
    """Diarizace v samostatném procesu. sherpa-onnx/onnxruntime je nativní kód —
    kdyby spadl (nebo došla paměť), zabil by celou aplikaci bez hlášky. V podprocesu
    se pád projeví jen jako DiarizationFailed a aplikace uloží přepis bez mluvčích."""
    py = _worker_python()
    if py is None:
        samples = read_wav16k_mono(wav_path)
        return diarize(samples, threshold=threshold, num_speakers=num_speakers,
                       threads=threads, cb=cb, cancel=cancel)
    _log(cb, "Rozpoznávám mluvčí…")
    with tempfile.TemporaryDirectory() as td:
        out_json = Path(td) / "diar.json"
        cmd = [str(py), str(Path(__file__).resolve()), "--diar-worker",
               str(wav_path), str(out_json), str(threshold),
               str(num_speakers or 0), str(threads or 0)]
        env = dict(os.environ, PYTHONIOENCODING="utf-8", PYTHONUNBUFFERED="1")

        def _on_line(line: str) -> bool:
            if not line.startswith("@@P "):
                return False
            try:
                f = float(line[4:])
            except ValueError:
                return False
            _log(cb, f"Rozpoznávám mluvčí… {int(f * 100)} %", f)
            return True

        rc, tail = _stream_proc(cmd, _on_line, cancel, read_stderr=False, env=env)
        if rc != 0 or not out_json.exists():
            raise DiarizationFailed(
                f"Rozpoznání mluvčích selhalo ({_rc_str(rc)}).\n" + "".join(tail))
        data = json.loads(out_json.read_text(encoding="utf-8"))
    return [Seg(start=s, end=e, speaker=k) for s, e, k in data]


def _diar_worker(argv: list[str]) -> int:
    """Vstupní bod podprocesu: core.py --diar-worker <wav> <out.json> <thr> <n> <t>."""
    wav, out_json, thr, nspk, thr_n = argv
    samples = read_wav16k_mono(wav)

    def _cb(_m: str, f: float) -> None:
        if f >= 0:
            print(f"@@P {f:.4f}", flush=True)

    segs = diarize(samples, threshold=float(thr), num_speakers=int(nspk) or None,
                   threads=int(thr_n) or None, cb=_cb)
    Path(out_json).write_text(
        json.dumps([[s.start, s.end, s.speaker] for s in segs]), encoding="utf-8")
    return 0


def _renumber(segs: list[Seg]) -> list[Seg]:
    """Přečísluje mluvčí 0..k-1 podle prvního výskytu, seřadí podle času."""
    order: dict[int, int] = {}
    for s in sorted(segs, key=lambda x: x.start):
        if s.speaker not in order:
            order[s.speaker] = len(order)
    for s in segs:
        s.speaker = order[s.speaker]
    return sorted(segs, key=lambda x: x.start)


def _filter_and_renumber(segs: list[Seg]) -> list[Seg]:
    """Zahodí šumové mluvčí (pod prahem času), jejich segmenty přiřadí
    nejbližšímu reálnému mluvčímu, a reálné mluvčí přečísluje 0..k-1
    podle prvního výskytu."""
    if not segs:
        return segs
    totals: dict[int, float] = {}
    for s in segs:
        totals[s.speaker] = totals.get(s.speaker, 0.0) + (s.end - s.start)
    total_time = sum(totals.values())

    kept = {spk for spk, t in totals.items()
            if t >= MIN_SPEAKER_FRAC * total_time or t >= MIN_SPEAKER_SEC}
    if not kept:  # pojistka: nech aspoň nejvýraznějšího
        kept = {max(totals, key=totals.get)}

    kept_segs = [s for s in segs if s.speaker in kept]
    # přiřaď segmenty zahozených mluvčích nejbližšímu kept segmentu (podle mezery)
    for s in segs:
        if s.speaker in kept:
            continue
        mid = (s.start + s.end) / 2
        nearest = min(kept_segs,
                      key=lambda k: 0 if k.start <= mid <= k.end
                      else min(abs(k.start - mid), abs(k.end - mid)))
        s.speaker = nearest.speaker

    return _renumber(segs)


# --------------------------------------------------------------------------- #
# 4) Merge: každé přepsané větě přiřaď mluvčího podle časového překryvu
# --------------------------------------------------------------------------- #
def merge(transcript: list[Seg], diar: list[Seg]) -> list[Seg]:
    if not diar:
        return transcript
    for t in transcript:
        overlap: dict[int, float] = {}
        for d in diar:
            lo, hi = max(t.start, d.start), min(t.end, d.end)
            if hi > lo:
                overlap[d.speaker] = overlap.get(d.speaker, 0.0) + (hi - lo)
        if overlap:
            t.speaker = max(overlap, key=overlap.get)
        else:  # věta mimo jakýkoli diar segment → nejbližší mluvčí
            mid = (t.start + t.end) / 2
            t.speaker = min(diar, key=lambda d: min(abs(d.start - mid),
                                                    abs(d.end - mid))).speaker
    return transcript


# --------------------------------------------------------------------------- #
# 5) Výstupy
# --------------------------------------------------------------------------- #
def _ts_srt(sec: float) -> str:
    ms = int(round(sec * 1000))
    h, ms = divmod(ms, 3_600_000)
    m, ms = divmod(ms, 60_000)
    s, ms = divmod(ms, 1000)
    return f"{h:02d}:{m:02d}:{s:02d},{ms:03d}"


def _spk_label(i: int, word: str = "Mluvčí") -> str:
    return f"{word} {i + 1}" if i >= 0 else f"{word} ?"


def write_txt(segs: list[Seg], path: str | Path, with_speakers: bool,
              spk_word: str = "Mluvčí") -> None:
    lines, last = [], None
    for s in segs:
        if with_speakers and s.speaker != last:
            lines.append(f"\n[{_spk_label(s.speaker, spk_word)}]")
            last = s.speaker
        lines.append(s.text)
    Path(path).write_text("\n".join(lines).strip() + "\n", encoding="utf-8")


def write_srt(segs: list[Seg], path: str | Path, with_speakers: bool,
              spk_word: str = "Mluvčí") -> None:
    out = []
    for i, s in enumerate(segs, 1):
        prefix = (f"[{_spk_label(s.speaker, spk_word)}] "
                  if with_speakers and s.speaker >= 0 else "")
        out.append(f"{i}\n{_ts_srt(s.start)} --> {_ts_srt(s.end)}\n{prefix}{s.text}\n")
    Path(path).write_text("\n".join(out), encoding="utf-8")


# --------------------------------------------------------------------------- #
# Orchestrace
# --------------------------------------------------------------------------- #
def _check_runtime(do_diarize: bool) -> None:
    """Srozumitelná chyba místo kryptického WinError, když chybí kus distribuce."""
    missing = [p for p in ([WHISPER_EXE, WHISPER_MODEL]
                           + ([SEG_MODEL, EMB_MODEL] if do_diarize else []))
               if not Path(p).exists()]
    if missing:
        names = "\n  ".join(str(m) for m in missing)
        raise RuntimeError(f"Chybí součásti aplikace:\n  {names}\n"
                           "Zkontroluj, že je distribuce kompletně rozbalená.")


def process(src: str | Path, out_dir: str | Path, *, do_diarize: bool = True,
            threshold: float = DEFAULT_THRESHOLD, num_speakers: Optional[int] = None,
            lang: str = "cs", speaker_word: str = "Mluvčí",
            out_stem: Optional[str] = None,
            threads: Optional[int] = None, cb: ProgressCb = None,
            cancel: Optional[threading.Event] = None) -> dict:
    src = Path(src)
    out_dir = Path(out_dir)
    _check_runtime(do_diarize)
    out_dir.mkdir(parents=True, exist_ok=True)
    threads = threads or _default_threads()
    t0 = time.perf_counter()

    with tempfile.TemporaryDirectory() as td:
        wav = Path(td) / "audio16k.wav"
        _log(cb, "Připravuji zvuk…")
        to_wav16k(src, wav)
        duration = wav_duration(wav)

        # Progress bar monotónně (přepis 0–85 %, diarizace 85–100 %) + self-
        # kalibrující odhad zbývajícího času: z reálného poměru uplynulý_čas /
        # zpracovaná_část počítáme RTF tohoto stroje a promítneme zbytek.
        # DIAR_RTF = hrubý odhad času diarizace (zlomek délky), než ji změříme.
        DIAR_RTF = 0.13
        w_span = 0.85 if do_diarize else 1.0
        w_t0 = [0.0]
        d_t0 = [0.0]  # definováno před closures — d_cb na něj sahá

        def t_cb(m, f):
            if not cb:
                return
            eta = None
            if f > 0.02 and duration > 0:
                el = time.perf_counter() - w_t0[0]
                rtf = el / (f * duration)
                eta = rtf * duration * (1 - f) + (DIAR_RTF * duration if do_diarize else 0)
            cb(m, w_span * f if f >= 0 else f, eta)

        def d_cb(m, f):
            if not cb:
                return
            eta = None
            if f > 0.02 and duration > 0:
                el = time.perf_counter() - d_t0[0]
                eta = max(0.0, el / f - el)  # zbytek diarizace
            cb(m, 0.85 + 0.15 * f if f >= 0 else f, eta)

        w_t0[0] = time.perf_counter()
        transcript = transcribe(wav, lang=lang, threads=threads,
                                cb=(t_cb if cb else None), cancel=cancel)

        stem = out_stem or src.stem
        txt_path = out_dir / f"{stem}.txt"
        srt_path = out_dir / f"{stem}.srt"
        # Pojistka: přepis uložit hned (zatím bez mluvčích). Kdyby cokoli dalšího
        # selhalo, uživatel o hodiny práce whisperu nepřijde.
        write_txt(transcript, txt_path, with_speakers=False)
        write_srt(transcript, srt_path, with_speakers=False)

        diar_error: Optional[str] = None
        if do_diarize:
            d_t0[0] = time.perf_counter()
            try:
                diar = diarize_isolated(wav, threshold=threshold,
                                        num_speakers=num_speakers, threads=threads,
                                        cb=(d_cb if cb else None), cancel=cancel)
                transcript = merge(transcript, diar)
            except Cancelled:
                raise
            except Exception as e:  # noqa: BLE001 — přepis bez mluvčích je lepší než nic
                diar_error = str(e).strip()
                _log(cb, "Rozpoznání mluvčích selhalo — přepis uložen bez mluvčích.")

    with_spk = do_diarize and diar_error is None
    write_txt(transcript, txt_path, with_speakers=with_spk, spk_word=speaker_word)
    write_srt(transcript, srt_path, with_speakers=with_spk, spk_word=speaker_word)

    n_spk = len({s.speaker for s in transcript if s.speaker >= 0}) if with_spk else 0
    elapsed = time.perf_counter() - t0
    _log(cb, f"Hotovo za {elapsed:.0f}s. Mluvčích: {n_spk}. → {txt_path.name}, {srt_path.name}")
    return {"txt": str(txt_path), "srt": str(srt_path),
            "segments": len(transcript), "speakers": n_spk, "elapsed": elapsed,
            "duration": duration, "diar_error": diar_error}


def main() -> None:
    if len(sys.argv) > 1 and sys.argv[1] == "--diar-worker":
        sys.exit(_diar_worker(sys.argv[2:]))
    ap = argparse.ArgumentParser(description="Přepisovátko — přepis + diarizace (CLI)")
    ap.add_argument("audio")
    ap.add_argument("--out-dir", default=".")
    ap.add_argument("--no-diarize", action="store_true")
    ap.add_argument("--threshold", type=float, default=DEFAULT_THRESHOLD)
    ap.add_argument("--speakers", type=int, default=None,
                    help="pevný počet mluvčích (jinak automat)")
    ap.add_argument("--lang", default="cs")
    ap.add_argument("--threads", type=int, default=None)
    args = ap.parse_args()
    process(args.audio, args.out_dir, do_diarize=not args.no_diarize,
            threshold=args.threshold, num_speakers=args.speakers,
            lang=args.lang, threads=args.threads)


if __name__ == "__main__":
    main()
