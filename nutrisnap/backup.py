"""Consistent SQLite snapshot, including committed data in the WAL."""

import argparse
import os
import sqlite3
from pathlib import Path


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("destination", type=Path)
    args = parser.parse_args()
    source = Path(os.getenv("DATABASE_PATH", "data/nutrisnap.db"))
    if args.destination.exists():
        parser.error("Файл назначения уже существует")
    args.destination.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(f"file:{source}?mode=ro", uri=True) as src, sqlite3.connect(args.destination) as dst:
        src.backup(dst)
    print(f"Резервная копия: {args.destination}")


if __name__ == "__main__":
    main()
