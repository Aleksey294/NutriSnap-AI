PRAGMA foreign_keys=ON;
CREATE TABLE IF NOT EXISTS users (
 telegram_id INTEGER PRIMARY KEY, created_at TEXT NOT NULL,
 target_kcal REAL NOT NULL, target_p REAL NOT NULL, target_f REAL NOT NULL, target_c REAL NOT NULL,
 fair_date TEXT NOT NULL DEFAULT '', fair_count INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS meals (
 id INTEGER PRIMARY KEY AUTOINCREMENT,
 user_id INTEGER NOT NULL REFERENCES users(telegram_id) ON DELETE CASCADE,
 eaten_at TEXT NOT NULL, day_key TEXT NOT NULL, source TEXT NOT NULL,
 payload TEXT NOT NULL, status TEXT NOT NULL CHECK(status IN ('draft','confirmed','cancelled')),
 card_id INTEGER, editing INTEGER NOT NULL DEFAULT 0, expires_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS meals_user_day ON meals(user_id, day_key, status);
CREATE INDEX IF NOT EXISTS meals_time ON meals(eaten_at);
CREATE TABLE IF NOT EXISTS dashboards (
 user_id INTEGER NOT NULL REFERENCES users(telegram_id) ON DELETE CASCADE,
 day_key TEXT NOT NULL, message_id INTEGER NOT NULL, PRIMARY KEY(user_id,day_key)
);
CREATE TABLE IF NOT EXISTS barcode_cache (
 barcode TEXT PRIMARY KEY, payload TEXT NOT NULL, fetched_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS weekly_reports (
 user_id INTEGER NOT NULL REFERENCES users(telegram_id) ON DELETE CASCADE,
 week_start TEXT NOT NULL, body TEXT NOT NULL, created_at TEXT NOT NULL,
 sent INTEGER NOT NULL DEFAULT 0, PRIMARY KEY(user_id,week_start)
);
CREATE TABLE IF NOT EXISTS api_usage (day TEXT PRIMARY KEY, count INTEGER NOT NULL);
CREATE TABLE IF NOT EXISTS meal_revisions (
 draft_id INTEGER PRIMARY KEY REFERENCES meals(id) ON DELETE CASCADE,
 original_id INTEGER NOT NULL REFERENCES meals(id) ON DELETE CASCADE
);
CREATE INDEX IF NOT EXISTS revisions_original ON meal_revisions(original_id);
PRAGMA user_version=2;
