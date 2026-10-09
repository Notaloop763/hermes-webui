"""Hermes WebUI image: SQLite secure_delete for this interpreter.

python-build-standalone links its own SQLite into the interpreter without
SQLITE_SECURE_DELETE; the libsqlite3 built in the Dockerfile is never loaded
by it. The compiled default cannot be changed at runtime, so apply the
per-connection pragma instead."""


def _install():
    import functools
    import sqlite3
    import sqlite3.dbapi2 as dbapi2

    raw = dbapi2.connect
    if getattr(raw, "_hermes_secure_delete", False):
        return

    @functools.wraps(raw)
    def connect(*args, **kwargs):
        conn = raw(*args, **kwargs)
        try:
            conn.execute("PRAGMA secure_delete = ON")
        except BaseException:
            conn.close()
            raise
        return conn

    connect._hermes_secure_delete = True
    sqlite3.connect = dbapi2.connect = connect


_install()
del _install
