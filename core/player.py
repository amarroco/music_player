# music_player/core/player.py
import vlc
from pathlib import Path
from typing import Optional, List

class Player:
    def __init__(self):
        self.instance = vlc.Instance()
        self.player: vlc.MediaPlayer = self.instance.media_player_new()
        self.playlist: List[Path] = []
        self.current_index: Optional[int] = None
        self.state = "STOPPED"  # STOPPED, PLAYING, PAUSED
        self._volume = 100  # source de vérité côté Python : reste valide
                             # même avant qu'un média ait été chargé dans VLC

    def set_playlist(self, tracks: List[Path], start_index: Optional[int] = 0):
        self.playlist = tracks
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
        media = self.instance.media_new(str(track))
        self.player.set_media(media)
        self.player.play()
        # Pas d'appel à audio_set_volume ici : play() est asynchrone et la
        # sortie audio n'existe pas encore à cet instant précis — l'appeler
        # trop tôt segfaulte libvlc (même famille de bug que le guard dans
        # adjust_volume ci-dessous, juste décalée de quelques ms). Comme
        # self.player est réutilisé d'un morceau à l'autre, le volume réglé
        # via adjust_volume() pendant une lecture reste de toute façon
        # appliqué aux morceaux suivants sans qu'on ait besoin de le refaire.
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

    def current_track_path(self) -> Optional[Path]:
        if self.playlist and 0 <= self.current_index < len(self.playlist):
            return self.playlist[self.current_index]
        return None

    def current_position(self) -> float:
        pos_ms = self.player.get_time()
        if pos_ms is None or pos_ms < 0:
            return 0.0
        return pos_ms / 1000.0

    def current_duration(self) -> float:
        dur_ms = self.player.get_length()
        if dur_ms is None or dur_ms <= 0:
            return 0.0
        return dur_ms / 1000.0

    def seek(self, seconds: float):
        dur = self.current_duration()
        if dur <= 0:
            return
        new_pos = max(0, min(self.current_position() + seconds, dur))
        self.player.set_time(int(new_pos * 1000))

    # États où la sortie audio n'existe pas encore (ou plus) côté libvlc :
    # y toucher (audio_set_volume) y segfaulte. NothingSpecial = aucun média
    # jamais chargé ; Opening = média en cours d'ouverture juste après play().
    _UNSAFE_AUDIO_STATES = (vlc.State.NothingSpecial, vlc.State.Opening, vlc.State.Error)

    def adjust_volume(self, delta: int):
        self._volume = max(0, min(100, self._volume + delta))
        if self.player.get_state() not in self._UNSAFE_AUDIO_STATES:
            self.player.audio_set_volume(self._volume)

    def current_volume(self) -> int:
        return self._volume

    def check_auto_next(self):
        """À appeler régulièrement pour passer au morceau suivant si terminé"""
        if self.state == "PLAYING" and self.current_duration() > 0:
            if self.current_position() >= self.current_duration() - 0.5:  # marge 0.5s
                self.next()
