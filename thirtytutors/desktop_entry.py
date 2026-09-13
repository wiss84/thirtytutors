"""Entry point for the packaged Windows desktop build (PyInstaller), as
opposed to `cli.py:main` which is the entry point for the pip/developer
install (`thirtytutors` on PATH).

This file exists so the packaged build never touches cli.py at all: that
module's job is bootstrapping a developer/source environment (installing
extra pip packages, printing setup progress to a terminal, creating its
own desktop shortcut) - none of which applies to a packaged install, where
the installer already downloaded everything and already created the
shortcut, and there is no terminal to print to.

Asset presence/freshness is NOT re-checked here on purpose: the update
bell (static/UI/scripts/update.js) is loaded unconditionally on every
page and already polls /api/update-status, offering a visible
download-with-progress flow if anything is missing or outdated. Adding a
second, separate check here would just be a duplicate of that.
PyInstaller runs this file standalone (not as part of the `thirtytutors`
package), so a relative import (`from . import desktop`) fails at
runtime with "attempted relative import with no known parent package" -
use the absolute import below instead.

--host/--port are parsed (rather than always using desktop.run()'s own
defaults) so updater.relaunch_app() can hand this a fresh, OS-assigned
port for the Update & Relaunch flow - without this, a relaunched packaged
build always rebound the default port 8000 regardless of what the
outgoing process was still holding, racing its shutdown instead of
sidestepping it the way relaunch_app() intends.
"""

import argparse

from thirtytutors import desktop


def main() -> None:
    parser = argparse.ArgumentParser(description="ThirtyTutors desktop app (packaged build)")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8000)
    args = parser.parse_args()
    desktop.run(host=args.host, port=args.port)


if __name__ == "__main__":
    main()
