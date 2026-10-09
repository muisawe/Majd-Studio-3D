"""SQLite helper that commits and closes on `with`, so Windows can delete temp databases."""

import sqlite3

from majd_studio_3d.store import _ClosingConnection


def connect(path):
    return sqlite3.connect(path, factory=_ClosingConnection)
