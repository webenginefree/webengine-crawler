# -*- coding: utf-8 -*-
"""Stockage SQLite : comptes, jobs de crawl, tentatives de connexion.

Tout l'etat partage vit ici plutot qu'en memoire du process web : l'interface
survit a un redemarrage, peut tourner sur plusieurs workers gunicorn, et les
crawls s'executent dans des process separes qui ecrivent leur avancement ici.
"""
from __future__ import annotations

import json
import os
import sqlite3
import time

from werkzeug.security import check_password_hash, generate_password_hash

SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    username      TEXT UNIQUE NOT NULL,
    password_hash TEXT NOT NULL,
    role          TEXT NOT NULL DEFAULT 'user',   -- 'admin' ou 'user'
    active        INTEGER NOT NULL DEFAULT 1,
    max_pages     INTEGER NOT NULL DEFAULT 1000,  -- plafond par crawl
    max_parallel  INTEGER NOT NULL DEFAULT 1,     -- crawls simultanes
    created_at    REAL NOT NULL,
    last_login    REAL
);
CREATE TABLE IF NOT EXISTS jobs (
    id          TEXT PRIMARY KEY,
    user_id     INTEGER NOT NULL,
    url         TEXT NOT NULL,
    state       TEXT NOT NULL,                    -- queued|running|done|error|cancelled
    pct         REAL NOT NULL DEFAULT 0,
    message     TEXT NOT NULL DEFAULT '',
    crawled     INTEGER NOT NULL DEFAULT 0,
    params      TEXT NOT NULL DEFAULT '{}',
    gsc_path    TEXT,
    report      TEXT,
    csv_dir     TEXT,
    pid         INTEGER,
    cancel      INTEGER NOT NULL DEFAULT 0,
    created_at  REAL NOT NULL,
    finished_at REAL,
    FOREIGN KEY (user_id) REFERENCES users(id)
);
CREATE INDEX IF NOT EXISTS idx_jobs_user ON jobs(user_id, created_at DESC);
CREATE TABLE IF NOT EXISTS index_snapshots (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id     INTEGER NOT NULL,
    site        TEXT NOT NULL,
    imported_at REAL NOT NULL,
    date_gsc    TEXT,
    indexees    INTEGER,
    non_indexees INTEGER,
    resume      TEXT NOT NULL DEFAULT '{}',
    motifs      TEXT NOT NULL DEFAULT '[]',
    alertes     TEXT NOT NULL DEFAULT '[]',
    courbe      TEXT NOT NULL DEFAULT '[]',
    urls        TEXT NOT NULL DEFAULT '[]'
);
CREATE INDEX IF NOT EXISTS idx_snap_user ON index_snapshots(user_id, site, imported_at DESC);
CREATE TABLE IF NOT EXISTS login_attempts (
    ip     TEXT PRIMARY KEY,
    fails  INTEGER NOT NULL DEFAULT 0,
    first  REAL NOT NULL
);
"""

_PATH = None


def path():
    global _PATH
    if _PATH is None:
        out = os.environ.get("WEBENGINE_OUT", "webengine-rapports")
        os.makedirs(out, exist_ok=True)
        _PATH = os.path.join(out, "webengine.db")
    return _PATH


def connect():
    con = sqlite3.connect(path(), timeout=15)
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA journal_mode=WAL")
    con.execute("PRAGMA busy_timeout=10000")
    return con


def init():
    con = connect()
    with con:
        con.executescript(SCHEMA)
        cols = {r["name"] for r in con.execute("PRAGMA table_info(jobs)")}
        if "crawl_file" not in cols:      # migration des bases existantes
            con.execute("ALTER TABLE jobs ADD COLUMN crawl_file TEXT")
    con.close()


# --------------------------------------------------------------------- users
def create_user(username, password, role="user", max_pages=1000, max_parallel=1):
    username = (username or "").strip()
    if not username:
        raise ValueError("Nom d'utilisateur vide.")
    if len(password or "") < 10:
        raise ValueError("Mot de passe trop court (10 caracteres minimum).")
    con = connect()
    try:
        with con:
            con.execute(
                "INSERT INTO users (username, password_hash, role, active, max_pages,"
                " max_parallel, created_at) VALUES (?,?,?,1,?,?,?)",
                (username, generate_password_hash(password), role, max_pages,
                 max_parallel, time.time()))
    except sqlite3.IntegrityError:
        raise ValueError("Ce nom d'utilisateur existe deja.")
    finally:
        con.close()


def set_password(user_id, password):
    if len(password or "") < 10:
        raise ValueError("Mot de passe trop court (10 caracteres minimum).")
    con = connect()
    with con:
        con.execute("UPDATE users SET password_hash=? WHERE id=?",
                    (generate_password_hash(password), user_id))
    con.close()


def update_user(user_id, **fields):
    allowed = {"role", "active", "max_pages", "max_parallel"}
    sets = {k: v for k, v in fields.items() if k in allowed}
    if not sets:
        return
    con = connect()
    with con:
        con.execute("UPDATE users SET %s WHERE id=?" % ",".join("%s=?" % k for k in sets),
                    list(sets.values()) + [user_id])
    con.close()


def delete_user(user_id):
    con = connect()
    with con:
        con.execute("DELETE FROM jobs WHERE user_id=?", (user_id,))
        con.execute("DELETE FROM users WHERE id=?", (user_id,))
    con.close()


def get_user(user_id=None, username=None):
    con = connect()
    if username is not None:
        row = con.execute("SELECT * FROM users WHERE username=?", (username,)).fetchone()
    else:
        row = con.execute("SELECT * FROM users WHERE id=?", (user_id,)).fetchone()
    con.close()
    return dict(row) if row else None


def list_users(include_local=False):
    """Comptes reels. Le compte technique « local » (mode sans authentification)
    n'en est pas un : l'inclure ferait basculer une installation locale en mode
    connexion obligatoire des la premiere page consultee."""
    con = connect()
    rows = con.execute(
        "SELECT u.*, (SELECT COUNT(*) FROM jobs j WHERE j.user_id=u.id) AS jobs_count "
        "FROM users u WHERE (? OR u.username <> 'local') "
        "ORDER BY u.role='user', u.username", (1 if include_local else 0,)).fetchall()
    con.close()
    return [dict(r) for r in rows]


def count_admins():
    con = connect()
    n = con.execute("SELECT COUNT(*) FROM users WHERE role='admin' AND active=1").fetchone()[0]
    con.close()
    return n


def check_login(username, password):
    u = get_user(username=username)
    if not u or not u["active"] or not check_password_hash(u["password_hash"], password or ""):
        return None
    con = connect()
    with con:
        con.execute("UPDATE users SET last_login=? WHERE id=?", (time.time(), u["id"]))
    con.close()
    return u


# ---------------------------------------------------------------------- jobs
def create_job(jid, user_id, url, params, gsc_path=None):
    con = connect()
    with con:
        con.execute(
            "INSERT INTO jobs (id,user_id,url,state,message,params,gsc_path,created_at)"
            " VALUES (?,?,?,'queued','En attente…',?,?,?)",
            (jid, user_id, url, json.dumps(params), gsc_path, time.time()))
    con.close()


def update_job(jid, **fields):
    allowed = {"state", "pct", "message", "crawled", "report", "csv_dir", "pid",
               "cancel", "finished_at", "crawl_file"}
    sets = {k: v for k, v in fields.items() if k in allowed}
    if not sets:
        return
    con = connect()
    with con:
        con.execute("UPDATE jobs SET %s WHERE id=?" % ",".join("%s=?" % k for k in sets),
                    list(sets.values()) + [jid])
    con.close()


def get_job(jid):
    con = connect()
    row = con.execute("SELECT j.*, u.username FROM jobs j JOIN users u ON u.id=j.user_id"
                      " WHERE j.id=?", (jid,)).fetchone()
    con.close()
    return dict(row) if row else None


def list_jobs(user_id=None, limit=40):
    con = connect()
    if user_id is None:
        rows = con.execute("SELECT j.*, u.username FROM jobs j JOIN users u ON u.id=j.user_id"
                           " ORDER BY j.created_at DESC LIMIT ?", (limit,)).fetchall()
    else:
        rows = con.execute("SELECT j.*, u.username FROM jobs j JOIN users u ON u.id=j.user_id"
                           " WHERE j.user_id=? ORDER BY j.created_at DESC LIMIT ?",
                           (user_id, limit)).fetchall()
    con.close()
    return [dict(r) for r in rows]


def running_count(user_id=None):
    con = connect()
    q = "SELECT COUNT(*) FROM jobs WHERE state IN ('queued','running')"
    args = ()
    if user_id is not None:
        q += " AND user_id=?"
        args = (user_id,)
    n = con.execute(q, args).fetchone()[0]
    con.close()
    return n


def orphan_jobs():
    """Au demarrage : les jobs 'running' dont le process n'existe plus sont perdus."""
    con = connect()
    rows = con.execute("SELECT id, pid FROM jobs WHERE state IN ('queued','running')").fetchall()
    lost = []
    for r in rows:
        pid = r["pid"]
        alive = False
        if pid:
            try:
                os.kill(pid, 0)
                alive = True
            except OSError:
                alive = False
        if not alive:
            lost.append(r["id"])
    if lost:
        with con:
            con.executemany("UPDATE jobs SET state='error', message=?, finished_at=?"
                            " WHERE id=?",
                            [("Interrompu par un redemarrage du service.", time.time(), i)
                             for i in lost])
    con.close()
    return lost


