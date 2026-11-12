#!/usr/bin/python3 -I
"""Transporte por ssh; no ejecuta Pueue en la máquina local."""
import argparse
import json
from pathlib import Path
import subprocess
import sys


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=["install", "withdraw", "parar", "arrancar"])
    parser.add_argument("--seco", action="store_true")
    args = parser.parse_args()
    root = Path(__file__).resolve().parent
    core = (root / "control.py").read_text().split('if __name__ == "__main__":')[0]
    files = {"control.py": (root / "control.py").read_text()}
    files.update({path.name: path.read_text() for path in sorted((root / "units").iterdir())})
    program = (core + "\nFILES = " + repr(files) + "\n" + (root / "remote.py").read_text()
               + f"\nremote({json.dumps(args.action)}, {args.seco!r})\n")
    result = subprocess.run(["ssh", "desk", "/usr/bin/python3", "-I", "-"], input=program, text=True)
    sys.exit(result.returncode)


if __name__ == "__main__":
    main()
