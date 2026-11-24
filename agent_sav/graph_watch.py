"""Hot reload of the graph explorer: follows the code and the page while they are edited.

The website keeps the modules it started with, so the map of changed code is made by a fresh process. Only for
the shop of this machine: the public demo never reads its files again.
"""

import hashlib
import json
import subprocess
import sys
from pathlib import Path
from threading import Lock

ROOT = Path(__file__).resolve().parent.parent
TIMEOUT = 60


class Unreadable(Exception):
    """The code cannot be mapped as it is, in the middle of an edit for instance."""


def stamp(paths: list[Path]) -> str:
    seen = hashlib.sha256()
    for path in sorted(paths):
        try:
            state = path.stat()
        except FileNotFoundError:  # being replaced by an editor
            continue
        seen.update(f"{path.name}:{state.st_mtime_ns}:{state.st_size};".encode())
    return seen.hexdigest()[:16]


class Watch:
    def __init__(self, root: Path = ROOT):
        self.root = root
        self.lock = Lock()
        self.made: tuple[str, dict | str] | None = None  # the code it was made from, and the map or the error

    def version(self) -> dict[str, str]:
        """Changes with the code, the styles, or the page itself."""
        interface = self.root / "interface"
        return {
            "code": stamp([*(self.root / "agent_sav").glob("*.py"), self.root / "agent_sav" / "graph_examples.json"]),
            "style": stamp([interface / "graphes.css", interface / "theme.css"]),
            "page": stamp([interface / "graphes.html", interface / "graphes.js"]),
        }

    def graphs(self) -> dict:
        """The map of the code as it is now."""
        with self.lock:
            code = self.version()["code"]
            if self.made is None or self.made[0] != code:
                self.made = code, self.make()
            made = self.made[1]
        if isinstance(made, str):
            raise Unreadable(made)
        return made

    def make(self) -> dict | str:
        try:
            done = subprocess.run([sys.executable, "-X", "utf8", "-m", "agent_sav.graph_map"], cwd=self.root,
                                  capture_output=True, encoding="utf-8", timeout=TIMEOUT, check=False)
        except subprocess.TimeoutExpired:
            return f"Pas de réponse en {TIMEOUT} s."
        if done.returncode:
            return "\n".join(done.stderr.strip().splitlines()[-4:])
        return json.loads(done.stdout)
