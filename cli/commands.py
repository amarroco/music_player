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
from core.audio_edit import bake_edit
from core.models import TrackEdit


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

    def queue_tracks(self):
        """Morceaux de la file de lecture (dans l'ordre de lecture), enrichis
        des infos de la bibliothèque quand le fichier y est connu."""
        by_path = {t["path"]: t for t in self.list_tracks()}
        return [
            by_path.get(path) or {"id": None, "title": path.stem, "artist": None,
                                  "album": None, "path": path}
            for path in self.player.playlist
        ]

    def track_info(self, path: Path):
        """Infos de la bibliothèque pour un fichier (None si inconnu)."""
        cursor = self.library.conn.cursor()
        cursor.execute(
            "SELECT id, title, artist, album, path FROM tracks WHERE path=?", (str(path),)
        )
        r = cursor.fetchone()
        if not r:
            return None
        return {"id": r["id"], "title": r["title"], "artist": r["artist"],
                "album": r["album"], "path": Path(r["path"])}

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
        self.player.remove_path(path)  # sinon la file garde un fichier disparu
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

    @staticmethod
    def _import_target(src: Path, dest: Path):
        """Chemin de destination libre pour src, ou None si c'est un doublon."""
        target = dest / src.name
        n = 1
        while target.exists():
            if target.samefile(src) or target.stat().st_size == src.stat().st_size:
                return None
            target = dest / f"{src.stem} ({n}){src.suffix}"
            n += 1
        return target

    def import_folder(self, src_folder: Path, progress=None):
        """Copie les .mp3 de src_folder (récursif) dans le dossier musique et
        les ajoute à la bibliothèque. progress(done, total) est appelé après
        chaque fichier. Retourne (copiés, ignorés, échecs) ; un fichier de même
        nom ET de même taille est un doublon (ignoré), de même nom mais de taille
        différente est copié sous un nom suffixé."""
        music_folder = self.library.get_setting("music_folder")
        if not music_folder:
            return 0, 0, 0
        dest = Path(music_folder).expanduser()
        dest.mkdir(parents=True, exist_ok=True)
        files = sorted(f for f in Path(src_folder).rglob("*") if f.suffix.lower() == ".mp3")
        copied = skipped = failed = 0
        for done, src in enumerate(files, start=1):
            try:
                target = self._import_target(src, dest)
                if target is None:
                    skipped += 1
                else:
                    # copyfile et non copy2 : comme pour l'export, les montages
                    # gvfs (afc, mtp...) refusent chmod/utime.
                    shutil.copyfile(src, target)
                    self.library.add_track(target)
                    copied += 1
            except Exception:
                failed += 1
            if progress:
                progress(done, len(files))
        return copied, skipped, failed

    # ---------------- AUDIO EDIT (début/fin/gain) ---------------- #

    def get_track_edit(self, path: Path) -> TrackEdit:
        return self.library.get_track_edit(path)

    def set_track_edit(self, path: Path, edit: TrackEdit):
        self.library.set_track_edit(path, edit)

    def bake_track_edit(self, path: Path):
        """Applique définitivement l'édition au fichier (ffmpeg), puis la remet à zéro."""
        edit = self.library.get_track_edit(path)
        bake_edit(path, edit)
        self.library.set_track_edit(path, TrackEdit())

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
