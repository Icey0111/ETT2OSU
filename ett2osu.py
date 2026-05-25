#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
+============================================================+
|   ETT2OSU - Etterna (.sm) -> osu!mania (.osz) Converter    |
|                                                            |
|   Batch-converts Etterna chart packs (.zip with .sm files) |
|   into osu!mania chart packs (.osz with .osu files).       |
|   Conversion logic follows Arrow Vortex's approach.        |
+============================================================+

Usage:
    1. Place Etterna chart pack .zip files into the  input/  folder.
    2. Run:  python ett2osu.py
    3. Converted .osz files appear in the  output/  folder.

Requirements:  Python 3.7+  (no external libraries needed)
"""

import os
import re
import sys
import math
import shutil
import zipfile
import tempfile
from pathlib import Path

# +============================================================+
# |   USER CONFIG -- Edit these values before running          |
# +------------------------------------------------------------+
# |   These values will appear in every generated .osu file.   |
# |   Open this script in any text editor to change them.      |
# +============================================================+

HP_DRAIN_RATE      = 8.0                            # HP drain severity  (0-10)
OVERALL_DIFFICULTY  = 8.0                            # Timing strictness  (0-10)
CREATOR_NAME       = "ETT2OSU"                       # Creator / mapper name
TAGS               = "etterna stepmania converted"   # Space-separated search tags
SOURCE             = "Etterna"                       # Source field in metadata

# =============================================================


# ---------------------------------------------------------------------------
#  Constants
# ---------------------------------------------------------------------------

# 4K column centre X-positions (dance-single: Left / Down / Up / Right)
# Formula: column_x = floor(512 / key_count * (col + 0.5))
COLUMN_X_4K = [64, 192, 320, 448]

# Valid note characters in SM format
VALID_NOTE_CHARS = set("01234MLFKmlf k")

# Ordered list of encodings to try when reading .sm files
ENCODINGS_TO_TRY = [
    "utf-8-sig",   # UTF-8 with BOM  (common in Asian packs)
    "utf-8",       # plain UTF-8
    "latin-1",     # Western European  (never fails, 1-byte)
    "cp1252",      # Windows Western
    "shift_jis",   # Japanese
    "euc-kr",      # Korean
    "gb2312",      # Simplified Chinese
    "big5",        # Traditional Chinese
]


# ---------------------------------------------------------------------------
#  Encoding helpers
# ---------------------------------------------------------------------------

def read_file_with_fallback(filepath: str) -> str:
    """Read a text file, cascading through encodings until one works."""
    for enc in ENCODINGS_TO_TRY:
        try:
            with open(filepath, "r", encoding=enc, errors="strict") as fh:
                return fh.read()
        except (UnicodeDecodeError, UnicodeError):
            continue
    # Absolute fallback — latin-1 accepts every byte
    with open(filepath, "r", encoding="latin-1") as fh:
        return fh.read()


def sanitize_filename(name: str) -> str:
    """Replace characters that are illegal in Windows / osu! filenames."""
    name = re.sub(r'[<>:"/\\|?*]', "_", name)
    name = re.sub(r"[\x00-\x1f]", "", name)
    name = name.strip(" .")
    return name if name else "untitled"


# ---------------------------------------------------------------------------
#  SM parser
# ---------------------------------------------------------------------------

def parse_sm_tags(content: str) -> dict:
    """
    Extract every  #TAG:VALUE;  pair from the raw SM file text.

    Multi-line values (especially #NOTES: blocks) are handled correctly.
    Returns a dict; the 'NOTES' key maps to a *list* of raw note-block strings.
    """
    tags: dict = {}

    # The regex matches  #TAG: ... ;  across multiple lines.
    pattern = re.compile(r"#([A-Za-z]+)\s*:(.*?)\s*;", re.DOTALL)
    for m in pattern.finditer(content):
        tag = m.group(1).upper()
        value = m.group(2).strip()
        if tag == "NOTES":
            tags.setdefault("NOTES", []).append(value)
        else:
            tags[tag] = value
    return tags


def parse_bpms(raw: str) -> list:
    """Parse  '#BPMS:beat=bpm,beat=bpm,...;'  into [(beat, bpm), ...]."""
    if not raw or not raw.strip():
        return [(0.0, 120.0)]

    result = []
    for pair in raw.split(","):
        pair = pair.strip()
        if "=" not in pair:
            continue
        try:
            b, v = pair.split("=", 1)
            beat = float(b.strip())
            bpm = float(v.strip())
            if bpm > 0:
                result.append((beat, bpm))
        except ValueError:
            continue

    result.sort(key=lambda x: x[0])
    return result if result else [(0.0, 120.0)]


def parse_stops(raw: str) -> list:
    """Parse  '#STOPS:beat=seconds,...;'  into [(beat, seconds), ...]."""
    if not raw or not raw.strip():
        return []

    result = []
    for pair in raw.split(","):
        pair = pair.strip()
        if "=" not in pair:
            continue
        try:
            b, v = pair.split("=", 1)
            beat = float(b.strip())
            dur = float(v.strip())
            if dur > 0:
                result.append((beat, dur))
        except ValueError:
            continue

    result.sort(key=lambda x: x[0])
    return result


def parse_notes_block(raw_block: str) -> dict | None:
    """
    Parse one  #NOTES:  block into structured data.

    Format inside the block (after the colon following #NOTES):
        stepstype : description : difficulty : meter : radarvalues : notedata
    """
    # Strip comment lines  (// ...)
    lines = [ln for ln in raw_block.split("\n") if not ln.strip().startswith("//")]
    joined = "\n".join(lines)

    # Split on the first five colons to get the six fields
    parts = joined.split(":", 5)
    if len(parts) < 6:
        return None

    steps_type  = parts[0].strip()
    description = parts[1].strip()
    difficulty  = parts[2].strip()
    try:
        meter = int(parts[3].strip())
    except ValueError:
        meter = 1
    radar_values = parts[4].strip()
    note_data    = parts[5].strip()

    # ---- parse note data into  measures -> rows ----
    measures = []
    for measure_str in note_data.split(","):
        rows = []
        for line in measure_str.strip().split("\n"):
            row = line.strip()
            # A valid note row for dance-single has >=4 chars from the note charset
            if len(row) >= 4 and all(c in "01234MLFKmlfk" for c in row[:4]):
                rows.append(row[:4].upper())
        if rows:
            measures.append(rows)

    return {
        "steps_type":   steps_type,
        "description":  description,
        "difficulty":   difficulty,
        "meter":        meter,
        "radar_values": radar_values,
        "measures":     measures,
    }


# ---------------------------------------------------------------------------
#  Timing engine
# ---------------------------------------------------------------------------

def beat_to_ms(target_beat: float, bpms: list, stops: list, offset: float) -> float:
    """
    Convert a beat position -> milliseconds.

    Accounts for:
      - SM offset (negated, converted s -> ms)
      - BPM changes  (variable tempo segments)
      - Stops / freezes  (additional dead time)

    The SM OFFSET convention:  negative offset = music starts before beat 0.
    osu! convention:  first timing point time = -offset * 1000.
    """
    # osu! timing starts at  -offset (seconds) * 1000 -> ms
    current_ms   = -offset * 1000.0
    current_beat = 0.0
    current_bpm  = bpms[0][1]

    # Merge BPM-change and stop events into a single sorted timeline
    events = []
    for beat, bpm in bpms:
        events.append(("bpm", beat, bpm))
    for beat, dur in stops:
        events.append(("stop", beat, dur))
    # Sort by beat, with BPM changes processed *before* stops at the same beat
    events.sort(key=lambda e: (e[1], 0 if e[0] == "bpm" else 1))

    for event in events:
        ev_beat = event[1]

        # If this event is at or beyond the target, stop advancing
        if ev_beat >= target_beat:
            break

        # Advance time from current_beat → ev_beat at current BPM
        if ev_beat > current_beat:
            delta = ev_beat - current_beat
            current_ms += delta * (60000.0 / current_bpm)
            current_beat = ev_beat

        if event[0] == "bpm":
            current_bpm = event[2]
        elif event[0] == "stop":
            current_ms += event[2] * 1000.0   # freeze duration

    # Advance the remaining distance to target_beat
    if target_beat > current_beat:
        delta = target_beat - current_beat
        current_ms += delta * (60000.0 / current_bpm)

    return current_ms


# ---------------------------------------------------------------------------
#  Timing-point generator
# ---------------------------------------------------------------------------

def generate_timing_points(bpms: list, stops: list, offset: float) -> list:
    """
    Build osu! [TimingPoints] lines.

    - One uninherited (red) timing point per BPM change.
    - One resync timing point after each stop (to keep the metronome aligned).
    """
    tp_list: list = []     # (time_ms, line_string)

    # --- BPM changes -> red timing points ---
    for beat, bpm in bpms:
        time_ms    = beat_to_ms(beat, bpms, stops, offset)
        beat_len   = 60000.0 / bpm
        tp_list.append((
            time_ms,
            f"{round(time_ms)},{beat_len:.12g},4,1,0,100,1,0",
        ))

    # --- Stops -> resync timing points after each stop ---
    for stop_beat, stop_dur in stops:
        stop_ms   = beat_to_ms(stop_beat, bpms, stops, offset)
        resume_ms = stop_ms + stop_dur * 1000.0

        # Find the BPM that is active at the stop beat
        active_bpm = bpms[0][1]
        for b, bp in bpms:
            if b <= stop_beat:
                active_bpm = bp
            else:
                break

        beat_len = 60000.0 / active_bpm
        tp_list.append((
            resume_ms,
            f"{round(resume_ms)},{beat_len:.12g},4,1,0,100,1,0",
        ))

    # Sort chronologically and de-duplicate by rounded ms
    tp_list.sort(key=lambda x: x[0])
    seen   = set()
    result = []
    for t, line in tp_list:
        key = round(t)
        if key not in seen:
            seen.add(key)
            result.append(line)
    return result


# ---------------------------------------------------------------------------
#  Note converter
# ---------------------------------------------------------------------------

def convert_notes(measures: list, bpms: list, stops: list, offset: float) -> list:
    """
    Convert SM note measures -> osu! [HitObjects] lines.

    Handles taps (1), hold heads (2), roll heads (4), tails (3).
    Mines (M), lifts (L), fakes (F) are skipped (no mania equivalent).
    """
    hit_objects = []          # (time_ms, line_string)
    active_holds: dict = {}   # column → (start_ms_rounded, x)

    for m_idx, measure in enumerate(measures):
        n_rows = len(measure)
        if n_rows == 0:
            continue

        for r_idx, row in enumerate(measure):
            beat     = m_idx * 4.0 + r_idx * 4.0 / n_rows
            time_ms  = beat_to_ms(beat, bpms, stops, offset)
            time_r   = round(time_ms)

            for col in range(min(len(row), 4)):
                ch = row[col]
                x  = COLUMN_X_4K[col]

                if ch == "1":
                    # --- Tap note ---
                    hit_objects.append((
                        time_ms,
                        f"{x},192,{time_r},1,0,0:0:0:0:",
                    ))

                elif ch in ("2", "4"):
                    # --- Hold / Roll head ---
                    active_holds[col] = (time_r, x)

                elif ch == "3":
                    # --- Hold / Roll tail ---
                    if col in active_holds:
                        start_r, start_x = active_holds.pop(col)
                        end_r = time_r
                        if end_r <= start_r:
                            end_r = start_r + 1
                        hit_objects.append((
                            float(start_r),
                            f"{start_x},192,{start_r},128,0,{end_r}:0:0:0:0:",
                        ))
                # M / L / F / K  → ignored

    # Orphaned hold heads (no matching tail) -> convert to taps
    for col, (start_r, x) in active_holds.items():
        hit_objects.append((
            float(start_r),
            f"{x},192,{start_r},1,0,0:0:0:0:",
        ))

    hit_objects.sort(key=lambda h: h[0])
    return [line for _, line in hit_objects]


# ---------------------------------------------------------------------------
#  .osu file builder
# ---------------------------------------------------------------------------

def build_osu_content(
    metadata:       dict,
    diff_info:      dict,
    audio_filename: str,
    bg_filename:    str,
    timing_points:  list,
    hit_objects:    list,
    pack_name:      str = "",
) -> str:
    """Assemble a complete  osu file format v14  string."""

    title    = metadata.get("TITLE", "Unknown Title")
    subtitle = metadata.get("SUBTITLE", "")
    artist   = metadata.get("ARTIST", "Unknown Artist")

    # Full display title of the *song*  (with subtitle if present)
    if subtitle:
        song_display = f"{title} {subtitle}"
    else:
        song_display = title

    # -------------------------------------------------------------------
    # osu! uses Title/Artist from .osu metadata to name the beatmap set.
    # Title = pack name (matching the original .zip).
    # Artist = actual music artist from the SM file.
    # -------------------------------------------------------------------
    set_title  = pack_name if pack_name else song_display
    set_artist = artist

    # Chart author fallback chain:
    #   1) per-difficulty description from NOTES block
    #   2) #CREDIT tag from SM header
    #   3) author extracted from folder name, e.g. "SongName(Author)"
    chart_author = diff_info.get("description", "").strip()
    if not chart_author:
        chart_author = metadata.get("CREDIT", "").strip()
    if not chart_author:
        chart_author = metadata.get("FOLDER_AUTHOR", "").strip()

    # Difficulty / Version string:
    #   with chart author:  "Song [Author's Difficulty MSD.xx]"
    #   without author:     "Song [Difficulty MSD.xx]"
    diff_name = diff_info["difficulty"]
    meter     = diff_info["meter"]

    if chart_author:
        version = f"{song_display} [{chart_author}'s {diff_name} MSD.{meter}]"
    else:
        version = f"{song_display} [{diff_name} MSD.{meter}]"

    # Preview time
    try:
        preview_ms = int(float(metadata.get("SAMPLESTART", "0")) * 1000)
    except ValueError:
        preview_ms = -1
    if preview_ms <= 0:
        preview_ms = -1

    # Escape BG filename for the Events line
    bg_escaped = bg_filename.replace("\\", "/").replace('"', '\\"') if bg_filename else ""

    tp_block = "\n".join(timing_points)
    ho_block = "\n".join(hit_objects)

    return (
        f"osu file format v14\n"
        f"\n"
        f"[General]\n"
        f"AudioFilename: {audio_filename}\n"
        f"AudioLeadIn: 0\n"
        f"PreviewTime: {preview_ms}\n"
        f"Countdown: 0\n"
        f"SampleSet: Soft\n"
        f"StackLeniency: 0.7\n"
        f"Mode: 3\n"
        f"LetterboxInBreaks: 0\n"
        f"SpecialStyle: 0\n"
        f"WidescreenStoryboard: 0\n"
        f"\n"
        f"[Editor]\n"
        f"DistanceSpacing: 1\n"
        f"BeatDivisor: 4\n"
        f"GridSize: 4\n"
        f"TimelineZoom: 1\n"
        f"\n"
        f"[Metadata]\n"
        f"Title:{set_title}\n"
        f"TitleUnicode:{set_title}\n"
        f"Artist:{set_artist}\n"
        f"ArtistUnicode:{set_artist}\n"
        f"Creator:{chart_author if chart_author else CREATOR_NAME}\n"
        f"Version:{version}\n"
        f"Source:{SOURCE}\n"
        f"Tags:{TAGS} {chart_author}\n"
        f"BeatmapID:0\n"
        f"BeatmapSetID:-1\n"
        f"\n"
        f"[Difficulty]\n"
        f"HPDrainRate:{HP_DRAIN_RATE}\n"
        f"CircleSize:4\n"
        f"OverallDifficulty:{OVERALL_DIFFICULTY}\n"
        f"ApproachRate:5\n"
        f"SliderMultiplier:1.4\n"
        f"SliderTickRate:1\n"
        f"\n"
        f"[Events]\n"
        f"//Background and Video events\n"
        f'0,0,"{bg_escaped}",0,0\n'
        f"//Break Periods\n"
        f"\n"
        f"[TimingPoints]\n"
        f"{tp_block}\n"
        f"\n"
        f"[HitObjects]\n"
        f"{ho_block}\n"
    )


# ---------------------------------------------------------------------------
#  File-resolution helpers
# ---------------------------------------------------------------------------

def find_file_casefold(directory: str, target_name: str) -> str | None:
    """
    Locate *target_name* inside *directory*, ignoring case.
    Returns the actual path or None.
    """
    if not target_name:
        return None
    target_lower = target_name.lower()
    try:
        for entry in os.listdir(directory):
            if entry.lower() == target_lower:
                full = os.path.join(directory, entry)
                if os.path.isfile(full):
                    return full
    except OSError:
        pass
    return None


def find_audio_file(sm_dir: str, music_ref: str) -> tuple:
    """Return (abs_path, basename) of the audio file, or (None, '')."""
    # Try the referenced file first
    if music_ref:
        found = find_file_casefold(sm_dir, music_ref)
        if found:
            return found, os.path.basename(found)

    # Fallback: first audio file in the directory
    for ext in (".mp3", ".ogg", ".wav"):
        for f in os.listdir(sm_dir):
            if f.lower().endswith(ext):
                return os.path.join(sm_dir, f), f
    return None, ""


def find_bg_file(sm_dir: str, bg_ref: str) -> tuple:
    """Return (abs_path, basename) of the background image, or (None, '')."""
    if bg_ref:
        found = find_file_casefold(sm_dir, bg_ref)
        if found:
            return found, os.path.basename(found)

    # Fallback: first non-banner image
    for f in os.listdir(sm_dir):
        fl = f.lower()
        if fl.endswith((".png", ".jpg", ".jpeg", ".bmp")):
            if "banner" not in fl and "bn" not in fl and "cdtitle" not in fl:
                return os.path.join(sm_dir, f), f
    return None, ""


# ---------------------------------------------------------------------------
#  Per-song processor
# ---------------------------------------------------------------------------

def process_sm_file(
    sm_path:    str,
    build_dir:  str,
    song_index: int,
    pack_name:  str = "",
) -> int:
    """
    Process one .sm file -> one or more .osu files written into *build_dir*.

    *song_index* is used to prefix audio/bg filenames and avoid collisions
    when multiple songs share the same pack (and therefore the same .osz).

    Returns the number of difficulties successfully converted.
    """
    sm_dir = os.path.dirname(sm_path)

    # ---- read & parse ----
    content = read_file_with_fallback(sm_path)
    tags    = parse_sm_tags(content)

    if "NOTES" not in tags or not tags["NOTES"]:
        print(f"    [!] No notes found in {os.path.basename(sm_path)}, skipping")
        return 0

    bpms = parse_bpms(tags.get("BPMS", ""))
    stops = parse_stops(tags.get("STOPS", ""))
    try:
        offset = float(tags.get("OFFSET", "0"))
    except ValueError:
        offset = 0.0

    metadata = {
        "TITLE":           tags.get("TITLE", "Unknown"),
        "SUBTITLE":        tags.get("SUBTITLE", ""),
        "ARTIST":          tags.get("ARTIST", "Unknown"),
        "TITLETRANSLIT":   tags.get("TITLETRANSLIT", ""),
        "ARTISTTRANSLIT":  tags.get("ARTISTTRANSLIT", ""),
        "CREDIT":          tags.get("CREDIT", ""),
        "SAMPLESTART":     tags.get("SAMPLESTART", ""),
        "SAMPLELENGTH":    tags.get("SAMPLELENGTH", ""),
    }

    # Try to extract chart author from folder name, e.g. "Sally's Dance(Lofty)"
    folder_name = os.path.basename(sm_dir)
    folder_author = ""
    if "(" in folder_name and folder_name.rstrip().endswith(")"):
        folder_author = folder_name.rsplit("(", 1)[-1].rstrip(")" ).strip()
    metadata["FOLDER_AUTHOR"] = folder_author

    # ---- resolve audio & background ----
    audio_path, audio_base = find_audio_file(sm_dir, tags.get("MUSIC", ""))
    bg_path,    bg_base    = find_bg_file(sm_dir, tags.get("BACKGROUND", ""))

    # Prefix with song_index to avoid filename collisions across songs
    prefix = f"{song_index:03d}_"
    audio_osu_name = f"{prefix}{audio_base}" if audio_base else ""
    bg_osu_name    = f"{prefix}{bg_base}"    if bg_base    else ""

    # Copy media files into the build directory
    if audio_path and audio_osu_name:
        dst = os.path.join(build_dir, audio_osu_name)
        if not os.path.exists(dst):
            shutil.copy2(audio_path, dst)
    if bg_path and bg_osu_name:
        dst = os.path.join(build_dir, bg_osu_name)
        if not os.path.exists(dst):
            shutil.copy2(bg_path, dst)

    # ---- shared timing points ----
    timing_points = generate_timing_points(bpms, stops, offset)

    # ---- convert each dance-single difficulty ----
    diff_count = 0
    for notes_str in tags["NOTES"]:
        diff_info = parse_notes_block(notes_str)
        if diff_info is None:
            continue
        if diff_info["steps_type"] != "dance-single":
            continue

        hit_objects = convert_notes(diff_info["measures"], bpms, stops, offset)
        if not hit_objects:
            continue

        osu_content = build_osu_content(
            metadata, diff_info,
            audio_osu_name, bg_osu_name,
            timing_points, hit_objects,
            pack_name=pack_name,
        )

        # Build the .osu filename
        title    = metadata["TITLE"]
        subtitle = metadata["SUBTITLE"]
        full_t   = f"{title} {subtitle}".strip() if subtitle else title
        diff_n   = diff_info["difficulty"]
        meter    = diff_info["meter"]
        set_title = pack_name if pack_name else full_t

        # Chart author fallback: description -> CREDIT -> folder name
        chart_author = diff_info.get("description", "").strip()
        if not chart_author:
            chart_author = metadata.get("CREDIT", "").strip()
        if not chart_author:
            chart_author = metadata.get("FOLDER_AUTHOR", "").strip()

        if chart_author:
            version_part = f"{full_t} [{chart_author}'s {diff_n} MSD.{meter}]"
        else:
            version_part = f"{full_t} [{diff_n} MSD.{meter}]"

        osu_fname = sanitize_filename(
            f"{CREATOR_NAME} - {set_title} [{version_part}].osu"
        )
        osu_path = os.path.join(build_dir, osu_fname)

        # Write with UTF-8 BOM + CRLF (osu! expects this for Unicode)
        with open(osu_path, "w", encoding="utf-8-sig", newline="\r\n") as fh:
            fh.write(osu_content)

        diff_count += 1

    return diff_count


# ---------------------------------------------------------------------------
#  Blank host difficulty generator
# ---------------------------------------------------------------------------

def create_blank_difficulty(build_dir: str, pack_name: str) -> None:
    """
    Create a blank .osu difficulty authored by CREATOR_NAME.

    osu!'s Beatmap Submission System requires that the mapset host
    (the Creator) has at least one difficulty in the set. This generates
    a minimal difficulty with a single note so the set can be uploaded.

    Uses the first audio and background file found in *build_dir*.
    """
    # Find the first audio file in build_dir
    audio_name = ""
    bg_name    = ""
    for f in sorted(os.listdir(build_dir)):
        fl = f.lower()
        if not audio_name and fl.endswith((".mp3", ".ogg", ".wav")):
            audio_name = f
        if not bg_name and fl.endswith((".png", ".jpg", ".jpeg", ".bmp")):
            bg_name = f
        if audio_name and bg_name:
            break

    set_title  = pack_name if pack_name else "Untitled"
    set_artist = CREATOR_NAME
    version    = f"{CREATOR_NAME}'s Blank"

    bg_escaped = bg_name.replace("\\", "/").replace('"', '\\"') if bg_name else ""

    # A single tap note at 1000ms on column 0, so the map is not completely empty
    content = (
        f"osu file format v14\n"
        f"\n"
        f"[General]\n"
        f"AudioFilename: {audio_name}\n"
        f"AudioLeadIn: 0\n"
        f"PreviewTime: -1\n"
        f"Countdown: 0\n"
        f"SampleSet: Soft\n"
        f"StackLeniency: 0.7\n"
        f"Mode: 3\n"
        f"LetterboxInBreaks: 0\n"
        f"SpecialStyle: 0\n"
        f"WidescreenStoryboard: 0\n"
        f"\n"
        f"[Editor]\n"
        f"DistanceSpacing: 1\n"
        f"BeatDivisor: 4\n"
        f"GridSize: 4\n"
        f"TimelineZoom: 1\n"
        f"\n"
        f"[Metadata]\n"
        f"Title:{set_title}\n"
        f"TitleUnicode:{set_title}\n"
        f"Artist:{set_artist}\n"
        f"ArtistUnicode:{set_artist}\n"
        f"Creator:{CREATOR_NAME}\n"
        f"Version:{version}\n"
        f"Source:{SOURCE}\n"
        f"Tags:{TAGS}\n"
        f"BeatmapID:0\n"
        f"BeatmapSetID:-1\n"
        f"\n"
        f"[Difficulty]\n"
        f"HPDrainRate:1\n"
        f"CircleSize:4\n"
        f"OverallDifficulty:1\n"
        f"ApproachRate:5\n"
        f"SliderMultiplier:1.4\n"
        f"SliderTickRate:1\n"
        f"\n"
        f"[Events]\n"
        f"//Background and Video events\n"
        f'0,0,"{bg_escaped}",0,0\n'
        f"//Break Periods\n"
        f"\n"
        f"[TimingPoints]\n"
        f"0,500,4,1,0,100,1,0\n"
        f"\n"
        f"[HitObjects]\n"
        f"64,192,1000,1,0,0:0:0:0:\n"
    )

    osu_fname = sanitize_filename(
        f"{CREATOR_NAME} - {set_title} [{version}].osu"
    )
    osu_path = os.path.join(build_dir, osu_fname)

    with open(osu_path, "w", encoding="utf-8-sig", newline="\r\n") as fh:
        fh.write(content)


# ---------------------------------------------------------------------------
#  Pack-level processor  (.zip -> .osz)
# ---------------------------------------------------------------------------

def process_zip_pack(zip_path: str, output_dir: str) -> tuple:
    """
    Extract one Etterna pack .zip, convert every .sm inside,
    and package the results as a single .osz in *output_dir*.

    Returns (songs_converted, difficulties_converted).
    """
    pack_name = os.path.splitext(os.path.basename(zip_path))[0]
    osz_path  = os.path.join(output_dir, f"{pack_name}.osz")

    print(f"\n{'-'*60}")
    print(f"  [PACK]  {pack_name}")
    print(f"{'-'*60}")

    with tempfile.TemporaryDirectory() as tmp:
        extract_dir = os.path.join(tmp, "extracted")
        build_dir   = os.path.join(tmp, "build")
        os.makedirs(extract_dir)
        os.makedirs(build_dir)

        # ---- extract ----
        try:
            with zipfile.ZipFile(zip_path, "r") as zf:
                zf.extractall(extract_dir)
        except (zipfile.BadZipFile, Exception) as exc:
            print(f"  [FAIL] Failed to extract: {exc}")
            return 0, 0

        # ---- locate .sm files ----
        sm_files = sorted(
            str(p) for p in Path(extract_dir).rglob("*.sm")
        )
        if not sm_files:
            # Also try case-insensitive
            sm_files = sorted(
                str(p) for p in Path(extract_dir).rglob("*")
                if p.suffix.lower() == ".sm"
            )

        if not sm_files:
            print("  [!] No .sm files found -- skipping")
            return 0, 0

        # ---- process each song ----
        total_songs = 0
        total_diffs = 0

        for idx, sm_path in enumerate(sm_files, start=1):
            song_folder = os.path.basename(os.path.dirname(sm_path))
            diffs = process_sm_file(sm_path, build_dir, song_index=idx, pack_name=pack_name)

            if diffs > 0:
                total_songs += 1
                total_diffs += diffs
                suffix = "difficulty" if diffs == 1 else "difficulties"
                print(f"  [OK] {song_folder}  ->  {diffs} {suffix}")
            else:
                print(f"  [!]  {song_folder}  ->  no valid dance-single charts")

        if total_diffs == 0:
            print("  [FAIL] No valid charts produced -- .osz not created")
            return 0, 0

        # ---- create blank host difficulty for osu! upload ----
        create_blank_difficulty(build_dir, pack_name)
        print(f"  [OK] Created blank host difficulty for upload")

        # ---- build .osz (ZIP archive) ----
        with zipfile.ZipFile(osz_path, "w", zipfile.ZIP_DEFLATED) as zf:
            for root, _dirs, files in os.walk(build_dir):
                for fname in files:
                    abs_path = os.path.join(root, fname)
                    arc_name = os.path.relpath(abs_path, build_dir)
                    zf.write(abs_path, arc_name)

        size_mb = os.path.getsize(osz_path) / (1024 * 1024)
        print(f"\n  [DONE] -> {pack_name}.osz  "
              f"({total_songs} songs, {total_diffs} diffs + 1 blank, {size_mb:.1f} MB)")

    return total_songs, total_diffs


# ---------------------------------------------------------------------------
#  Main entry point
# ---------------------------------------------------------------------------

def prompt_config():
    """
    Interactive config editor.  Shows current defaults and lets the user
    press Enter to keep them or type a new value.  Returns the final
    values only after the user confirms.
    """
    global HP_DRAIN_RATE, OVERALL_DIFFICULTY, CREATOR_NAME, TAGS, SOURCE

    while True:
        print()
        print("+============================================================+")
        print("|   ETT2OSU -- Etterna -> osu!mania Batch Converter           |")
        print("+------------------------------------------------------------+")
        print("|   Configure the values below.  Press Enter to keep the     |")
        print("|   current value, or type a new one.                        |")
        print("+============================================================+")
        print()

        # --- HP ---
        raw = input(f"  HP Drain Rate  [{HP_DRAIN_RATE}]: ").strip()
        if raw:
            try:
                val = float(raw)
                if 0.0 <= val <= 10.0:
                    HP_DRAIN_RATE = val
                else:
                    print("    (!) Value out of range 0-10, keeping previous.")
            except ValueError:
                print("    (!) Invalid number, keeping previous.")

        # --- OD ---
        raw = input(f"  Overall Difficulty  [{OVERALL_DIFFICULTY}]: ").strip()
        if raw:
            try:
                val = float(raw)
                if 0.0 <= val <= 10.0:
                    OVERALL_DIFFICULTY = val
                else:
                    print("    (!) Value out of range 0-10, keeping previous.")
            except ValueError:
                print("    (!) Invalid number, keeping previous.")

        # --- Creator ---
        raw = input(f"  Creator Name  [{CREATOR_NAME}]: ").strip()
        if raw:
            CREATOR_NAME = raw

        # --- Tags ---
        raw = input(f"  Tags  [{TAGS}]: ").strip()
        if raw:
            TAGS = raw

        # --- Source ---
        raw = input(f"  Source  [{SOURCE}]: ").strip()
        if raw:
            SOURCE = raw

        # --- Show summary and confirm ---
        print()
        print("  +--------------------------------------------------------+")
        print("  |  Current Settings                                      |")
        print("  +--------------------------------------------------------+")
        print(f"  |  HP: {HP_DRAIN_RATE:<8}  OD: {OVERALL_DIFFICULTY:<8}  Creator: {CREATOR_NAME}")
        print(f"  |  Tags: {TAGS}")
        print(f"  |  Source: {SOURCE}")
        print("  +--------------------------------------------------------+")
        print()

        confirm = input("  Proceed with these settings? (Y/n): ").strip().lower()
        if confirm in ("", "y", "yes"):
            print()
            return
        else:
            print("\n  OK, let's re-enter the values...")


def main():
    # Ensure console output handles non-ASCII gracefully on Windows
    try:
        sys.stdout.reconfigure(errors="replace")
    except Exception:
        pass

    # ---- interactive config ----
    prompt_config()

    # ---- set up directories ----
    script_dir = os.path.dirname(os.path.abspath(__file__))
    input_dir  = os.path.join(script_dir, "input")
    output_dir = os.path.join(script_dir, "output")

    os.makedirs(input_dir,  exist_ok=True)
    os.makedirs(output_dir, exist_ok=True)

    # ---- discover .zip files ----
    zip_files = sorted(
        os.path.join(input_dir, f)
        for f in os.listdir(input_dir)
        if f.lower().endswith(".zip")
    )

    if not zip_files:
        print(f"  [!] No .zip files found in:  {input_dir}")
        print(f"     Place Etterna chart packs here and run again.")
        print()
        return

    print(f"  Found {len(zip_files)} pack(s) to convert.\n")

    # ---- process each pack ----
    grand_packs = 0
    grand_songs = 0
    grand_diffs = 0

    for i, zp in enumerate(zip_files, 1):
        print(f"  [{i}/{len(zip_files)}]", end="")
        songs, diffs = process_zip_pack(zp, output_dir)
        if diffs > 0:
            grand_packs += 1
            grand_songs += songs
            grand_diffs += diffs

    # ---- summary ----
    print()
    print("=" * 60)
    print(f"  All done!")
    print(f"      Packs converted:  {grand_packs}")
    print(f"      Songs converted:  {grand_songs}")
    print(f"      Difficulties:     {grand_diffs}")
    print(f"      Output folder:    {output_dir}")
    print("=" * 60)
    print()


if __name__ == "__main__":
    main()
