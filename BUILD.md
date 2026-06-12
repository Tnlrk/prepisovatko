# Sestavení portable balíčku

Repozitář obsahuje jen zdrojový kód. Hotový balíček (~850 MB) se skládá z níže
uvedených součástí. Cílová platforma: **Windows 10/11, 64-bit, CPU-only**.

## Výsledná struktura

```
Prepisovatko/
├── Přepisovátko.bat                 # spouštěč (start pythonw gui.py)
├── Přepisovátko - diagnostika.bat   # spouštěč s konzolí (řešení potíží)
├── ČTI-MĚ.txt                       # návod pro uživatele
├── core.py, gui.py
├── assets/icon.ico
├── python/                          # embeddable Python + balíčky + tkinter
├── bin/                             # whisper-cli.exe + DLL + ffmpeg.exe
└── models/                          # whisper + diarizační modely
```

## 1) Embeddable Python

Stáhnout **python-3.14.x-embed-amd64.zip** z python.org a rozbalit do `python/`.
V `python/python314._pth` musí být tyto řádky (jinak `import core` selže — embeddable
je plně izolovaný a sám nepřidává kořen aplikace na `sys.path`):

```
python314.zip
.
Lib\site-packages
..
```

## 2) Balíčky

```bash
python/python.exe -m pip install --target python/Lib/site-packages -r requirements.txt
```

(embeddable nemá pip — použijte pip ze stejné verze plné instalace Pythonu, nebo
`get-pip.py`. Balíčky: viz requirements.txt.)

## 3) tkinter (embeddable ho neobsahuje)

Z plné instalace stejné verze Pythonu zkopírovat:

- `DLLs/_tkinter.pyd`, `tcl86t.dll`, `tk86t.dll`, `zlib1.dll` → do `python/`
- `Lib/tkinter` → do `python/Lib/site-packages/tkinter`
- složku `tcl` → do `python/tcl`

## 4) whisper.cpp binárky → `bin/`

Z release [ggml-org/whisper.cpp](https://github.com/ggml-org/whisper.cpp/releases)
(Windows x64 CPU build) zkopírovat: `whisper-cli.exe`, `whisper.dll`, `ggml.dll`,
`ggml-base.dll`, `ggml-cpu.dll`.

## 5) ffmpeg → `bin/`

Libovolný Windows build `ffmpeg.exe` (a přiložte jeho licenci). Aplikace ho hledá
v `bin/ffmpeg.exe`; ve vývoji použije ffmpeg ze systémové PATH.

## 6) Modely → `models/`

- `models/ggml-large-v3-turbo-q5_0.bin` — [HuggingFace ggerganov/whisper.cpp](https://huggingface.co/ggerganov/whisper.cpp)
- `models/diarize/nemo_en_titanet_small.onnx` — [sherpa-onnx speaker models](https://github.com/k2-fsa/sherpa-onnx/releases/tag/speaker-recongition-models)
- `models/diarize/sherpa-onnx-pyannote-segmentation-3-0/model.onnx` — [sherpa-onnx segmentation models](https://github.com/k2-fsa/sherpa-onnx/releases/tag/speaker-segmentation-models)

## Spuštění

Poklepat na `Přepisovátko.bat`. Hotovo — bez instalace, bez admin práv, offline.

## Výkonové poznámky

- Vlákna whisperu = `logické_procesory − 2`; diarizace `min(8, …)` (výš neškáluje).
- Přepis ~RTF 0,5–0,7, diarizace ~0,1 (na slabším CPU víc).
