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


@dataclass
class TrackEdit:
    """Édition NON destructive d'un morceau (appliquée à la lecture).

    start_ms / end_ms délimitent la portion lue (dans la timeline du fichier) ;
    gain_db est un gain appliqué au volume du lecteur."""
    start_ms: int = 0
    end_ms: Optional[int] = None  # None = jusqu'à la fin du fichier
    gain_db: float = 0.0

    def is_default(self) -> bool:
        return self.start_ms == 0 and self.end_ms is None and self.gain_db == 0.0
