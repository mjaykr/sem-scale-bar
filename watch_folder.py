"""Continuously process stable files arriving in a microscope export folder."""

from __future__ import annotations

import argparse
from pathlib import Path
import time

from cli import discover_inputs, main as run_batch


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="Watch an SEM export folder for new images.")
    parser.add_argument("input", type=Path)
    parser.add_argument("-o", "--output", type=Path, default=Path("publication_ready"))
    parser.add_argument("--config", type=Path)
    parser.add_argument("--interval", type=float, default=5.0)
    parser.add_argument("--recursive", action="store_true")
    parser.add_argument("--once", action="store_true", help="Run one immediate scan and exit")
    args = parser.parse_args(argv)
    if not args.input.is_dir():
        parser.error("input must be an existing directory")
    if args.interval < 1:
        parser.error("interval must be at least one second")

    observed: dict[Path, tuple[int, int]] = {}
    print(f"Watching {args.input.resolve()} — press Ctrl+C to stop")
    try:
        while True:
            files, _ = discover_inputs([str(args.input)], args.recursive)
            current = {path: (path.stat().st_size, path.stat().st_mtime_ns) for path in files}
            stable = files if args.once else [path for path in files if observed.get(path) == current[path]]
            if stable:
                command = [str(path) for path in stable] + ["--output", str(args.output)]
                if args.config:
                    command += ["--config", str(args.config)]
                run_batch(command)
            if args.once:
                return 0
            observed = current
            time.sleep(args.interval)
    except KeyboardInterrupt:
        print("Watch stopped.")
        return 0


if __name__ == "__main__":
    raise SystemExit(main())
