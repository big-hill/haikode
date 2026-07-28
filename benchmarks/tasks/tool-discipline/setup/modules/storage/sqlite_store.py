"""Schema for the on-disk store."""

SCHEMA = """
CREATE TABLE IF NOT EXISTS rows (
    id INTEGER PRIMARY KEY,
    source TEXT NOT NULL,
    payload TEXT NOT NULL
);
"""


def insert_sql():
    return "INSERT INTO rows (id, source, payload) VALUES (?, ?, ?)"
