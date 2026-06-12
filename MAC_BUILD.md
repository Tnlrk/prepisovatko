# Přepisovátko — sestavení macOS verze (podklad pro Claude Code)

> **Pro Claude:** Tento dokument je kompletní zadání. Postupuj podle něj krok za
> krokem, po každém kroku ověř výsledek. Píšeš pro vývojáře, který projekt nezná —
> průběžně stručně reportuj. Cíl: funkční `Přepisovátko.app` pro **Apple Silicon**.

## Co je Přepisovátko (kontext)

Offline aplikace pro **přepis nahrávek z porad s detekcí mluvčích**. Vše běží
lokálně (žádný internet, žádné API). Stack:

- **whisper.cpp** (`whisper-cli` jako subprocess) — přepis řeči, model
  `ggml-large-v3-turbo-q5_0.bin`
- **sherpa-onnx** (Python, ONNX runtime) — diarizace (kdo kdy mluví)
- **ffmpeg** (subprocess) — převod libovolného audia/videa na 16 kHz mono WAV
- **CustomTkinter** GUI + tkinterdnd2 (drag&drop)

Windows verze je hotová, otestovaná a nasazená (v1.1.2). Kód je již
**multiplatformní** (v1.2.0): názvy binárek bez `.exe`, otevírání složky přes
`open`, cesty fungují v PyInstaller bundlu (`sys._MEIPASS`). Na Macu se nic
z logiky měnit nemusí.

## Struktura zdrojů

```
prepisovatko/
├── core.py            # pipeline: ffmpeg → whisper → diarizace → merge → TXT/SRT
├── gui.py             # GUI; importuje core; nic víc
├── assets/icon.png    # 1024px — z něj se dělá .icns
├── Prepisovatko.spec  # připravený PyInstaller spec pro .app
└── (bin/, models/ — NEJSOU ve zdrojích, doplníš je níže)
```

Klíčové: `core.py` hledá binárky v `<ROOT>/bin/` a modely v `<ROOT>/models/`;
když `bin/ffmpeg` neexistuje, vezme `ffmpeg` z PATH (jen pro vývoj).

## Postup

### 0) Prostředí

Mac s Apple Silicon, Python 3.12+ (ideálně z python.org nebo brew — **ne systémový**,
kvůli tkinteru), Xcode Command Line Tools (`xcode-select --install`), cmake
(`brew install cmake`).

```bash
cd prepisovatko
python3 -m venv .venv && source .venv/bin/activate
pip install sherpa-onnx numpy customtkinter tkinterdnd2 psutil pillow pyinstaller
python -c "import tkinter; tkinter.Tk().destroy()"   # ověř, že tkinter funguje
```

### 1) whisper.cpp (arm64 + Metal)

```bash
git clone https://github.com/ggml-org/whisper.cpp /tmp/whisper.cpp
cd /tmp/whisper.cpp && cmake -B build -DCMAKE_BUILD_TYPE=Release && cmake --build build -j
mkdir -p <prepisovatko>/bin
cp build/bin/whisper-cli <prepisovatko>/bin/
# zkopíruj i dylib knihovny, které whisper-cli potřebuje (libwhisper, libggml*):
cp build/src/libwhisper*.dylib build/ggml/src/libggml*.dylib <prepisovatko>/bin/ 2>/dev/null || true
# ověř: <prepisovatko>/bin/whisper-cli --help  (kdyby nenašel dylib, oprav
# install_name_tool -add_rpath @executable_path bin/whisper-cli, nebo slinkuj staticky:
# cmake -B build -DBUILD_SHARED_LIBS=OFF a zkopíruj jen whisper-cli)
```

Doporučení: **statický build (`-DBUILD_SHARED_LIBS=OFF`)** = jediný soubor, žádné dylib tahanice.

### 2) ffmpeg (arm64)

