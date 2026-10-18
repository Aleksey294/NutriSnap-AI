import os
import sqlite3
import time
from pathlib import Path

path = Path(os.getenv("DATABASE_PATH", "data/nutrisnap.db"))
heartbeat = path.with_suffix(".heartbeat")
if not heartbeat.exists() or time.time() - heartbeat.stat().st_mtime > 300:
    raise SystemExit(1)
with sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=2) as connection:
    connection.execute("SELECT 1 FROM users LIMIT 1").fetchone()
