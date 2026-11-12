# music_player/core/audio_edit.py
import math
import os
import shutil
import subprocess
from pathlib import Path

from mutagen.id3 import ID3
from mutagen.mp3 import MP3

from core.models import TrackEdit


class EditError(RuntimeError):
    pass


def parse_timestamp(text: str) -> float:
    """'83', '83.5', '1:23', '1:23.5' ou '0:01:23' -> secondes. ValueError si invalide."""
    parts = text.strip().split(":")
    if not text.strip() or len(parts) > 3:
        raise ValueError(f"invalid timestamp: {text!r}")
    seconds = 0.0
    for part in parts:
        value = float(part)
        if value < 0 or not math.isfinite(value):
            raise ValueError(f"invalid timestamp: {text!r}")
        seconds = seconds * 60 + value
    return seconds


def format_timestamp(seconds: float) -> str:
    minutes, secs = divmod(max(0.0, seconds), 60)
    return f"{int(minutes)}:{secs:04.1f}"


def _target_bitrate(path: Path) -> int:
    """Débit du fichier d'origine (pour ne pas dégrader un 320k en 192k)."""
    try:
        bitrate = int(MP3(path).info.bitrate)
        if bitrate > 0:
            return bitrate
    except Exception:
        pass
    return 192_000


def bake_edit(path: Path, edit: TrackEdit) -> None:
    """Applique DÉFINITIVEMENT l'édition au fichier avec ffmpeg.

    - coupe seule : copie du flux audio (sans perte) ;
    - gain : ré-encodage mp3 au débit d'origine ;
    - tags ID3 et pochette conservés (la pochette est ré-attachée avec mutagen :
      ffmpeg l'abandonne quand on coupe le début) ;
    - écriture dans un fichier temporaire puis os.replace (atomique) : en cas
      d'échec, le fichier d'origine n'est jamais touché."""
    ffmpeg = shutil.which("ffmpeg")
    if not ffmpeg:
        raise EditError("ffmpeg not found")
    if edit.is_default():
        return

    path = Path(path)
    tmp = path.with_name(f".{path.stem}.edit-tmp{path.suffix}")

    cmd = [ffmpeg, "-y", "-v", "error", "-i", str(path)]
    if edit.start_ms:
        cmd += ["-ss", f"{edit.start_ms / 1000:.3f}"]
    if edit.end_ms is not None:
        cmd += ["-t", f"{(edit.end_ms - edit.start_ms) / 1000:.3f}"]
    cmd += ["-map", "0:a:0"]
    if edit.gain_db:
        cmd += ["-af", f"volume={edit.gain_db:.2f}dB",
                "-c:a", "libmp3lame", "-b:a", str(_target_bitrate(path))]
    else:
        cmd += ["-c:a", "copy"]
    cmd += ["-map_metadata", "0", "-id3v2_version", "3", str(tmp)]

    try:
        covers = ID3(path).getall("APIC")
    except Exception:
        covers = []

    try:
        result = subprocess.run(cmd, capture_output=True, text=True)
        if result.returncode != 0:
            raise EditError((result.stderr or "ffmpeg failed").strip().splitlines()[-1])
        if covers:
            tags = ID3(tmp)
            for frame in covers:
                tags.add(frame)
            tags.save(tmp, v2_version=3)
        os.replace(tmp, path)
    finally:
        tmp.unlink(missing_ok=True)
