# music_player/main.py

from core.library import Library
from core.player import Player
from cli.commands import Commands
from cli.ui import UI

def main():
    library = Library()
    player = Player()
    cmds = Commands(library, player)
    ui = UI(library, player, cmds)
    ui.start()

if __name__ == "__main__":
    main()
