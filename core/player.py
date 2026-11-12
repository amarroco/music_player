# music_player/core/player.py
import vlc
from pathlib import Path
from typing import Callable, Optional, List

from core.models import TrackEdit

class Player:
    def __init__(self):
        self.instance = vlc.Instance()
        self.player: vlc.MediaPlayer = self.instance.media_player_new()
        self.playlist: List[Path] = []
        self.current_index: Optional[int] = None
        self.state = "STOPPED"  # STOPPED, PLAYING, PAUSED
        self._volume = 100  # source de vérité côté Python : reste valide
                             # même avant qu'un média ait été chargé dans VLC
        # Fournit l'édition (début/fin/gain) d'un fichier ; branché dans main.py
        self.edit_provider: Optional[Callable[[Path], TrackEdit]] = None
        self._edit = TrackEdit()        # édition du morceau en cours de lecture
        self._volume_pending = False    # volume (avec gain) à appliquer dès que VLC lit
        self._seen_playing = False      # VLC a atteint "Playing" pour ce morceau

    def set_playlist(self, tracks: List[Path], start_index: Optional[int] = 0):
        self.playlist = list(tracks)  # copie : la file est modifiable (move/remove)
        self.current_index = start_index if tracks else None

    def play(self, index: Optional[int] = None):
        if index is not None:
            if 0 <= index < len(self.playlist):
                self.current_index = index
            else:
                return
        if self.current_index is None:
            return
        track = self.playlist[self.current_index]
        self._edit = self.edit_provider(track) if self.edit_provider else TrackEdit()
        media = self.instance.media_new(str(track))
        if self._edit.start_ms:
            media.add_option(f":start-time={self._edit.start_ms / 1000:.3f}")
        if self._edit.end_ms is not None:
            media.add_option(f":stop-time={self._edit.end_ms / 1000:.3f}")
        self.player.set_media(media)
        self.player.play()
        # Pas d'appel à audio_set_volume ici : play() est asynchrone et la
        # sortie audio n'existe pas encore à cet instant précis — l'appeler
        # trop tôt segfaulte libvlc (même famille de bug que le guard dans
        # adjust_volume ci-dessous, juste décalée de quelques ms). Le volume
        # (avec le gain du morceau) est appliqué par tick() dès que VLC est
        # en état Playing.
        self._volume_pending = True
        self._seen_playing = False
        self.state = "PLAYING"

    def pause(self):
        if self.state in ("PLAYING", "PAUSED"):
            self.player.pause()
            self.state = "PAUSED" if self.state == "PLAYING" else "PLAYING"

    def stop(self):
        self.player.stop()
        self.state = "STOPPED"

    def next(self):
        if self.current_index is None:
            return
        if self.current_index + 1 < len(self.playlist):
            self.current_index += 1
            self.play()
        else:
            self.stop()

    def prev(self):
        if self.current_index is None:
            return
        if self.current_index - 1 >= 0:
            self.current_index -= 1
            self.play()

    # ---------------- FILE DE LECTURE ---------------- #

    def move(self, index: int, new_index: int) -> bool:
        """Déplace le morceau `index` en `new_index` ; current_index continue
        de désigner le même morceau."""
        n = len(self.playlist)
        if index == new_index or not (0 <= index < n and 0 <= new_index < n):
            return False
        self.playlist.insert(new_index, self.playlist.pop(index))
        cur = self.current_index
        if cur is not None:
            if cur == index:
                self.current_index = new_index
            elif index < cur <= new_index:
                self.current_index = cur - 1
            elif new_index <= cur < index:
                self.current_index = cur + 1
        return True

    def remove(self, index: int) -> bool:
        """Retire un morceau de la file. Si c'est le morceau en cours, la
        lecture passe à celui qui prend sa place (ou s'arrête s'il n'y en a plus)."""
        if not (0 <= index < len(self.playlist)):
            return False
        del self.playlist[index]
        cur = self.current_index
        if cur is None:
            return True
        if index < cur:
            self.current_index = cur - 1
        elif index == cur:
            if index < len(self.playlist):
                self.play(index)
            else:
                self.stop()
                self.current_index = None
        return True

    def remove_path(self, path: Path):
        """Retire toutes les occurrences d'un fichier (ex. fichier supprimé)."""
        for i in reversed(range(len(self.playlist))):
            if self.playlist[i] == path:
                self.remove(i)

    def current_track_path(self) -> Optional[Path]:
        if self.playlist and self.current_index is not None \
                and 0 <= self.current_index < len(self.playlist):
            return self.playlist[self.current_index]
        return None

    # ---------------- POSITION ---------------- #
    # Position/durée exposées à l'UI : relatives à la portion jouée (début/fin
    # de l'édition) ; file_position() donne la position dans le fichier brut.

    def file_position(self) -> float:
        pos_ms = self.player.get_time()
        if pos_ms is None or pos_ms < 0:
            return 0.0
        return pos_ms / 1000.0

    def current_position(self) -> float:
        return max(0.0, self.file_position() - self._edit.start_ms / 1000.0)

    def current_duration(self) -> float:
        dur_ms = self.player.get_length()
        if dur_ms is None or dur_ms <= 0:
            return 0.0
        end_ms = dur_ms
        if self._edit.end_ms is not None:
            end_ms = min(end_ms, self._edit.end_ms)
        return max(0.0, (end_ms - self._edit.start_ms) / 1000.0)

    def seek(self, seconds: float):
        dur = self.current_duration()
        if dur <= 0:
            return
        new_pos = max(0, min(self.current_position() + seconds, dur))
        self.player.set_time(int((new_pos + self._edit.start_ms / 1000.0) * 1000))

    # États où la sortie audio n'existe pas encore (ou plus) côté libvlc :
    # y toucher (audio_set_volume) y segfaulte. NothingSpecial = aucun média
    # jamais chargé ; Opening = média en cours d'ouverture juste après play().
    _UNSAFE_AUDIO_STATES = (vlc.State.NothingSpecial, vlc.State.Opening, vlc.State.Error)

    def _effective_volume(self) -> int:
        """Volume VLC = volume utilisateur x gain du morceau (VLC accepte 0-200)."""
        gain = 10 ** (self._edit.gain_db / 20.0) if self._edit.gain_db else 1.0
        return max(0, min(200, round(self._volume * gain)))

    def adjust_volume(self, delta: int):
        self._volume = max(0, min(100, self._volume + delta))
        if self.player.get_state() not in self._UNSAFE_AUDIO_STATES:
            self.player.audio_set_volume(self._effective_volume())

    def current_volume(self) -> int:
        return self._volume

    def tick(self):
        """À appeler à chaque frame : applique le volume du morceau dès que VLC
        lit, puis gère le passage au morceau suivant."""
        if self.state != "PLAYING":
            return
        if self.player.get_state() == vlc.State.Playing:
            self._seen_playing = True
            if self._volume_pending:
                self.player.audio_set_volume(self._effective_volume())
                self._volume_pending = False
        self.check_auto_next()

    def check_auto_next(self):
        """Passe au morceau suivant si le morceau courant est terminé."""
        if self.state != "PLAYING":
            return
        # Avec une fin éditée (:stop-time), VLC s'arrête de lui-même (Ended) avant
        # que la position n'atteigne la durée du fichier. On ne s'y fie qu'après
        # avoir vu "Playing" : juste après play(), get_state() peut encore
        # renvoyer l'état Ended du morceau précédent.
        if self._seen_playing and self.player.get_state() == vlc.State.Ended:
            self.next()
            return
        if self.current_duration() > 0:
            if self.current_position() >= self.current_duration() - 0.5:  # marge 0.5s
                self.next()
