# music_player/cli/commands.py

import glob
import shutil
import threading
import time
from pathlib import Path
from typing import Optional
from core.player import Player
from core.library import Library
from core.metadata import write_tags
from core.downloader import download_mp3


class Commands:
    # Tri autorisé pour list_tracks : whitelist fixe (pas de SQL injecté via `sort`)
    _TRACK_SORT_SQL = {
        "id":     "id",                                    # ordre d'ajout brut (défaut historique)
        "title":  "title COLLATE NOCASE",                   # alphabétique
        "artist": "artist COLLATE NOCASE, title COLLATE NOCASE",
        "recent": "id DESC",                                 # derniers ajouts en premier
    }

    def __init__(self, library: Library, player: Player):
        self.library = library
        self.player = player
        # Téléchargement YouTube en arrière-plan (cf. download_youtube)
        self.download_status: Optional[str] = None  # texte affiché par l'UI
        self._download_thread: Optional[threading.Thread] = None
        self._download_result = None                # ("ok", Path) | ("error", str)
        self._status_clear_at = 0.0
        self.last_export_error: Optional[str] = None

    # ---------------- TRACKS ---------------- #

    def add_folder(self, args):
        if not args:
            return
        self.library.add_folder(Path(args[0]))

    def list_tracks(self, sort="id"):
        order = self._TRACK_SORT_SQL.get(sort, "id")
        cursor = self.library.conn.cursor()
        cursor.execute(f"SELECT id, title, artist, album, path FROM tracks ORDER BY {order}")
        return [
            {"id": r["id"], "title": r["title"], "artist": r["artist"],
             "album": r["album"], "path": Path(r["path"])}
            for r in cursor.fetchall()
        ]

    def playlist_tracks(self, playlist_id):
        cursor = self.library.conn.cursor()
        cursor.execute("""
            SELECT t.id, t.title, t.artist, t.album, t.path
            FROM tracks t
            JOIN playlist_tracks pt ON t.id = pt.track_id
            WHERE pt.playlist_id = ?
            ORDER BY pt.position
        """, (playlist_id,))
        return [
            {"id": r["id"], "title": r["title"], "artist": r["artist"],
             "album": r["album"], "path": Path(r["path"])}
            for r in cursor.fetchall()
        ]

    def delete_track(self, track_id):
        cursor = self.library.conn.cursor()
        cursor.execute("SELECT path FROM tracks WHERE id=?", (track_id,))
        row = cursor.fetchone()
        if not row:
            return
        path = Path(row["path"])
        try:
            if path.exists():
                path.unlink()
        except Exception:
            pass
        cursor.execute("DELETE FROM playlist_tracks WHERE track_id=?", (track_id,))
        cursor.execute("DELETE FROM tracks WHERE id=?", (track_id,))
        self.library.conn.commit()

    def sync_library(self):
        """Supprime les tracks dont le fichier n'existe plus sur disque."""
        return self.library.sync_missing_tracks()

    @staticmethod
    def resolve_folder(raw: str) -> Optional[Path]:
        """Expanse ~ et les jokers (ex. /run/user/1000/gvfs/afc*/org.videolan.vlc-ios,
        dont le nom change à chaque branchement du téléphone).
        None si aucun dossier ne correspond."""
        pattern = str(Path(raw).expanduser())
        if Path(pattern).is_dir():       # chemin littéral d'abord (gère les [ ])
            return Path(pattern)
        for match in sorted(glob.glob(pattern)):
            if Path(match).is_dir():
                return Path(match)
        return None

    def export_tracks(self, tracks, dest_folder):
        """Copie les fichiers des tracks donnés vers dest_folder (str ou Path,
        jokers acceptés). Retourne (nb_copiés, nb_échecs) ; la dernière erreur
        est disponible dans self.last_export_error."""
        self.last_export_error = None
        raw = str(dest_folder)
        dest = self.resolve_folder(raw)
        if dest is None:
            if glob.has_magic(raw):
                self.last_export_error = f"Folder not found: {raw} (phone connected?)"
                return 0, len(tracks)
            dest = Path(raw).expanduser()
            try:
                dest.mkdir(parents=True, exist_ok=True)
            except OSError as e:
                self.last_export_error = f"{dest}: {e}"
                return 0, len(tracks)
        copied, failed = 0, 0
        for t in tracks:
            try:
                # copyfile et non copy2 : les montages gvfs (afc, mtp...) refusent
                # chmod/utime, ce qui fait échouer copy2 alors que le contenu est copié.
                shutil.copyfile(t["path"], dest / Path(t["path"]).name)
                copied += 1
            except Exception as e:
                failed += 1
                self.last_export_error = f"{Path(t['path']).name}: {e}"
        return copied, failed

    # ---------------- PLAYER CONTROL ---------------- #

    def play_track(self, track_id=None):
        tracks = self.list_tracks()
        if not tracks:
            return
        self.player.set_playlist([t["path"] for t in tracks])
        if track_id:
            idx = next((i for i, t in enumerate(tracks) if t["id"] == track_id), None)
            if idx is not None:
                self.player.play(idx)
        else:
            self.player.play()

    def pause_track(self):    self.player.pause()
    def stop_track(self):     self.player.stop()
    def next_track(self):     self.player.next()
    def prev_track(self):     self.player.prev()
    def seek_forward(self, s=10):  self.player.seek(s)
    def seek_backward(self, s=10): self.player.seek(-s)
    def adjust_volume(self, delta: int): self.player.adjust_volume(delta)

    # ---------------- METADATA ---------------- #

    def edit_track(self, track_id, updates: dict):
        cursor = self.library.conn.cursor()
        cursor.execute("SELECT * FROM tracks WHERE id=?", (track_id,))
        row = cursor.fetchone()
        if not row:
            return
        write_tags(
            Path(row["path"]),
            title=updates.get("title"),
            artist=updates.get("artist"),
            album=updates.get("album"),
            cover_path=updates.get("cover")
        )
        cursor.execute(
            "UPDATE tracks SET title=?, artist=?, album=? WHERE id=?",
            (
                updates.get("title") or row["title"],
                updates.get("artist") or row["artist"],
                updates.get("album") or row["album"],
                track_id,
            )
        )
        self.library.conn.commit()

    # ---------------- YOUTUBE ---------------- #

    def download_youtube(self, url) -> bool:
        """Lance le téléchargement en arrière-plan (l'UI reste utilisable et
        affiche l'avancement via self.download_status au lieu de la sortie de
        yt-dlp). False si impossible : pas de dossier musique, ou un
        téléchargement est déjà en cours."""
        music_folder = self.library.get_setting("music_folder")
        if not music_folder or self.downloading():
            return False
        folder = Path(music_folder).expanduser()
        self.download_status = "DL starting..."
        self._download_result = None

        def worker():
            try:
                path = download_mp3(url, folder, on_progress=self._set_download_status)
                self._download_result = ("ok", path)
            except Exception as e:
                self._download_result = ("error", str(e))

        self._download_thread = threading.Thread(target=worker, daemon=True)
        self._download_thread.start()
        return True

    def downloading(self) -> bool:
        alive = self._download_thread is not None and self._download_thread.is_alive()
        return alive or self._download_result is not None

    def _set_download_status(self, text: str):
        self.download_status = text

    def poll_download(self):
        """À appeler à chaque frame depuis le thread principal : l'ajout en base
        doit s'y faire (connexion sqlite liée à son thread). Finalise un
        téléchargement terminé et efface le message au bout de quelques secondes."""
        result = self._download_result
        if result is not None:
            self._download_result = None
            kind, value = result
            if kind == "ok":
                self.library.add_track(value)
                self.download_status = f"DL done: {value.stem}"
            else:
                self.download_status = "DL error: " + " ".join(str(value).split())
            self._status_clear_at = time.monotonic() + 5
        elif (self.download_status and not self.downloading()
              and self._status_clear_at and time.monotonic() >= self._status_clear_at):
            self.download_status = None
            self._status_clear_at = 0.0

    # ---------------- PLAYLISTS ---------------- #

    def list_playlists(self):
        cursor = self.library.conn.cursor()
        cursor.execute("SELECT id, name FROM playlists ORDER BY name")
        return [{"id": r["id"], "name": r["name"]} for r in cursor.fetchall()]

    def create_playlist(self, name):
        self.library.create_playlist(name)

    def add_to_playlist(self, playlist_id, track_id):
        self.library.add_track_to_playlist(playlist_id, track_id)

    def remove_from_playlist(self, playlist_id, track_id):
        self.library.remove_track_from_playlist(playlist_id, track_id)

    def delete_playlist(self, playlist_id):
        self.library.delete_playlist(playlist_id)
