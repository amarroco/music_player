# music_player/core/downloader.py
import yt_dlp
from pathlib import Path
from typing import Callable, Optional


class _QuietLogger:
    """Avale tout ce que yt-dlp voudrait écrire : sous curses, un print sur
    stdout/stderr corrompt l'écran. On ne garde que la dernière erreur."""

    def __init__(self):
        self.last_error: Optional[str] = None

    def debug(self, msg): pass
    def info(self, msg): pass
    def warning(self, msg): pass

    def error(self, msg):
        self.last_error = str(msg)


def download_mp3(url: str, output_folder: Path,
                 on_progress: Optional[Callable[[str], None]] = None) -> Path:
    """
    Télécharge une vidéo YouTube en MP3 et retourne le chemin du fichier.
    Lève RuntimeError en cas d'échec.

    N'ajoute rien à la bibliothèque : sqlite n'est utilisable que depuis le
    thread qui a ouvert la connexion, c'est donc à l'appelant de le faire.
    """
    output_folder.mkdir(parents=True, exist_ok=True)

    def hook(d):
        if on_progress is None:
            return
        if d.get("status") == "downloading":
            total = d.get("total_bytes") or d.get("total_bytes_estimate")
            if total:
                on_progress(f"DL {int(d.get('downloaded_bytes', 0) * 100 / total)}%")
            else:
                on_progress("DL ...")
        elif d.get("status") == "finished":
            on_progress("DL converting...")

    logger = _QuietLogger()
    ydl_opts = {
        'format': 'bestaudio/best',
        'outtmpl': str(output_folder / '%(title)s.%(ext)s'),
        'postprocessors': [{
            'key': 'FFmpegExtractAudio',
            'preferredcodec': 'mp3',
            'preferredquality': '192',
        }],
        'logger': logger,
        'quiet': True,
        'no_warnings': True,
        'noprogress': True,
        'noplaylist': True,
        'progress_hooks': [hook],
    }

    try:
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(url, download=True)
            # prepare_filename tient compte du nettoyage des caractères
            # interdits dans le titre ; f"{info['title']}.mp3" non.
            mp3_path = Path(ydl.prepare_filename(info)).with_suffix(".mp3")
    except Exception as e:
        raise RuntimeError(logger.last_error or str(e)) from e

    if not mp3_path.exists():
        raise RuntimeError("mp3 introuvable après la conversion")
    return mp3_path
