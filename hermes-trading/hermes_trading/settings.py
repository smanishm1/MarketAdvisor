"""Runtime settings shared by the worker and dashboard.

Stored in the DB `meta` table so the dashboard can change them live and the
worker picks them up on its next tick. Resolution order: DB value -> env -> default.
"""
from __future__ import annotations

import os
import sqlite3

from . import db

_TRUE = ("1", "true", "yes", "on")


def _resolve(conn: sqlite3.Connection, key: str, env: str, default: str) -> str:
    val = db.get_meta(conn, f"setting.{key}", None)
    if val is not None:
        return val
    return os.environ.get(env, default)


def auto_reflect(conn: sqlite3.Connection) -> bool:
    return _resolve(conn, "auto_reflect", "HERMES_AUTO_REFLECT", "true").strip().lower() in _TRUE


def reflect_mode(conn: sqlite3.Connection) -> str:
    mode = _resolve(conn, "reflect_mode", "HERMES_REFLECT_MODE", "hermes").strip().lower()
    return mode if mode in ("hermes", "fallback") else "hermes"


def fast_exits(conn: sqlite3.Connection) -> bool:
    # "exit fast, add slow" — check rotation exits every tick; buys stay weekly. Default on.
    return _resolve(conn, "fast_exits", "HERMES_FAST_EXITS", "true").strip().lower() in _TRUE


def set_fast_exits(conn: sqlite3.Connection, on: bool) -> None:
    db.set_meta(conn, "setting.fast_exits", "true" if on else "false")


def set_auto_reflect(conn: sqlite3.Connection, on: bool) -> None:
    db.set_meta(conn, "setting.auto_reflect", "true" if on else "false")


def set_reflect_mode(conn: sqlite3.Connection, mode: str) -> None:
    mode = (mode or "").strip().lower()
    if mode not in ("hermes", "fallback"):
        mode = "hermes"
    db.set_meta(conn, "setting.reflect_mode", mode)
