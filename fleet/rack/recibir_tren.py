#!/usr/bin/env python3
"""Recibe un único tren-host@1 por stdin para el rack."""
import argparse
import importlib.util
import json
from pathlib import Path
import sys
import tempfile
import time

# El comando forzado usa Python -I, que omite el directorio del script.
_spec = importlib.util.spec_from_file_location('rack_tren', Path(__file__).resolve().with_name('tren.py'))
tren = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(tren)


def receive(stream, config, state_dir):
    try:
        raw = stream.read(tren.MAX_DOCUMENT + 1)
        if len(raw) > tren.MAX_DOCUMENT:
            return 65
        now = time.time()
        data = tren.valid_tren(tren.decode(raw), tren.load_repos(config), now)
        data['received_at'] = now
        body = json.dumps(data, ensure_ascii=False, allow_nan=False, separators=(',', ':')).encode('utf-8')
    except (OSError, ValueError, RecursionError):
        return 65

    try:
        state_dir.mkdir(parents=True, exist_ok=True)
        with tempfile.NamedTemporaryFile(dir=state_dir, prefix='.tren-', delete=False) as temporary:
            path = Path(temporary.name)
            try:
                temporary.write(body)
                temporary.close()
                path.replace(state_dir / 'tren.json')
            finally:
                path.unlink(missing_ok=True)
    except OSError:
        return 75
    return 0


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--config', type=Path, default=tren.DEFAULT_CONFIG)
    parser.add_argument('--state-dir', type=Path, default=Path.home() / '.local/state/pao/rack')
    args = parser.parse_args()
    return receive(sys.stdin.buffer, args.config, args.state_dir)


if __name__ == '__main__':
    sys.exit(main())
