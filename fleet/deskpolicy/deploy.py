"""Prepara el retiro desde la MacBook; --seco sólo imprime, sin conectar."""
import argparse
from pathlib import Path
import subprocess
import sys


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--seco", action="store_true")
    args = parser.parse_args()
    source = Path(__file__).resolve().parent
    program = ((source / "remote.py").read_text()
               + "\nretire(" + repr((source / "cli.py").read_bytes()) + ")\n")
    if args.seco:
        print("# ssh desk /usr/bin/python3 -I -\n" + program, end="")
        return 0
    return subprocess.run(
        ["ssh", "desk", "/usr/bin/python3", "-I", "-"], input=program, text=True
    ).returncode


if __name__ == "__main__":
    sys.exit(main())
