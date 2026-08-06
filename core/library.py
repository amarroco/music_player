# music_player/core/library.py
import sqlite3
from pathlib import Path
from core.metadata import read_tags


class Library:
    def __init__(self, db_path="music_player.db"):
        self.db_path = Path(db_path)
        self.conn = sqlite3.connect(self.db_path)
        self.conn.row_factory = sqlite3.Row
        self._init_db()

    def _init_db(self):
        cursor = self.conn.cursor()

        cursor.execute("""
        CREATE TABLE IF NOT EXISTS tracks (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            title TEXT,
            artist TEXT,
            album TEXT,
            path TEXT UNIQUE,
            source TEXT DEFAULT 'local'
        )
        """)
        cursor.execute("""
        CREATE TABLE IF NOT EXISTS playlists (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT UNIQUE
        )
        """)
        cursor.execute("""
        CREATE TABLE IF NOT EXISTS playlist_tracks (
            playlist_id INTEGER,
            track_id INTEGER,
            position INTEGER,
            PRIMARY KEY (playlist_id, track_id),
            FOREIGN KEY (playlist_id) REFERENCES playlists(id),
            FOREIGN KEY (track_id) REFERENCES tracks(id)
        )
        """)
        cursor.execute("""
        CREATE TABLE IF NOT EXISTS settings (
            key TEXT PRIMARY KEY,
            value TEXT
        )
        """)
        self.conn.commit()

        # Migration : ajouter la colonne position si elle n'existe pas
        cursor.execute("PRAGMA table_info(playlist_tracks)")
        columns = [row[1] for row in cursor.fetchall()]
        if "position" not in columns:
            cursor.execute("""
            CREATE TABLE playlist_tracks_new (
                playlist_id INTEGER,
                track_id INTEGER,
                position INTEGER,
                PRIMARY KEY (playlist_id, track_id),
                FOREIGN KEY (playlist_id) REFERENCES playlists(id),
                FOREIGN KEY (track_id) REFERENCES tracks(id)
            )
            """)
            # Copier les données en assignant une position basée sur track_id
            cursor.execute("SELECT DISTINCT playlist_id FROM playlist_tracks")
            playlist_ids = [r[0] for r in cursor.fetchall()]
            for pid in playlist_ids:
                cursor.execute(
                    "SELECT track_id FROM playlist_tracks WHERE playlist_id=? ORDER BY track_id",
                    (pid,)
                )
                for pos, row in enumerate(cursor.fetchall(), start=1):
                    cursor.execute(
                        "INSERT INTO playlist_tracks_new VALUES (?, ?, ?)",
                        (pid, row[0], pos)
                    )
            cursor.execute("DROP TABLE playlist_tracks")
            cursor.execute("ALTER TABLE playlist_tracks_new RENAME TO playlist_tracks")
            self.conn.commit()

    # ---------------- Tracks ---------------- #

    def add_folder(self, folder: Path):
        if not folder.is_dir():
            return
        for file in folder.rglob("*.mp3"):
            self.add_track(file)

    def add_track(self, file_path: Path, source="local"):
        file_path = Path(file_path)
        if not file_path.exists():
            return
        tags = read_tags(file_path)
        title = tags.get("title") or file_path.stem
        artist = tags.get("artist")
        album = tags.get("album")
        cursor = self.conn.cursor()
        try:
            cursor.execute(
                "INSERT INTO tracks (title, artist, album, path, source) VALUES (?, ?, ?, ?, ?)",
                (title, artist, album, str(file_path), source)
            )
            self.conn.commit()
        except sqlite3.IntegrityError:
            pass  # déjà présent

    def sync_missing_tracks(self):
        """Supprime de la BDD tous les tracks dont le fichier n'existe plus sur disque."""
        cursor = self.conn.cursor()
        cursor.execute("SELECT id, path FROM tracks")
        rows = cursor.fetchall()
        removed = 0
        for row in rows:
            if not Path(row["path"]).exists():
                cursor.execute("DELETE FROM playlist_tracks WHERE track_id=?", (row["id"],))
                cursor.execute("DELETE FROM tracks WHERE id=?", (row["id"],))
                removed += 1
        if removed:
            self.conn.commit()
        return removed

    # ---------------- Playlists ---------------- #

    def create_playlist(self, name: str):
        cursor = self.conn.cursor()
        try:
            cursor.execute("INSERT INTO playlists (name) VALUES (?)", (name,))
            self.conn.commit()
        except Exception:
            pass

    def add_track_to_playlist(self, playlist_id: int, track_id: int):
        cursor = self.conn.cursor()
        try:
            cursor.execute(
                "SELECT MAX(position) as max_pos FROM playlist_tracks WHERE playlist_id=?",
                (playlist_id,)
            )
            row = cursor.fetchone()
            next_pos = (row["max_pos"] or 0) + 1
            cursor.execute(
                "INSERT INTO playlist_tracks (playlist_id, track_id, position) VALUES (?, ?, ?)",
                (playlist_id, track_id, next_pos)
            )
            self.conn.commit()
        except Exception:
            pass

    def remove_track_from_playlist(self, playlist_id: int, track_id: int):
        cursor = self.conn.cursor()
        cursor.execute(
            "DELETE FROM playlist_tracks WHERE playlist_id=? AND track_id=?",
            (playlist_id, track_id)
        )
        self.conn.commit()

    def delete_playlist(self, playlist_id: int):
        cursor = self.conn.cursor()
        cursor.execute("DELETE FROM playlist_tracks WHERE playlist_id=?", (playlist_id,))
        cursor.execute("DELETE FROM playlists WHERE id=?", (playlist_id,))
        self.conn.commit()

    # ---------------- Settings ---------------- #

    def get_setting(self, key: str):
        cursor = self.conn.cursor()
        cursor.execute("SELECT value FROM settings WHERE key=?", (key,))
        row = cursor.fetchone()
        return row["value"] if row else None

    def set_setting(self, key: str, value: str):
        cursor = self.conn.cursor()
        cursor.execute("INSERT OR REPLACE INTO settings (key, value) VALUES (?, ?)", (key, value))
        self.conn.commit()

    # ---------------- Cleanup ---------------- #

    def close(self):
        self.conn.close()
