# music_player/cli/commands.py

import shutil
from pathlib import Path
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

    def export_tracks(self, tracks, dest_folder: Path):
        """Copie les fichiers des tracks donnés vers dest_folder.
        Retourne (nb_copiés, nb_échecs)."""
        dest_folder = Path(dest_folder)
        dest_folder.mkdir(parents=True, exist_ok=True)
        copied, failed = 0, 0
        for t in tracks:
            try:
                shutil.copy2(t["path"], dest_folder / Path(t["path"]).name)
                copied += 1
            except Exception:
                failed += 1
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

    def download_youtube(self, url):
        music_folder = self.library.get_setting("music_folder")
        if not music_folder:
            return
        download_mp3(url, Path(music_folder).expanduser(), self.library)

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
