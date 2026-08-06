# music_player/core/models.py
from dataclasses import dataclass
from pathlib import Path
from typing import Optional


@dataclass
class Track:
    id: int
    path: Path
    title: str
    artist: Optional[str] = None
    album: Optional[str] = None
    duration: Optional[float] = None  # secondes
    cover_path: Optional[Path] = None
    source: str = "local"  # local | youtube


@dataclass
class Playlist:
    id: int
    name: str
