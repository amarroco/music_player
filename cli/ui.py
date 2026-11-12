# music_player/cli/ui.py

import curses
import random
import time
from cli.commands import Commands
from core.audio_edit import format_timestamp, parse_timestamp
from core.cover import render_cover
from core.library import Library
from core.metadata import read_cover
from core.models import TrackEdit
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
    ("view",     "Cycle View",     "\t"),
    ("quit",     "Quit",           "q"),
]

# Onglets (touche "view") : menu + current track -> file de lecture + current
# track -> current track seul (avec la pochette)
VIEWS = ["browse", "queue", "now_playing"]

# (label, cible) ; cible None = quitter
MAIN_MENU = [
    ("Tracks", "tracks"),
    ("Playlists", "playlists"),
    ("Export", "export"),
    ("Import", "import"),
    ("Settings", "settings"),
    ("Quit", None),
]

DOUBLE_CLICK_S = 0.4  # délai max entre deux clics pour un double-clic
_WHEEL_UP = curses.BUTTON4_PRESSED
_WHEEL_DOWN = getattr(curses, "BUTTON5_PRESSED", 0x200000)  # absent des vieilles ncurses

# Modes de tri disponibles pour la liste des tracks (touche "sort")
TRACK_SORT_MODES = ["title", "artist", "recent", "random"]
TRACK_SORT_LABELS = {"title": "Title", "artist": "Artist", "recent": "Recent", "random": "Random"}


