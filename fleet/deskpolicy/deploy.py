"""Prepara el retiro desde la MacBook; --seco sólo imprime, sin conectar."""
import argparse
from pathlib import Path
import subprocess
import sys


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--seco", action="store_true")
    parser.add_argument("--esperar", type=int, default=0, metavar="N",
                        help="Esperar hasta N segundos a las corridas admitidas")
    args = parser.parse_args()
    if args.esperar < 0:
        parser.error("--esperar debe ser no negativo")
    source = Path(__file__).resolve().parent
    program = ((source / "remote.py").read_text()
               + "\nsys.exit(retire(" + repr((source / "cli.py").read_bytes())
               + ", wait=" + str(args.esperar) + "))\n")
    if args.seco:
        print("# ssh desk /usr/bin/python3 -I -\n" + program, end="")
        return 0
    return subprocess.run(
        ["ssh", "desk", "/usr/bin/python3", "-I", "-"], input=program, text=True
    ).returncode


if __name__ == "__main__":
    sys.exit(main())
