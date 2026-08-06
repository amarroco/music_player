# music_player/cli/ui.py

import curses
import time
from cli.commands import Commands
from core.library import Library
from core.player import Player
from pathlib import Path

# (id_setting, label affiché, valeur par défaut)
CONTROL_ACTIONS = [
    ("pause",    "Play / Pause",   " "),
    ("vol_up",   "Volume Up",      "+"),
    ("vol_down", "Volume Down",    "-"),
    ("next",     "Next Track",     "*"),
    ("prev",     "Prev Track",     "/"),
    ("seek_fwd", "Seek +10s",      "l"),
    ("seek_bwd", "Seek -10s",      "j"),
    ("quit",     "Quit",           "q"),
]


def key_display(char: str) -> str:
    if char == " ":
        return "SPACE"
    return char.upper()


class UI:
    STATES = [
        "main", "tracks", "playlists", "playlist_internal",
        "settings", "controls", "input_prompt", "selection"
    ]

    def __init__(self, library: Library, player: Player, cmds: Commands):
        self.library = library
        self.player = player
        self.cmds = cmds

        self.running = True
        self.state = "main"
        self.prev_state = "main"
        self.selected_index = 0
        self.scroll_offset = 0       # premier item visible dans le menu
        self.active_playlist = None
        self.items = []
        self.input_buffer = ""
        self.selection_items = []
        self.selection_callback = None
        self.stdscr = None
        self.first_launch = False
        self._controls: dict = {}
        self._menu_visible_rows = 0  # mis à jour dans draw_menu_box

    # ---------------- CONTROLS ---------------- #

    def _load_controls(self):
        self._controls = {}
        for action_id, _, default in CONTROL_ACTIONS:
            val = self.library.get_setting(f"ctrl_{action_id}")
            self._controls[action_id] = val if val else default

    def _ctrl(self, action_id: str) -> int:
        return ord(self._controls.get(action_id, " "))

    # ---------------- LOOP ---------------- #

    def start(self):
        music_folder = self.library.get_setting("music_folder")
        if not music_folder:
            self.first_launch = True
        else:
            folder_path = Path(music_folder).expanduser()
            if folder_path.exists():
                self.library.add_folder(folder_path)
            self.cmds.sync_library()
        curses.wrapper(self.main_loop)

    def main_loop(self, stdscr):
        self.stdscr = stdscr
        curses.curs_set(0)
        stdscr.nodelay(True)
        stdscr.timeout(100)
        self._load_controls()

        if self.first_launch:
            self.state = "settings"
            self.first_launch = False

        while self.running:
            self.stdscr.clear()
            h, w = self.stdscr.getmaxyx()
            self.render_current_state(h, w)
            self.stdscr.refresh()
            key = self.stdscr.getch()
            self.handle_key(key)
            self.player.check_auto_next()
            time.sleep(0.05)

    # ---------------- RENDER ---------------- #

    def _shortcuts_visible(self) -> bool:
        return self.library.get_setting("show_shortcuts") != "off"

    def render_current_state(self, h, w):
        shortcuts_h = 4 if self._shortcuts_visible() else 0
        self.draw_current_track_box(0, 0, 5, w)
        # La box menu occupe tout l'espace entre le track et le bas
        menu_y = 6
        menu_h = h - menu_y - shortcuts_h
        self.draw_menu_box(menu_y, 0, menu_h, w)
        if shortcuts_h:
            self.draw_shortcuts_box(h - shortcuts_h, 0, shortcuts_h, w)

        # Remplir items selon l'état
        if self.state == "main":
            self.items = ["Tracks", "Playlists", "Settings", "Quit"]
        elif self.state == "tracks":
            self.items = self.cmds.list_tracks()
        elif self.state == "playlists":
            self.items = self.cmds.list_playlists()
        elif self.state == "playlist_internal" and self.active_playlist:
            self.items = self.cmds.playlist_tracks(self.active_playlist["id"])
        elif self.state == "settings":
            music_folder = self.library.get_setting("music_folder") or "Not configured"
            sc = "ON" if self._shortcuts_visible() else "OFF"
            self.items = [
                f"Music Folder: {music_folder}",
                "Change Music Folder",
                f"Show Shortcuts: {sc}",
                "Controls",
                "Back to Main Menu",
            ]
        elif self.state == "controls":
            items = []
            for action_id, label, default in CONTROL_ACTIONS:
                val = self.library.get_setting(f"ctrl_{action_id}") or default
                items.append(f"{label:<22} [{key_display(val)}]")
            items.append("Back to Settings")
            self.items = items
        elif self.state == "selection":
            self.items = self.selection_items

        # Clamp selected_index
        if self.items:
            self.selected_index = max(0, min(self.selected_index, len(self.items) - 1))
        else:
            self.selected_index = 0

        # Maintenir le scroll pour garder l'item sélectionné visible
        self._clamp_scroll()

    # ---------------- SCROLL ---------------- #

    def _clamp_scroll(self):
        """Ajuste scroll_offset pour que selected_index reste dans la zone visible."""
        vis = max(1, self._menu_visible_rows)
        if self.selected_index < self.scroll_offset:
            self.scroll_offset = self.selected_index
        elif self.selected_index >= self.scroll_offset + vis:
            self.scroll_offset = self.selected_index - vis + 1
        # Ne pas dépasser la fin
        max_offset = max(0, len(self.items) - vis)
        self.scroll_offset = max(0, min(self.scroll_offset, max_offset))

    # ---------------- BOXES ---------------- #

    def draw_box(self, y, x, h, w, title=None):
        try:
            self.stdscr.addstr(y, x, "+" + "-" * (w - 2) + "+")
            for i in range(1, h - 1):
                self.stdscr.addstr(y + i, x, "|")
                self.stdscr.addstr(y + i, x + w - 1, "|")
            self.stdscr.addstr(y + h - 1, x, "+" + "-" * (w - 2) + "+")
            if title:
                self.stdscr.addstr(y, x + 2, f"[ {title} ]")
        except curses.error:
            pass

    def draw_current_track_box(self, y, x, h, w):
        self.draw_box(y, x, h, w, "Current Track")
        track = self.player.current_track_path()
        pos, dur = self.player.current_position(), self.player.current_duration()
        bar_len = max(w - 10, 10)
        progress = int((pos / dur) * bar_len) if dur else 0
        bar = "[" + "#" * progress + "-" * (bar_len - progress) + "]"
        try:
            self.stdscr.addstr(y + 1, x + 2, track.stem if track else "No track playing")
            self.stdscr.addstr(y + 2, x + 2, bar)
            self.stdscr.addstr(
                y + 3, x + 2,
                f"{int(pos)//60}:{int(pos)%60:02d} / {int(dur)//60}:{int(dur)%60:02d}",
            )
        except curses.error:
            pass

    def draw_menu_box(self, y, x, h, w):
        self.draw_box(y, x, h, w, "Menu")

        interior_rows = h - 2          # lignes disponibles entre les bordures
        self._menu_visible_rows = interior_rows

        n = len(self.items)
        if n == 0:
            return

        need_top_dots = self.scroll_offset > 0
        need_bot_dots = (self.scroll_offset + interior_rows) < n

        # Si on affiche des "...", on réserve une ligne pour eux
        top_reserve = 1 if need_top_dots else 0
        bot_reserve = 1 if need_bot_dots else 0
        items_rows = interior_rows - top_reserve - bot_reserve

        draw_row = y + 1  # première ligne intérieure

        # "..." en haut
        if need_top_dots:
            try:
                self.stdscr.addstr(draw_row, x + 2, ". . .")
            except curses.error:
                pass
            draw_row += 1

        # Items visibles
        for i in range(items_rows):
            idx = self.scroll_offset + i
            if idx >= n:
                break
            item = self.items[idx]
            name = (
                item["title"] if isinstance(item, dict) and "title" in item
                else item.get("name") if isinstance(item, dict)
                else str(item)
            )
            # Tronquer pour ne pas dépasser la largeur de la box
            max_name = w - 5
            if len(name) > max_name:
                name = name[:max_name - 1] + "…"
            try:
                attr = curses.A_REVERSE if idx == self.selected_index else 0
                self.stdscr.addstr(draw_row, x + 2, name, attr)
            except curses.error:
                pass
            draw_row += 1

        # "..." en bas
        if need_bot_dots:
            try:
                self.stdscr.addstr(draw_row, x + 2, ". . .")
            except curses.error:
                pass

    def draw_shortcuts_box(self, y, x, h, w):
        if self.state == "input_prompt":
            return
        self.draw_box(y, x, h, w, "Shortcuts")

        ctrl = self._controls
        def c(k): return key_display(ctrl.get(k, "?"))

        global_line = (
            f"{c('pause')}-play/pause | BKSP-back | ESC/{c('quit')}-quit | "
            f"{c('vol_up')}/{c('vol_down')}-vol | {c('prev')}/{c('next')}-prev/next | "
            f"{c('seek_bwd')}/{c('seek_fwd')} ±10s"
        )
        menu_line = {
            "tracks":           "ENTER-play | X-delete | D-download",
            "playlists":        "ENTER-play | O-open | C-create | X-delete",
            "playlist_internal":"ENTER-play | X-remove | A-add track",
            "settings":         "ENTER-select",
            "controls":         "ENTER-remap key",
            "selection":        "Arrows + ENTER to select",
        }.get(self.state, "")

        try:
            self.stdscr.addstr(y + 1, x + 2, global_line[:w - 4])
            self.stdscr.addstr(y + 2, x + 2, menu_line[:w - 4])
        except curses.error:
            pass

    # ---------------- KEY HANDLER ---------------- #

    def handle_key(self, key):
        if key == -1:
            return

        # Quitter (ESC ou touche quit)
        if key == 27 or key == self._ctrl("quit"):
            self.running = False
            self.player.stop()
            return

        # Contrôles lecteur (remappables)
        if key == self._ctrl("pause"):
            self.player.pause()
        elif key == self._ctrl("vol_up"):
            self.player.adjust_volume(5)
        elif key == self._ctrl("vol_down"):
            self.player.adjust_volume(-5)
        elif key == self._ctrl("next"):
            self.player.next()
        elif key == self._ctrl("prev"):
            self.player.prev()
        elif key == self._ctrl("seek_fwd"):
            self.player.seek(10)
        elif key == self._ctrl("seek_bwd"):
            self.player.seek(-10)

        # Navigation
        elif key in (curses.KEY_BACKSPACE, 127):
            self._handle_backspace()
            return
        elif key == curses.KEY_UP:
            if self.selected_index > 0:
                self.selected_index -= 1
                self._clamp_scroll()
        elif key == curses.KEY_DOWN:
            if self.selected_index < len(self.items) - 1:
                self.selected_index += 1
                self._clamp_scroll()

        # Handler d'état
        handler = getattr(self, f"handle_state_{self.state}", None)
        if handler:
            handler(key)

        if self.state not in ("input_prompt", "selection"):
            self.handle_end_of_track()

    def _handle_backspace(self):
        """Gère le retour arrière selon l'état courant."""
        if self.state == "playlist_internal":
            self.state = "playlists"
        elif self.state == "controls":
            self.state = "settings"
        elif self.state == "selection":
            # Retour à l'état précédent (playlist_internal si on ajoutait un track)
            self.state = self.prev_state
        else:
            self.state = "main"
        self.selected_index = 0
        self.scroll_offset = 0
        # Ne pas toucher à active_playlist si on revient dans playlist_internal
        if self.state not in ("playlist_internal", "selection"):
            if self.state != "playlists":
                self.active_playlist = None

    # ---------------- STATES ---------------- #

    def handle_state_main(self, key):
        if key in (10, 13):
            targets = ["tracks", "playlists", "settings", None]
            target = targets[self.selected_index] if self.selected_index < len(targets) else None
            if target:
                self.state = target
            else:
                self.running = False
                self.player.stop()
            self.selected_index = 0
            self.scroll_offset = 0

    def handle_state_tracks(self, key):
        tracks = self.cmds.list_tracks()
        if not tracks:
            return
        if key in (10, 13):
            self.player.set_playlist([t["path"] for t in tracks])
            self.player.play(self.selected_index)
        elif key in (ord("x"), ord("X")):
            track = tracks[self.selected_index]
            if self.prompt_input(f"Delete '{track['title']}'? (y/n): ").lower() == "y":
                self.cmds.delete_track(track["id"])
                self.selected_index = min(self.selected_index, max(0, len(tracks) - 2))
                self.scroll_offset = 0
        elif key in (ord("d"), ord("D")):
            url = self.prompt_input("YouTube URL: ")
            if url:
                self.cmds.download_youtube(url)

    def handle_state_playlists(self, key):
        playlists = self.cmds.list_playlists()
        if key in (ord("c"), ord("C")):
            name = self.prompt_input("Playlist name: ")
            if name:
                self.cmds.create_playlist(name)
            return
        if not playlists:
            return
        playlist = playlists[self.selected_index]
        if key in (10, 13):
            tracks = self.cmds.playlist_tracks(playlist["id"])
            if tracks:
                self.player.set_playlist([t["path"] for t in tracks])
                self.player.play()
        elif key in (ord("o"), ord("O")):
            self.active_playlist = playlist
            self.state = "playlist_internal"
            self.selected_index = 0
            self.scroll_offset = 0
        elif key in (ord("x"), ord("X")):
            if self.prompt_input(f"Delete '{playlist['name']}'? (y/n): ").lower() == "y":
                self.cmds.delete_playlist(playlist["id"])
                self.selected_index = 0
                self.scroll_offset = 0

    def handle_state_playlist_internal(self, key):
        if not self.active_playlist:
            return
        tracks = self.cmds.playlist_tracks(self.active_playlist["id"])
        if key in (10, 13):
            if not tracks:
                return
            self.player.set_playlist([t["path"] for t in tracks])
            self.player.play(self.selected_index)
        elif key in (ord("x"), ord("X")):
            if not tracks:
                return
            track = tracks[self.selected_index]
            if self.prompt_input(f"Remove '{track['title']}'? (y/n): ").lower() == "y":
                self.cmds.remove_from_playlist(self.active_playlist["id"], track["id"])
                self.selected_index = min(self.selected_index, max(0, len(tracks) - 2))
                self.scroll_offset = 0
        elif key in (ord("a"), ord("A")):
            track_list = self.cmds.list_tracks()
            if track_list:
                self.start_selection(track_list, self.add_selected_to_playlist)

    def handle_state_settings(self, key):
        if key not in (10, 13):
            return
        if self.selected_index == 1:
            folder = self.prompt_input("Music folder path: ")
            if folder:
                p = Path(folder).expanduser()
                if p.exists() and p.is_dir():
                    self.library.set_setting("music_folder", str(p))
                    self.library.add_folder(p)
                    self.cmds.sync_library()
                else:
                    self.prompt_input("Invalid path. Press Enter to continue.")
        elif self.selected_index == 2:
            current = self.library.get_setting("show_shortcuts") or "on"
            self.library.set_setting("show_shortcuts", "off" if current == "on" else "on")
        elif self.selected_index == 3:
            self.state = "controls"
            self.selected_index = 0
            self.scroll_offset = 0
        elif self.selected_index == 4:
            self.state = "main"
            self.selected_index = 0
            self.scroll_offset = 0

    def handle_state_controls(self, key):
        if key not in (10, 13):
            return
        if self.selected_index == len(CONTROL_ACTIONS):
            self.state = "settings"
            self.selected_index = 0
            self.scroll_offset = 0
            return
        action_id, label, _ = CONTROL_ACTIONS[self.selected_index]
        new_key = self.prompt_key(f"New key for '{label}' (press key, ESC to cancel): ")
        if new_key is not None:
            self.library.set_setting(f"ctrl_{action_id}", new_key)
            self._load_controls()

    def handle_state_selection(self, key):
        if key in (10, 13):
            if self.selection_items and self.selection_callback:
                self.selection_callback(self.selection_items[self.selected_index])
            self.state = self.prev_state
            self.selected_index = 0
            self.scroll_offset = 0

    # ---------------- INPUT / SELECTION ---------------- #

    def start_selection(self, items, callback):
        self.selection_items = items
        self.selection_callback = callback
        self.prev_state = self.state   # sera "playlist_internal"
        self.state = "selection"
        self.selected_index = 0
        self.scroll_offset = 0

    def add_selected_to_playlist(self, track):
        if self.active_playlist and track:
            self.cmds.add_to_playlist(self.active_playlist["id"], track["id"])

    def prompt_input(self, prompt: str) -> str:
        """Saisie texte. Le bandeau raccourcis est masqué ; une box dédiée s'affiche."""
        self.input_buffer = ""
        saved_state = self.state
        self.state = "input_prompt"
        self.stdscr.nodelay(False)
        self.stdscr.timeout(-1)

        while True:
            h, w = self.stdscr.getmaxyx()
            self.stdscr.clear()
            self.draw_current_track_box(0, 0, 5, w)
            # Menu réduit pour laisser place à la zone de saisie
            self.draw_menu_box(6, 0, h - 6, w)
            # Box de saisie en bas
            try:
                by = h - 4
                self.stdscr.addstr(by,     0, "+" + "-" * (w - 2) + "+")
                self.stdscr.addstr(by + 1, 0, "|")
                self.stdscr.addstr(by + 1, w - 1, "|")
                text = f" {prompt}{self.input_buffer}"
                self.stdscr.addstr(by + 1, 2, text[:w - 4])
                self.stdscr.addstr(by + 2, 0, "+" + "-" * (w - 2) + "+")
            except curses.error:
                pass
            self.stdscr.refresh()

            try:
                key = self.stdscr.get_wch()
            except Exception:
                continue

            if isinstance(key, str):
                if key == "\n":
                    result = self.input_buffer.strip()
                    self.state = saved_state
                    self.stdscr.nodelay(True)
                    self.stdscr.timeout(100)
                    return result
                elif key in ("\b", "\x7f"):
                    self.input_buffer = self.input_buffer[:-1]
                else:
                    self.input_buffer += key
            elif key == curses.KEY_BACKSPACE:
                self.input_buffer = self.input_buffer[:-1]

    def prompt_key(self, prompt: str):
        """Capture une seule touche pour le remapping. ESC annule."""
        self.stdscr.nodelay(False)
        self.stdscr.timeout(-1)
        h, w = self.stdscr.getmaxyx()
        self.stdscr.clear()
        try:
            by = h // 2 - 1
            self.stdscr.addstr(by,     2, "+" + "-" * (w - 6) + "+")
            self.stdscr.addstr(by + 1, 2, "|")
            self.stdscr.addstr(by + 1, w - 3, "|")
            self.stdscr.addstr(by + 1, 4, prompt[:w - 8])
            self.stdscr.addstr(by + 2, 2, "+" + "-" * (w - 6) + "+")
        except curses.error:
            pass
        self.stdscr.refresh()

        result = None
        try:
            raw = self.stdscr.get_wch()
            if raw != 27 and isinstance(raw, str):
                result = raw
        except Exception:
            pass

        self.stdscr.nodelay(True)
        self.stdscr.timeout(100)
        return result

    # ---------------- END OF TRACK ---------------- #

    def handle_end_of_track(self):
        if self.player.state == "STOPPED" and self.player.current_index is not None:
            self.player.next()