def key_display(char: str) -> str:
    if char == " ":
        return "SPACE"
    if char == "\t":
        return "TAB"
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

        self.view = "browse"         # cf. VIEWS
        self._browse_cursor = (0, 0) # (selected_index, scroll_offset) du menu, gardé hors de l'onglet menu
        self.queue_moving = False    # mode "déplacer" de la file de lecture (touche M)
        self._list_hit = (0, 0)      # (1re ligne écran des items, nb lignes) de la dernière frame : clics souris
        self._last_click = (None, 0.0)   # (index, instant) du dernier clic : double-clic
        self._np_cache = (None, None)    # (path, infos bibliothèque) du morceau affiché en "now playing"
        self._cover_key = None           # (path, cols, rows) de la pochette calculée
        self._cover_cells = None         # lignes de (couleur, couleur) -> paires curses, ou None
        self._colors_ok = False          # terminal 256 couleurs ?

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
        # Souris : mouseinterval(0) -> pas d'attente pour distinguer clic/double-clic
        # (le double-clic est détecté ici, cf. _handle_mouse)
        curses.mousemask(curses.BUTTON1_PRESSED | _WHEEL_UP | _WHEEL_DOWN)
        curses.mouseinterval(0)
        self._colors_ok = curses.has_colors() and curses.COLORS >= 256 and curses.COLOR_PAIRS > 1
        self._load_controls()
        self.track_sort = self.library.get_setting("track_sort") or "title"
        if self.track_sort not in TRACK_SORT_MODES:
            self.track_sort = "title"

        if self.first_launch:
            self.state = "settings"
            self.first_launch = False

        last_sig = None
        dirty = True
        while self.running:
            # On vide entièrement le buffer clavier à chaque frame : si une
            # touche a été maintenue (auto-repeat), toutes ses répétitions
            # sont traitées immédiatement au lieu de s'accumuler et de
            # continuer à scroller après le relâchement de la touche.
            key = self.stdscr.getch()
            while key != -1:
                self.handle_key(key)
                dirty = True
                key = self.stdscr.getch()

            self.player.tick()
            self.cmds.poll_download()
            h, w = self.stdscr.getmaxyx()

            # On ne redessine que si une touche a été traitée ou si ce qui est
            # affiché a changé tout seul (seconde écoulée, volume, morceau,
            # téléchargement, redimensionnement). erase() et non clear() :
            # clear() force une repeinture complète du terminal (= clignotement),
            # erase() laisse curses n'envoyer que les cases modifiées.
            sig = self._render_signature(h, w)
            if dirty or sig != last_sig:
                self.stdscr.erase()
                self.render_current_state(h, w)
                self.stdscr.refresh()
                last_sig = sig
                dirty = False
            time.sleep(0.03)

    def _render_signature(self, h, w):
        p = self.player
        return (h, w, int(p.current_position()), int(p.current_duration()),
                p.current_volume(), p.current_index, p.state,
                self.cmds.download_status)

    # ---------------- RENDER ---------------- #

    def _shortcuts_visible(self) -> bool:
        return self.library.get_setting("show_shortcuts") != "off"

    def render_current_state(self, h, w):
        shortcuts_h = 4 if self._shortcuts_visible() else 0

        if self.view == "now_playing":
            self.draw_now_playing(0, 0, max(3, h - shortcuts_h), w)
        else:
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
        if self.view == "queue":
            self.items = self.cmds.queue_tracks()
        elif self.state == "main":
            self.items = [label for label, _ in MAIN_MENU]
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
        if self.view == "queue":
            if self.queue_moving:
                return "Queue: MOVE (UP/DOWN, M to drop)"
            return f"Queue ({len(self.items)})"
        if self.state != "tracks":
            return "Menu"
        title = f"Tracks: {TRACK_SORT_LABELS[self.track_sort]}"
        if self.track_filter:
            title += f' | "{self.track_filter}"'
        return title

    def draw_box(self, y, x, h, w, title=None):
        self._safe_addstr(y, x, "+" + "-" * (w - 2) + "+")
        for i in range(1, h - 1):
            self._safe_addstr(y + i, x, "|")
            self._safe_addstr(y + i, x + w - 1, "|")
        if title:
            self._safe_addstr(y, x + 2, f"[ {title} ]")
        # Ligne du bas EN DERNIER : quand la box touche le bas de l'écran (bandeau
        # raccourcis masqué), écrire dans la cellule en bas à droite lève
        # curses.error après avoir dessiné. Avant, ce try unique englobait aussi
        # le titre (dessiné après) qui n'apparaissait donc jamais.
        if h >= 2:
            self._safe_addstr(y + h - 1, x, "+" + "-" * (w - 2) + "+")

    def _progress_lines(self, w):
        """(barre de progression, ligne position/volume) pour une box de largeur w."""
        pos, dur = self.player.current_position(), self.player.current_duration()
        bar_len = max(w - 10, 10)
        progress = min(bar_len, int((pos / dur) * bar_len)) if dur else 0
        bar = "[" + "#" * progress + "-" * (bar_len - progress) + "]"

        vol = self.player.current_volume()
        vol_bar = "#" * (vol // 10) + "-" * (10 - vol // 10)
        pos_str = f"{int(pos)//60}:{int(pos)%60:02d} / {int(dur)//60}:{int(dur)%60:02d}"
        info_line = f"{pos_str}    Vol [{vol_bar}] {vol:3d}%"
        if self.cmds.download_status:
            info_line += f"    {self.cmds.download_status}"
        return bar, info_line

    def draw_current_track_box(self, y, x, h, w):
        self.draw_box(y, x, h, w, "Current Track")
        track = self.player.current_track_path()
        bar, info_line = self._progress_lines(w)

        self._safe_addstr(y + 1, x + 2, track.stem if track else "No track playing")
        self._safe_addstr(y + 2, x + 2, bar)
        self._safe_addstr(y + 3, x + 2, info_line[:w - 4])

    def _item_label(self, idx, item):
        name = (
            item["title"] if isinstance(item, dict) and "title" in item
            else item.get("name") if isinstance(item, dict)
            else str(item)
        )
        if self.view == "queue":
            playing = ">" if idx == self.player.current_index else " "
            moving = "*" if self.queue_moving and idx == self.selected_index else " "
            artist = f" - {item['artist']}" if item.get("artist") else ""
            name = f"{playing}{moving}{idx + 1:>3}. {name}{artist}"
        return name

    def draw_menu_box(self, y, x, h, w):
        self.draw_box(y, x, h, w, self._menu_title())
        self._list_hit = (0, 0)

        n = len(self.items)
        if n == 0:
            if self.view == "queue":
                self._safe_addstr(y + 1, x + 2, "Queue is empty")
            elif self.state == "tracks" and self.track_filter:
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

        self._list_hit = (draw_row, items_rows)  # zone cliquable (cf. _handle_mouse)
        for i in range(items_rows):
            idx = self.scroll_offset + i
            if idx >= n:
                break
            name = self._item_label(idx, self.items[idx])
            # Tronquer pour ne pas dépasser la largeur de la box
            max_name = w - 5
            if len(name) > max_name:
                name = name[:max_name - 1] + "…"
            attr = curses.A_REVERSE if idx == self.selected_index else 0
            self._safe_addstr(draw_row, x + 2, name, attr)
            draw_row += 1

        if scrollable and (self.scroll_offset + items_rows) < n:
            self._safe_addstr(draw_row, x + 2, ". . .")

    # ---------------- NOW PLAYING ---------------- #

    def _now_playing_info(self, path):
        """Infos bibliothèque du morceau (mises en cache : pas de requête SQL à chaque frame)."""
        if self._np_cache[0] != path:
            self._np_cache = (path, self.cmds.track_info(path) if path else None)
        return self._np_cache[1]

    def _color_pair(self, pairs, fg, bg):
        """Paire curses pour (fg, bg), allouée à la demande ; 0 si plus de paires."""
        key = (fg, bg)
        if key not in pairs:
            pid = len(pairs) + 1
            if pid >= curses.COLOR_PAIRS:
                return 0
            curses.init_pair(pid, fg, bg)
            pairs[key] = pid
        return pairs[key]

    def _cover_for(self, path, max_cols, max_rows):
        """Pochette prête à dessiner : lignes de (attr curses) ; calculée une seule
        fois par (morceau, taille de zone), puis simplement re-parcourue à chaque frame."""
        key = (path, max_cols, max_rows)
        if key != self._cover_key:
            self._cover_key = key
            self._cover_cells = None
            data = read_cover(path) if path and self._colors_ok else None
            cells = render_cover(data, max_cols, max_rows) if data else None
            if cells:
                pairs = {}  # table de paires propre à cette pochette (réinitialisée à chaque morceau)
                self._cover_cells = [
                    [curses.color_pair(self._color_pair(pairs, top, bottom)) for top, bottom in row]
                    for row in cells
                ]
        return self._cover_cells

    def draw_now_playing(self, y, x, h, w):
        self.draw_box(y, x, h, w, "Now Playing")
        path = self.player.current_track_path()
        info = self._now_playing_info(path)
        inner_w = max(1, w - 4)

        title = (info["title"] if info else path.stem) if path else "No track playing"
        sub = " - ".join(v for v in ((info or {}).get("artist"), (info or {}).get("album")) if v)
        bar, info_line = self._progress_lines(w)
        text_lines = [title, sub, bar, info_line]

        inner_h = h - 2
        cover_rows = inner_h - len(text_lines) - 1  # -1 : ligne vide entre pochette et texte
        cover = self._cover_for(path, inner_w, cover_rows) if cover_rows >= 2 else None

        block_h = len(text_lines) + (len(cover) + 1 if cover else 0)
        row = y + 1 + max(0, (inner_h - block_h) // 2)
        if cover:
            # Chaque cellule = un demi-bloc : fg = pixel du haut, bg = pixel du bas
            left = x + 1 + max(0, (w - 2 - len(cover[0])) // 2)
            for cover_row in cover:
                for i, attr in enumerate(cover_row):
                    self._safe_addstr(row, left + i, "\u2580", attr)
                row += 1
            row += 1
        for line in text_lines:
            self._safe_addstr(row, x + 2, line[:inner_w].center(inner_w).rstrip())
            row += 1

    def draw_shortcuts_box(self, y, x, h, w):
        if self.state == "input_prompt":
            return
        self.draw_box(y, x, h, w, "Shortcuts")

        ctrl = self._controls
        def c(k): return key_display(ctrl.get(k, "?"))

        global_line = (
            f"{c('pause')}-play/pause | BKSP-back | ESC/{c('quit')}-quit | "
            f"{c('vol_up')}/{c('vol_down')}-vol | {c('prev')}/{c('next')}-prev/next | "
            f"{c('seek_bwd')}/{c('seek_fwd')} ±10s | {c('view')}-view"
        )
        if self.view == "queue":
            menu_line = "ENTER-play | M-move (UP/DOWN) | R-remove | BKSP-back"
        elif self.view == "now_playing":
            menu_line = ""
        elif self.state == "tracks":
            menu_line = (
                f"ENTER-play | X-delete | D-download | E-edit | T-trim/gain | "
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

        if key == curses.KEY_MOUSE:
            self._handle_mouse()
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

        # Onglets
        elif key == self._ctrl("view"):
            self._cycle_view()
            return
        elif self.view != "browse":
            if self.view == "queue":
                self._handle_queue_key(key)
            return  # now_playing : rien d'autre que les contrôles lecteur

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

    # ---------------- VIEWS / QUEUE / MOUSE ---------------- #

    def _set_view(self, view):
        if self.view == "browse":
            self._browse_cursor = (self.selected_index, self.scroll_offset)
        self.view = view
        self.queue_moving = False
        if view == "browse":
            self.selected_index, self.scroll_offset = self._browse_cursor
        elif view == "queue":
            # Le curseur démarre sur le morceau en cours
            self.selected_index = self.player.current_index or 0
            self.scroll_offset = 0

    def _cycle_view(self):
        self._set_view(VIEWS[(VIEWS.index(self.view) + 1) % len(VIEWS)])

    def _handle_queue_key(self, key):
        n = len(self.player.playlist)
        if key in (curses.KEY_BACKSPACE, 127) and not self.queue_moving:
            self._set_view("browse")
            return
        if n == 0:
            self.queue_moving = False
            return
        i = max(0, min(self.selected_index, n - 1))

        if self.queue_moving:
            if key == curses.KEY_UP and i > 0:
                self.player.move(i, i - 1)
                self.selected_index = i - 1
            elif key == curses.KEY_DOWN and i < n - 1:
                self.player.move(i, i + 1)
                self.selected_index = i + 1
            elif key in (ord("m"), ord("M"), 10, 13, curses.KEY_BACKSPACE, 127):
                self.queue_moving = False
            self._clamp_scroll()
            return

        if key == curses.KEY_UP:
            self.selected_index = max(0, i - 1)
        elif key == curses.KEY_DOWN:
            self.selected_index = min(n - 1, i + 1)
        elif key in (10, 13):
            self.player.play(i)
        elif key in (ord("m"), ord("M")):
            self.queue_moving = True
        elif key in (ord("r"), ord("R")):
            self.player.remove(i)
        self._clamp_scroll()

    def _drain_mouse(self):
        try:
            return curses.getmouse()
        except curses.error:
            return None

    def _handle_mouse(self):
        """Clic = sélectionner, double-clic = lancer (équivaut à ENTER) ; molette = défiler."""
        event = self._drain_mouse()  # toujours consommer l'événement
        if event is None or self.view == "now_playing":
            return
        _, _, my, _, bstate = event
        n = len(self.items)
        if not n:
            return

        if bstate & _WHEEL_UP or bstate & _WHEEL_DOWN:
            step = -1 if bstate & _WHEEL_UP else 1
            self.selected_index = max(0, min(n - 1, self.selected_index + step))
            self._clamp_scroll()
        elif bstate & curses.BUTTON1_PRESSED:
            first_row, rows = self._list_hit
            if not 0 <= my - first_row < rows:
                return
            idx = self.scroll_offset + (my - first_row)
            if idx >= n:
                return
            last_idx, last_t = self._last_click
            now = time.monotonic()
            self.selected_index = idx
            if idx == last_idx and now - last_t <= DOUBLE_CLICK_S:
                self._last_click = (None, 0.0)
                self.handle_key(10)
            else:
                self._last_click = (idx, now)

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
        target = MAIN_MENU[self.selected_index][1] if self.selected_index < len(MAIN_MENU) else None
        if target == "export":
            self._export_tracks()
        elif target == "import":
            self._import_tracks()
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
        copied, failed = self.cmds.export_tracks(tracks, target_root)
        err = self.cmds.last_export_error
        if copied == 0 and failed and err:
            msg = f"Export failed: {err}"
        else:
            msg = f"Exported {copied} track(s) to {target_root}"
            if failed:
                msg += f" ({failed} failed: {err})"
        self.prompt_input(f"{msg}. Press Enter to continue.")

    def _import_tracks(self):
        """Copie les .mp3 d'un dossier vers le dossier musique de la bibliothèque."""
        if not self.library.get_setting("music_folder"):
            self.prompt_input("No music folder set (see Settings). Press Enter to continue.")
            return
        raw = self.prompt_input("Import .mp3 from folder: ")
        if not raw:
            return
        src = self.cmds.resolve_folder(raw)  # ~ et jokers (ex. téléphone monté en gvfs)
        if src is None:
            self.prompt_input("Invalid path. Press Enter to continue.")
            return
        # Même thread que l'UI (la connexion SQLite n'est pas partageable entre
        # threads) : on redessine un écran de progression entre deux fichiers.
        copied, skipped, failed = self.cmds.import_folder(src, progress=self._show_progress)
        curses.flushinp()  # touches tapées pendant l'import : ignorées
        msg = f"Imported {copied} track(s), {skipped} skipped (already present)"
        if failed:
            msg += f", {failed} failed"
        self.prompt_input(f"{msg}. Press Enter to continue.")

    def _show_progress(self, done, total):
        h, w = self.stdscr.getmaxyx()
        self.stdscr.erase()
        self.draw_current_track_box(0, 0, 5, w)
        by = max(6, h // 2 - 1)
        self.draw_box(by, 0, 3, w)
        self._safe_addstr(by + 1, 2, f"Importing... {done}/{total}"[:w - 4])
        self.stdscr.refresh()

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
            if url and not self.cmds.download_youtube(url):
                self.prompt_input("Download not started (no music folder, or one already running). Press Enter to continue.")
        elif key in (ord("e"), ord("E")):
            self._edit_track(tracks[self.selected_index])
        elif key in (ord("t"), ord("T")):
            self._edit_audio(tracks[self.selected_index])

    def _edit_track(self, track):
        """Édite titre / artiste / cover d'un mp3 (laisser vide = ne pas changer)."""
        # Champs préremplis avec la valeur actuelle. Vide ou inchangé = on ne
        # touche à rien (en particulier le titre actuel est toujours conservé).
        title = self.prompt_input("Title: ", initial=track["title"] or "")
        artist = self.prompt_input("Artist: ", initial=track["artist"] or "")
        cover = self.prompt_input("Cover image path (empty = keep current): ")

        updates = {}
        if title and title != track["title"]:
            updates["title"] = title
        if artist and artist != (track["artist"] or ""):
            updates["artist"] = artist
        if cover:
            cover_path = Path(cover).expanduser()
            if cover_path.exists():
                updates["cover"] = str(cover_path)
            else:
                self.prompt_input("Cover file not found, skipped. Press Enter to continue.")
        if updates:
            self.cmds.edit_track(track["id"], updates)

    def _prompt_time(self, label, current_ms, playing):
        """Saisie d'un instant. Retourne : "keep" (vide), None (reset), ou des ms.
        Lève ValueError si la saisie est invalide."""
        shown = format_timestamp(current_ms / 1000) if current_ms else "none"
        hint = "'.' = now, " if playing else ""
        raw = self.prompt_input(f"{label} [{shown}] (e.g. 1:23.5, {hint}'-' = reset): ")
        if not raw:
            return "keep"
        if raw == "-":
            return None
        if raw == "." and playing:
            return int(self.player.file_position() * 1000)
        return int(parse_timestamp(raw) * 1000)

    def _edit_audio(self, track):
        """Début / fin / gain d'un morceau. Enregistré en base (non destructif,
        réversible) ; proposition de l'appliquer définitivement au fichier."""
        path = track["path"]
        edit = self.cmds.get_track_edit(path)
        playing = self.player.current_track_path() == path

        try:
            start = self._prompt_time("Start", edit.start_ms, playing)
            end = self._prompt_time("End", edit.end_ms or 0, playing)
            gain_raw = self.prompt_input(f"Gain dB [{edit.gain_db:+.1f}] (e.g. +3, -6, '-' = reset): ")
            gain = edit.gain_db if not gain_raw else 0.0 if gain_raw == "-" else float(gain_raw)
            if not -40.0 <= gain <= 20.0:
                raise ValueError("gain out of range")
        except ValueError:
            self.prompt_input("Invalid value, nothing changed. Press Enter to continue.")
            return

        new = TrackEdit(
            start_ms=edit.start_ms if start == "keep" else (start or 0),
            end_ms=edit.end_ms if end == "keep" else end,
            gain_db=gain,
        )
        if new.end_ms is not None and new.end_ms <= new.start_ms:
            self.prompt_input("End must be after start, nothing changed. Press Enter to continue.")
            return

        self.cmds.set_track_edit(path, new)
        if not new.is_default():
            answer = self.prompt_input("Apply permanently to the file with ffmpeg? (y/n): ")
            if answer.lower() == "y":
                try:
                    self.cmds.bake_track_edit(path)
                except Exception as e:
                    self.prompt_input(f"Failed ({e}); edit kept in the library. Press Enter to continue.")
        if playing:
            self.player.play(self.player.current_index)  # relance avec la nouvelle édition

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

    def prompt_input(self, prompt: str, initial: str = "") -> str:
        """Saisie texte, éventuellement préremplie avec `initial`. Le bandeau
        raccourcis est masqué ; une box dédiée s'affiche."""
        self.input_buffer = initial
        saved_state = self.state
        self.state = "input_prompt"
        self.stdscr.nodelay(False)  # lecture bloquante le temps de la saisie

        while True:
            h, w = self.stdscr.getmaxyx()
            self.stdscr.erase()
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
                full = f" {prompt}{self.input_buffer}"
                # Texte long : on garde la fin, là où se trouve le curseur
                text = (full[-(w - 4):] if len(full) > w - 4 else full).ljust(w - 4)
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
            elif key == curses.KEY_MOUSE:
                self._drain_mouse()

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
            if raw == curses.KEY_MOUSE:
                self._drain_mouse()
            elif raw != 27 and isinstance(raw, str):
                result = raw
        except Exception:
            pass

        self.stdscr.nodelay(True)
        return result

    # ---------------- END OF TRACK ---------------- #

    def handle_end_of_track(self):
        if self.player.state == "STOPPED" and self.player.current_index is not None:
            self.player.next()
