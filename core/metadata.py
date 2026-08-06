# music_player/core/metadata.py
from pathlib import Path
from mutagen.id3 import ID3, APIC, TIT2, TPE1, TALB
from mutagen.mp3 import MP3

def read_tags(file_path: Path):
    """Retourne un dict des tags principaux"""
    try:
        audio = MP3(file_path, ID3=ID3)
        tags = audio.tags
        return {
            "title": tags.get("TIT2").text[0] if tags.get("TIT2") else None,
            "artist": tags.get("TPE1").text[0] if tags.get("TPE1") else None,
            "album": tags.get("TALB").text[0] if tags.get("TALB") else None,
        }
    except Exception:
        return {}

def write_tags(file_path: Path, title=None, artist=None, album=None, cover_path=None):
    """Modifie les tags ID3 et éventuellement la pochette"""
    audio = MP3(file_path, ID3=ID3)
    if audio.tags is None:
        audio.add_tags()

    if title is not None:
        audio.tags.add(TIT2(encoding=3, text=title))
    if artist is not None:
        audio.tags.add(TPE1(encoding=3, text=artist))
    if album is not None:
        audio.tags.add(TALB(encoding=3, text=album))
    if cover_path is not None and Path(cover_path).exists():
        with open(cover_path, "rb") as img:
            audio.tags.add(APIC(
                encoding=3,
                mime="image/jpeg",
                type=3,  # cover front
                desc="Cover",
                data=img.read()
            ))
    audio.save()
