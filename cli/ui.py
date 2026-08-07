# music_player/cli/ui.py

import curses
import random
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
    ("sort",     "Cycle Sort",     "s"),
    ("search",   "Search Tracks",  "f"),
    ("quit",     "Quit",           "q"),
]

# Modes de tri disponibles pour la liste des tracks (touche "sort")
TRACK_SORT_MODES = ["title", "artist", "recent", "random"]
TRACK_SORT_LABELS = {"title": "Title", "artist": "Artist", "recent": "Recent", "random": "Random"}


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
        self.track_sort = "title"    # cf. TRACK_SORT_MODES
        self.track_filter = ""       # filtre de recherche courant (tracks)
        self._random_order = []      # ordre figé pour le tri "random" (liste d'ids)
        self.input_buffer = ""
        self.selection_items = []
        self.selection_callback = None
        self.stdscr = None
        self.first_launch = False
        self._controls: dict = {}
        self._menu_visible_rows = 0  # cf. _visible_rows(), recalculé à chaque frame

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
        stdscr.nodelay(True)  # getch() ne bloque jamais
        self._load_controls()
        self.track_sort = self.library.get_setting("track_sort") or "title"
        if self.track_sort not in TRACK_SORT_MODES:
            self.track_sort = "title"

        if self.first_launch:
            self.state = "settings"
            self.first_launch = False

        while self.running:
            # On vide entièrement le buffer clavier à chaque frame : si une
            # touche a été maintenue (auto-repeat), toutes ses répétitions
            # sont traitées immédiatement au lieu de s'accumuler et de
            # continuer à scroller après le relâchement de la touche.
            key = self.stdscr.getch()
            while key != -1:
                self.handle_key(key)
                key = self.stdscr.getch()

            self.player.check_auto_next()
            self.stdscr.clear()
            h, w = self.stdscr.getmaxyx()
            self.render_current_state(h, w)
            self.stdscr.refresh()
            time.sleep(0.03)

    # ---------------- RENDER ---------------- #

    def _shortcuts_visible(self) -> bool:
        return self.library.get_setting("show_shortcuts") != "off"

    def render_current_state(self, h, w):
        shortcuts_h = 4 if self._shortcuts_visible() else 0
        self.draw_current_track_box(0, 0, 5, w)

        # Items d'abord : draw_menu_box et _clamp_scroll doivent voir la
        # liste à jour de cette frame, jamais celle de l'état précédent.
        self._refresh_items()

        if self.items:
            self.selected_index = max(0, min(self.selected_index, len(self.items) - 1))
        else:
            self.selected_index = 0

        # La box menu occupe tout l'espace entre le track et le bas
        menu_y = 6
        menu_h = h - menu_y - shortcuts_h
        self._menu_visible_rows = self._visible_rows(menu_h, len(self.items))
        self._clamp_scroll()

        self.draw_menu_box(menu_y, 0, menu_h, w)
        if shortcuts_h:
            self.draw_shortcuts_box(h - shortcuts_h, 0, shortcuts_h, w)

    def _refresh_items(self):
        """Remplit self.items selon l'état courant (tri/filtre pour les tracks)."""
        if self.state == "main":
            self.items = ["Tracks", "Playlists", "Export", "Settings", "Quit"]
        elif self.state == "tracks":
            db_sort = "id" if self.track_sort == "random" else self.track_sort
            tracks = self.cmds.list_tracks(sort=db_sort)
            if self.track_sort == "random":
                tracks = self._shuffled(tracks)
            if self.track_filter:
                q = self.track_filter.lower()
                tracks = [
                    t for t in tracks
                    if q in t["title"].lower() or (t["artist"] and q in t["artist"].lower())
                ]
            self.items = tracks
        elif self.state == "playlists":
            self.items = self.cmds.list_playlists()
        elif self.state == "playlist_internal" and self.active_playlist:
            self.items = self.cmds.playlist_tracks(self.active_playlist["id"])
        elif self.state == "settings":
            music_folder = self.library.get_setting("music_folder") or "Not configured"
            export_folder = self.library.get_setting("export_folder") or "Not configured"
            sc = "ON" if self._shortcuts_visible() else "OFF"
            self.items = [
                f"Music Folder: {music_folder}",
                "Change Music Folder",
                f"Export Folder: {export_folder}",
                "Change Export Folder",
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

    def _shuffled(self, tracks):
        """Applique un ordre aléatoire STABLE d'une frame à l'autre (sinon la
        liste se remélangerait à chaque redraw, ~30x/s, et rendrait toute
        sélection impossible). Le mélange n'est régénéré que si la touche
        sort re-sélectionne "random" (_cycle_track_sort) ou si le set de
        tracks a changé (ajout/suppression) depuis le dernier mélange."""
        ids = {t["id"] for t in tracks}
        if not self._random_order or set(self._random_order) != ids:
            self._random_order = [t["id"] for t in tracks]
            random.shuffle(self._random_order)
        by_id = {t["id"]: t for t in tracks}
        return [by_id[i] for i in self._random_order]

    # ---------------- SCROLL ---------------- #

    @staticmethod
    def _visible_rows(box_h, n):
        """Nombre de lignes dispo pour les items dans une box de hauteur box_h.
        Si la liste ne tient pas, on réserve 1 ligne en haut + 1 en bas pour
        les indicateurs "...", de façon FIXE (indépendante du scroll actuel).
        C'est ce point qui garantit que le dessin et le clamp du scroll
        s'accordent toujours sur le même nombre de lignes."""
        interior = max(0, box_h - 2)
        reserved = 2 if n > interior else 0
        return max(1, interior - reserved)

    def _clamp_scroll(self):
        """Ajuste scroll_offset pour que selected_index reste dans la zone visible."""
        vis = self._menu_visible_rows
        if self.selected_index < self.scroll_offset:
            self.scroll_offset = self.selected_index
        elif self.selected_index >= self.scroll_offset + vis:
            self.scroll_offset = self.selected_index - vis + 1
        # Ne pas dépasser la fin
        max_offset = max(0, len(self.items) - vis)
        self.scroll_offset = max(0, min(self.scroll_offset, max_offset))

    # ---------------- BOXES ---------------- #

    def _safe_addstr(self, y, x, text, attr=0):
        try:
            self.stdscr.addstr(y, x, text, attr)
        except curses.error:
            pass

    def _menu_title(self):
        if self.state != "tracks":
            return "Menu"
        title = f"Tracks: {TRACK_SORT_LABELS[self.track_sort]}"
        if self.track_filter:
            title += f' | "{self.track_filter}"'
        return title

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

        vol = self.player.current_volume()
        vol_bar = "#" * (vol // 10) + "-" * (10 - vol // 10)
        pos_str = f"{int(pos)//60}:{int(pos)%60:02d} / {int(dur)//60}:{int(dur)%60:02d}"
        info_line = f"{pos_str}    Vol [{vol_bar}] {vol:3d}%"

        self._safe_addstr(y + 1, x + 2, track.stem if track else "No track playing")
        self._safe_addstr(y + 2, x + 2, bar)
        self._safe_addstr(y + 3, x + 2, info_line[:w - 4])

    def draw_menu_box(self, y, x, h, w):
        self.draw_box(y, x, h, w, self._menu_title())

        n = len(self.items)
        if n == 0:
            if self.state == "tracks" and self.track_filter:
                self._safe_addstr(y + 1, x + 2, f"No matches for '{self.track_filter}'")
            return

        # Nombre de lignes réellement dispo, déjà calculé dans
        # render_current_state() via _visible_rows() : c'est la même valeur
        # qu'a utilisée _clamp_scroll(), donc l'item sélectionné est
        # toujours dans la zone qu'on s'apprête à dessiner ici.
        items_rows = self._menu_visible_rows
        scrollable = n > items_rows
        draw_row = y + 1

        if scrollable:
            if self.scroll_offset > 0:
                self._safe_addstr(draw_row, x + 2, ". . .")
            draw_row += 1

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
            attr = curses.A_REVERSE if idx == self.selected_index else 0
            self._safe_addstr(draw_row, x + 2, name, attr)
            draw_row += 1

        if scrollable and (self.scroll_offset + items_rows) < n:
            self._safe_addstr(draw_row, x + 2, ". . .")

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
        if self.state == "tracks":
            menu_line = (
                f"ENTER-play | X-delete | D-download | E-edit | "
                f"{c('sort')}-sort | {c('search')}-search"
            )
        else:
            menu_line = {
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
        if self.state == "tracks":
            self.track_filter = ""
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
        if key not in (10, 13):
            return
        targets = ["tracks", "playlists", "export", "settings", None]
        target = targets[self.selected_index] if self.selected_index < len(targets) else None
        if target == "export":
            self._export_tracks()
        elif target:
            self.state = target
            self.selected_index = 0
            self.scroll_offset = 0
        else:
            self.running = False
            self.player.stop()

    def _export_tracks(self):
        """Copie tous les tracks de la bibliothèque vers le dossier d'export configuré."""
        target_root = self.library.get_setting("export_folder")
        if not target_root:
            self.prompt_input("No export folder set (see Settings). Press Enter to continue.")
            return
        tracks = self.cmds.list_tracks()
        if not tracks:
            self.prompt_input("No tracks to export. Press Enter to continue.")
            return
        dest = Path(target_root).expanduser()
        copied, failed = self.cmds.export_tracks(tracks, dest)
        msg = f"Exported {copied} track(s) to {dest}"
        if failed:
            msg += f" ({failed} failed)"
        self.prompt_input(f"{msg}. Press Enter to continue.")

    def handle_state_tracks(self, key):
        if key == self._ctrl("sort"):
            self._cycle_track_sort()
            return
        if key == self._ctrl("search"):
            self.track_filter = self.prompt_input("Search: ").strip()
            self.selected_index = 0
            self.scroll_offset = 0
            return

        # self.items est déjà la liste triée/filtrée de cette frame : on
        # l'utilise directement pour que l'action porte sur le morceau
        # réellement affiché et sélectionné à l'écran.
        tracks = self.items
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
        elif key in (ord("e"), ord("E")):
            self._edit_track(tracks[self.selected_index])

    def _edit_track(self, track):
        """Édite titre / artiste / cover d'un mp3 (laisser vide = ne pas changer)."""
        title = self.prompt_input(f"Title [{track['title']}]: ")
        artist = self.prompt_input(f"Artist [{track['artist'] or ''}]: ")
        cover = self.prompt_input("Cover image path (empty = keep current): ")

        updates = {}
        if title:
            updates["title"] = title
        if artist:
            updates["artist"] = artist
        if cover:
            cover_path = Path(cover).expanduser()
            if cover_path.exists():
                updates["cover"] = str(cover_path)
            else:
                self.prompt_input("Cover file not found, skipped. Press Enter to continue.")
        if updates:
            self.cmds.edit_track(track["id"], updates)

    def _cycle_track_sort(self):
        i = (TRACK_SORT_MODES.index(self.track_sort) + 1) % len(TRACK_SORT_MODES)
        self.track_sort = TRACK_SORT_MODES[i]
        if self.track_sort == "random":
            self._random_order = []  # force un nouveau mélange (vrai "shuffle")
        self.library.set_setting("track_sort", self.track_sort)
        self.selected_index = 0
        self.scroll_offset = 0

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
        elif self.selected_index == 3:
            folder = self.prompt_input("Export folder path: ")
            if folder:
                self.library.set_setting("export_folder", str(Path(folder).expanduser()))
        elif self.selected_index == 4:
            current = self.library.get_setting("show_shortcuts") or "on"
            self.library.set_setting("show_shortcuts", "off" if current == "on" else "on")
        elif self.selected_index == 5:
            self.state = "controls"
            self.selected_index = 0
            self.scroll_offset = 0
        elif self.selected_index == 6:
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
        self.stdscr.nodelay(False)  # lecture bloquante le temps de la saisie

        while True:
            h, w = self.stdscr.getmaxyx()
            self.stdscr.clear()
            self.draw_current_track_box(0, 0, 5, w)
            # Menu réduit pour laisser place à la zone de saisie (3 lignes,
            # de `by` à `by+2`). Il DOIT s'arrêter avant `by`, sinon le menu
            # écrit un item juste sous la zone de saisie et le prompt (plus
            # court) ne l'efface que partiellement — d'où du texte résiduel
            # qui "traîne" jusqu'à être recouvert par la frappe suivante.
            by = h - 4
            menu_h = max(1, by - 6)
            self._menu_visible_rows = self._visible_rows(menu_h, len(self.items))
            self._clamp_scroll()
            self.draw_menu_box(6, 0, menu_h, w)
            # Box de saisie en bas
            try:
                self.stdscr.addstr(by,     0, "+" + "-" * (w - 2) + "+")
                self.stdscr.addstr(by + 1, 0, "|")
                self.stdscr.addstr(by + 1, w - 1, "|")
                # ljust : efface tout résidu au lieu de laisser les colonnes
                # non écrites telles quelles (défense en profondeur).
                text = f" {prompt}{self.input_buffer}"[:w - 4].ljust(w - 4)
                self.stdscr.addstr(by + 1, 2, text)
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
                    return result
                elif key in ("\b", "\x7f"):
                    self.input_buffer = self.input_buffer[:-1]
                else:
                    self.input_buffer += key
            elif key == curses.KEY_BACKSPACE:
                self.input_buffer = self.input_buffer[:-1]

    def prompt_key(self, prompt: str):
        """Capture une seule touche pour le remapping. ESC annule."""
        self.stdscr.nodelay(False)  # lecture bloquante le temps de la capture
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
        return result

    # ---------------- END OF TRACK ---------------- #

    def handle_end_of_track(self):
        if self.player.state == "STOPPED" and self.player.current_index is not None:
            self.player.next()