# ------------------------------------------------------- suivi d'indexation
def add_index_snapshot(user_id, site, res):
    """Enregistre un import du rapport d'indexation. Retourne son id."""
    r = res["resume"]
    con = connect()
    with con:
        cur = con.execute(
            "INSERT INTO index_snapshots (user_id,site,imported_at,date_gsc,indexees,"
            "non_indexees,resume,motifs,alertes,courbe,urls) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            (user_id, site, time.time(), r.get("date"), r.get("indexees"), r.get("non_indexees"),
             json.dumps(r), json.dumps(res["motifs"]), json.dumps(res["alertes"]),
             json.dumps(res["courbe"]), json.dumps(res["urls"][:2000])))
        sid = cur.lastrowid
    con.close()
    return sid


def last_index_snapshot(user_id, site, before=None):
    con = connect()
    q = ("SELECT * FROM index_snapshots WHERE user_id=? AND site=?"
         + (" AND id<?" if before else "") + " ORDER BY imported_at DESC LIMIT 1")
    args = (user_id, site) + ((before,) if before else ())
    row = con.execute(q, args).fetchone()
    con.close()
    return dict(row) if row else None


def get_index_snapshot(sid, user_id=None, is_admin=False):
    con = connect()
    row = con.execute("SELECT s.*, u.username FROM index_snapshots s JOIN users u ON u.id=s.user_id"
                      " WHERE s.id=?", (sid,)).fetchone()
    con.close()
    if not row:
        return None
    d = dict(row)
    if user_id is not None and not is_admin and d["user_id"] != user_id:
        return None
    return d