Nejjednodušší bez ručního stahování:
```bash
pip download imageio-ffmpeg --no-deps -d /tmp/iff && cd /tmp/iff && unzip -o *.whl
cp imageio_ffmpeg/binaries/ffmpeg-macos-arm64-* <prepisovatko>/bin/ffmpeg
chmod +x <prepisovatko>/bin/ffmpeg && <prepisovatko>/bin/ffmpeg -version
```
(Alternativa: statický build z https://www.osxexperts.net. Přilož licenci ffmpeg.)

### 3) Modely (~600 MB, platformně nezávislé)

```bash
mkdir -p models/diarize/sherpa-onnx-pyannote-segmentation-3-0
curl -L -o models/ggml-large-v3-turbo-q5_0.bin \
  "https://huggingface.co/ggerganov/whisper.cpp/resolve/main/ggml-large-v3-turbo-q5_0.bin?download=true"
curl -L -o models/diarize/nemo_en_titanet_small.onnx \
  "https://github.com/k2-fsa/sherpa-onnx/releases/download/speaker-recongition-models/nemo_en_titanet_small.onnx"
curl -L -o /tmp/seg.tar.bz2 \
  "https://github.com/k2-fsa/sherpa-onnx/releases/download/speaker-segmentation-models/sherpa-onnx-pyannote-segmentation-3-0.tar.bz2"
tar -xjf /tmp/seg.tar.bz2 -C /tmp
cp /tmp/sherpa-onnx-pyannote-segmentation-3-0/model.onnx \
   /tmp/sherpa-onnx-pyannote-segmentation-3-0/LICENSE \
   models/diarize/sherpa-onnx-pyannote-segmentation-3-0/
```

### 4) Test ze zdrojů (PŘED balením!)

```bash
# CLI test na libovolné nahrávce (m4a/mp4/wav…):
python core.py /cesta/k/nahravce.m4a --out-dir /tmp/test_out
# → musí vzniknout .txt a .srt, počet mluvčích dává smysl
python gui.py   # GUI: přidej soubor, spusť přepis, ověř progress i výstup
```

### 5) Ikona .icns

```bash
mkdir /tmp/icon.iconset
for s in 16 32 64 128 256 512; do
  sips -z $s $s assets/icon.png --out /tmp/icon.iconset/icon_${s}x${s}.png
  sips -z $((s*2)) $((s*2)) assets/icon.png --out /tmp/icon.iconset/icon_${s}x${s}@2x.png
done
iconutil -c icns /tmp/icon.iconset -o assets/icon.icns
```

### 6) Sestavení .app

```bash
pyinstaller Prepisovatko.spec
# → dist/Přepisovátko.app
```

### 7) Testovací checklist .app

1. Otevři `dist/Přepisovátko.app` — okno se otevře uprostřed, s ikonou.
2. Přetáhni do okna m4a/mp4 → spusť přepis → progress běží, ETA odpočítává.
3. Výstup .txt má rozumně rozdělené mluvčí; .srt validní časy.
4. Tlačítko Zastavit přeruší běh okamžitě.
5. „Otevřít výstup" otevře Finder.
6. Patička ukazuje CPU/RAM.

### 8) Distribuce

```bash
cd dist && zip -r -y Prepisovatko-mac-arm64-v1.2.0.zip "Přepisovátko.app"
```
App **není podepsaná** — příjemkyně ji poprvé otevře přes **pravý klik → Otevřít**
(Gatekeeper). Pokud macOS hlásí „poškozeno", spustit:
`xattr -dr com.apple.quarantine "/cesta/k/Přepisovátko.app"`.

## Známé zrady / poznámky pro Claude

- **Neměň logiku v core.py/gui.py** — je odladěná proti Windows verzi (threshold
  diarizace 0.7 + post-filtr šumových mluvčích, vlákna whisper `cpu-2` /
  diarizace max 8). Opravuj jen věci nutné pro macOS build.
- `gui.py` má nahoře Windows-specifické berličky (sys.stderr=None guard, přepnutí
  draw metody) — na Macu jsou neškodné, nech je být.
- sherpa-onnx API drobnosti: výsledek diarizace je iterovatelný až po
  `.sort_by_start_time()`; vstup musí být 16 kHz mono float32 — to vše core řeší.
- Pokud PyInstaller nepřibalí dylib sherpa-onnx, je ve specu
  `collect_dynamic_libs("sherpa_onnx")` — zkontroluj, případně přidej cestu ručně.
- Výkon na M-čipech: whisper s Metal poběží výrazně rychleji než RTF 0,5
  z Windows referencí; diarizace je ONNX na CPU, RTF ~0,1.
- Kontakt na autora zadání: Antonín Lerek, tnlrk@tnlrk.cz.
