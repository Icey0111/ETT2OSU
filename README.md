# ETT2OSU

**Batch convert Etterna chart packs (.zip) into osu!mania chart packs (.osz)**

Converts StepMania `.sm` files to osu!mania `.osu` files following [Arrow Vortex](https://arrowvortex.ddrnl.com/)'s conversion approach. No external dependencies — pure Python 3.7+ standard library.

---

## Quick Start

```bash
# 1. Clone
git clone https://github.com/YOUR_USERNAME/ETT2OSU.git
cd ETT2OSU

# 2. Drop your Etterna .zip packs into the input/ folder

# 3. Run
python ett2osu.py

# 4. Pick up .osz files from the output/ folder
```

## How It Works

```
input/                          output/
├── MyPack.zip        ──→       ├── MyPack.osz
├── AnotherPack.zip   ──→       ├── AnotherPack.osz
└── ...                         └── ...
```

1. Extracts each `.zip` pack
2. Finds all `.sm` files inside
3. Parses metadata, timing (BPM changes + stops), and note data
4. Converts `dance-single` charts to osu!mania 4K format
5. Packages only the `.osu`, audio, and background files into a `.osz`

## Interactive Config

On launch, you can edit these values before conversion begins:

```
+============================================================+
|   ETT2OSU -- Etterna -> osu!mania Batch Converter           |
+------------------------------------------------------------+
|   Configure the values below.  Press Enter to keep the     |
|   current value, or type a new one.                        |
+============================================================+

  HP Drain Rate  [8.0]:
  Overall Difficulty  [8.0]:
  Creator Name  [ETT2OSU]:
  Tags  [etterna stepmania converted]:
  Source  [Etterna]:

  Proceed with these settings? (Y/n):
```

Default values can also be changed permanently at the top of `ett2osu.py`.

## Conversion Details

| Feature | Details |
|---|---|
| **Step types** | `dance-single` (4K) only — other modes are skipped |
| **Note types** | Tap (1), Hold (2→3), Roll (4→3) are converted; Mines/Lifts/Fakes are skipped |
| **Timing** | BPM changes and stops/freezes are fully supported |
| **Encoding** | Auto-detects UTF-8, Latin-1, Shift-JIS, GBK, Big5, EUC-KR, etc. |
| **Output naming** | `.osz` filename = original `.zip` filename |
| **Difficulty names** | `{Song Title} [{Difficulty} MSD.{Meter}]` |
| **Metadata** | Title = pack name, ensuring osu! displays the pack name correctly |
| **File cleanup** | Only audio + background + `.osu` files are included — all Etterna-specific files are excluded |

## Requirements

- **Python 3.7+**
- No external packages needed (uses only the standard library)

## License

MIT