def list_index_snapshots(user_id, limit=50):
    con = connect()
    rows = con.execute("SELECT id,site,imported_at,date_gsc,indexees,non_indexees,alertes"
                       " FROM index_snapshots WHERE user_id=?"
                       " ORDER BY imported_at DESC LIMIT ?", (user_id, limit)).fetchall()
    con.close()
    return [dict(r) for r in rows]


def index_sites(user_id):
    con = connect()
    rows = con.execute("SELECT site, COUNT(*) n, MAX(imported_at) last FROM index_snapshots"
                       " WHERE user_id=? GROUP BY site ORDER BY last DESC", (user_id,)).fetchall()
    con.close()
    return [dict(r) for r in rows]


def last_crawl_for_host(user_id, host, is_admin=False):
    """Dernier crawl termine d'un domaine, pour confronter les URL de la Search Console."""
    con = connect()
    q = ("SELECT * FROM jobs WHERE state='done' AND crawl_file IS NOT NULL"
         " AND (url LIKE ? OR url LIKE ?)")
    args = ["%//" + host + "/%", "%//" + host]
    if not is_admin:
        q += " AND user_id=?"
        args.append(user_id)
    q += " ORDER BY created_at DESC LIMIT 1"
    row = con.execute(q, args).fetchone()
    con.close()
    return dict(row) if row else None
