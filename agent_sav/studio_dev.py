import sys
from pathlib import Path

from watchfiles import PythonFilter, run_process


def serve(args: list[str]) -> None:
    from langgraph_cli.cli import cli

    cli(["dev", "--no-reload", *args])


if __name__ == "__main__":
    # The Uvicorn reloader can hang while restarting its Windows worker.
    run_process(
        Path(__file__).resolve().parent,
        target=serve,
        args=(sys.argv[1:],),
        watch_filter=PythonFilter(),
    )
