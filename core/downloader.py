# music_player/core/downloader.py
import yt_dlp
from pathlib import Path
from core.library import Library

def download_mp3(url: str, output_folder: Path, library: Library):
    """
    Télécharge une vidéo YouTube en MP3 et l'ajoute à la bibliothèque
    """
    output_folder.mkdir(parents=True, exist_ok=True)

    ydl_opts = {
        'format': 'bestaudio/best',
        'outtmpl': str(output_folder / '%(title)s.%(ext)s'),
        'postprocessors': [{
            'key': 'FFmpegExtractAudio',
            'preferredcodec': 'mp3',
            'preferredquality': '192',
        }],
        'quiet': False,
        'noplaylist': True,
    }

    try:
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(url, download=True)
            mp3_filename = output_folder / f"{info['title']}.mp3"
            if mp3_filename.exists():
                print(f"Downloaded: {mp3_filename.name}")
                library.add_track(mp3_filename)
            else:
                print("Download failed")
    except Exception as e:
        print(f"Error downloading {url}: {e}")