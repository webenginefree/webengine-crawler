# -*- coding: utf-8 -*-
"""Petite interface web locale : on colle une URL, on clique, on lit le rapport."""
from __future__ import annotations

import os
import secrets
import threading
import time
import traceback
import uuid
import webbrowser
from functools import wraps

from flask import (Flask, jsonify, redirect, request, send_file, session,
                   url_for, Response)
from werkzeug.middleware.proxy_fix import ProxyFix
from werkzeug.security import check_password_hash

from . import __version__
from .crawler import Crawler, DEFAULT_UA
from .report import build_data, export_csv, render_html

OUT = os.path.abspath(os.environ.get("WEBENGINE_OUT", "webengine-rapports"))
MAX_PAGES = int(os.environ.get("WEBENGINE_MAX_PAGES", "5000"))
JOBS = {}

app = Flask(__name__)
# Derriere un reverse proxy (Apache/Nginx) : recupere IP, schema et hote reels.
app.wsgi_app = ProxyFix(app.wsgi_app, x_for=1, x_proto=1, x_host=1)
app.secret_key = os.environ.get("WEBENGINE_SECRET") or secrets.token_hex(32)
app.config.update(
    SESSION_COOKIE_HTTPONLY=True,
    SESSION_COOKIE_SAMESITE="Lax",
    SESSION_COOKIE_SECURE=os.environ.get("WEBENGINE_HTTPS", "") == "1",
    PERMANENT_SESSION_LIFETIME=int(os.environ.get("WEBENGINE_SESSION_HOURS", "12")) * 3600,
)

# Authentification : activee seulement si les deux variables sont definies.
# En local (aucune variable), l'interface reste ouverte comme avant.
AUTH_USER = os.environ.get("WEBENGINE_USER", "")
AUTH_HASH = os.environ.get("WEBENGINE_PASSWORD_HASH", "")
AUTH_ON = bool(AUTH_USER and AUTH_HASH)

_ATTEMPTS = {}          # ip -> [nb_echecs, premier_echec_ts]
_LOCK = threading.Lock()
MAX_TRIES, WINDOW, BAN = 8, 600, 900


def _client_ip():
    return request.remote_addr or "?"


def _blocked(ip):
    with _LOCK:
        rec = _ATTEMPTS.get(ip)
        if not rec:
            return 0
        fails, first = rec
        if time.time() - first > (BAN if fails >= MAX_TRIES else WINDOW):
            _ATTEMPTS.pop(ip, None)
            return 0
        return max(0, int(first + BAN - time.time())) if fails >= MAX_TRIES else 0


def _note_failure(ip):
    with _LOCK:
        fails, first = _ATTEMPTS.get(ip, [0, time.time()])
        _ATTEMPTS[ip] = [fails + 1, first]


def login_required(fn):
    @wraps(fn)
    def wrapper(*a, **kw):
        if AUTH_ON and not session.get("user"):
            if request.path.startswith("/api/"):
                return jsonify(error="Session expiree, reconnectez-vous."), 401
            return redirect(url_for("login", next=request.path))
        return fn(*a, **kw)
    return wrapper

PAGE = """<!doctype html><html lang="fr"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><title>WebEngine Crawler</title>
<style>
:root{--bg:#f6f7f9;--panel:#fff;--ink:#12161c;--muted:#66707d;--line:#e2e6ec;--accent:#2f7d4f}
@media(prefers-color-scheme:dark){:root{--bg:#0f1216;--panel:#161b22;--ink:#e6edf3;--muted:#8b97a6;--line:#242c36;--accent:#4ea87a}}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--ink);
font:15px/1.55 -apple-system,BlinkMacSystemFont,"Segoe UI",Inter,Roboto,sans-serif;
display:flex;justify-content:center;padding:48px 20px}
.box{width:100%;max-width:680px}
h1{font-size:28px;margin:0 0 6px;letter-spacing:-.02em}p.sub{color:var(--muted);margin:0 0 26px}
form,.card{background:var(--panel);border:1px solid var(--line);border-radius:14px;padding:20px;margin-bottom:16px}
label{display:block;font-size:12px;text-transform:uppercase;letter-spacing:.06em;color:var(--muted);margin:14px 0 5px}
label:first-child{margin-top:0}
input,select{width:100%;padding:10px 12px;border:1px solid var(--line);border-radius:9px;background:var(--bg);color:var(--ink);font:inherit}
.row{display:flex;gap:12px}.row>div{flex:1}
button{margin-top:18px;width:100%;padding:12px;border:0;border-radius:9px;background:var(--accent);color:#fff;
font:600 15px inherit;cursor:pointer}button:disabled{opacity:.55;cursor:default}
.bar{height:8px;background:var(--bg);border-radius:6px;overflow:hidden;margin:12px 0}
.bar i{display:block;height:100%;background:var(--accent);width:0;transition:width .3s}
.log{font:12px/1.6 ui-monospace,SFMono-Regular,Menlo,monospace;color:var(--muted);word-break:break-all;min-height:20px}
a{color:var(--accent)}small{color:var(--muted)}
</style></head><body><div class="box">
<h1>⚙️ WebEngine Crawler</h1><p class="sub">Crawler SEO local — H1 en double, 404, liens entrants, Search Console.</p>
<form id="f" enctype="multipart/form-data">
  <label>URL du site</label><input name="url" placeholder="https://exemple.fr" required>
  <div class="row">
    <div><label>Pages max</label><input name="max_pages" type="number" value="500" min="1"></div>
    <div><label>Threads</label><input name="threads" type="number" value="8" min="1" max="32"></div>
    <div><label>Profondeur</label><input name="max_depth" type="number" value="15" min="1"></div>
  </div>
  <label>Export Search Console (optionnel — .csv, .zip ou .txt)</label>
  <input type="file" name="gsc" accept=".csv,.zip,.txt,.tsv">
  <label>Exclure (regex, optionnel)</label><input name="exclude" placeholder="/panier|\\?filtre=">
  <button id="go">Lancer le crawl</button>
</form>
<div class="card" id="prog" style="display:none">
  <b id="ptitle">Crawl en cours…</b><div class="bar"><i id="pbar"></i></div>
  <div class="log" id="plog"></div>
</div>
<div class="card"><b>Rapports precedents</b><div id="hist"><small>chargement…</small></div></div>
<small>WebEngine Crawler __V__ — tout reste sur votre machine.__LOGOUT__</small>
</div><script>
const f=document.getElementById('f');
f.onsubmit=async e=>{e.preventDefault();document.getElementById('go').disabled=true;
 document.getElementById('prog').style.display='block';
 const r=await fetch('/api/crawl',{method:'POST',body:new FormData(f)});const j=await r.json();
 if(j.error){document.getElementById('plog').textContent=j.error;document.getElementById('go').disabled=false;return;}
 poll(j.id);};
async function poll(id){
 const r=await fetch('/api/status/'+id);const s=await r.json();
 document.getElementById('pbar').style.width=Math.min(100,s.pct)+'%';
 document.getElementById('plog').textContent=s.message;
 if(s.state==='done'){document.getElementById('ptitle').innerHTML='Termine — <a href="/rapport/'+id+'" target="_blank">ouvrir le rapport</a>';
   document.getElementById('go').disabled=false;window.open('/rapport/'+id,'_blank');hist();return;}
 if(s.state==='error'){document.getElementById('ptitle').textContent='Erreur';document.getElementById('go').disabled=false;return;}
 setTimeout(()=>poll(id),700);}
async function hist(){const r=await fetch('/api/rapports');const l=await r.json();
 document.getElementById('hist').innerHTML=l.length?l.map(x=>'<div><a href="/fichier/'+encodeURIComponent(x.f)+'" target="_blank">'+x.f+'</a> <small>'+x.d+'</small></div>').join(''):'<small>aucun</small>';}
hist();
</script></body></html>"""


LOGIN_PAGE = """<!doctype html><html lang="fr"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><title>Connexion — WebEngine Crawler</title>
<style>
:root{--bg:#f6f7f9;--panel:#fff;--ink:#12161c;--muted:#66707d;--line:#e2e6ec;--accent:#2f7d4f;--err:#c0362c}
@media(prefers-color-scheme:dark){:root{--bg:#0f1216;--panel:#161b22;--ink:#e6edf3;--muted:#8b97a6;
--line:#242c36;--accent:#4ea87a;--err:#ff7b6b}}
*{box-sizing:border-box}body{margin:0;min-height:100vh;background:var(--bg);color:var(--ink);
font:15px/1.55 -apple-system,BlinkMacSystemFont,"Segoe UI",Inter,Roboto,sans-serif;
display:flex;align-items:center;justify-content:center;padding:24px}
form{background:var(--panel);border:1px solid var(--line);border-radius:14px;padding:28px;width:100%;max-width:380px}
h1{font-size:19px;margin:0 0 4px}p.s{color:var(--muted);margin:0 0 20px;font-size:13.5px}
label{display:block;font-size:12px;text-transform:uppercase;letter-spacing:.06em;color:var(--muted);margin:14px 0 5px}
input{width:100%;padding:10px 12px;border:1px solid var(--line);border-radius:9px;background:var(--bg);
color:var(--ink);font:inherit}
button{margin-top:20px;width:100%;padding:11px;border:0;border-radius:9px;background:var(--accent);
color:#fff;font:600 15px inherit;cursor:pointer}
.err{background:color-mix(in srgb, var(--err) 12%, transparent);color:var(--err);border-radius:8px;
padding:9px 12px;font-size:13.5px;margin-top:16px}
</style></head><body>
<form method="post" autocomplete="on">
  <h1>⚙️ WebEngine Crawler</h1>
  <p class="s">Accès réservé.</p>
  <label for="u">Identifiant</label>
  <input id="u" name="username" autocomplete="username" autofocus required>
  <label for="p">Mot de passe</label>
  <input id="p" name="password" type="password" autocomplete="current-password" required>
  <button type="submit">Se connecter</button>
  __ERR__
</form></body></html>"""


@app.route("/login", methods=["GET", "POST"])
def login():
    if not AUTH_ON:
        return redirect(url_for("index"))
    err = ""
    if request.method == "POST":
        wait = _blocked(_client_ip())
        if wait:
            err = "Trop de tentatives. Reessayez dans %d minute(s)." % max(1, wait // 60)
        else:
            user = (request.form.get("username") or "").strip()
            pwd = request.form.get("password") or ""
            if secrets.compare_digest(user, AUTH_USER) and check_password_hash(AUTH_HASH, pwd):
                session.permanent = True
                session["user"] = user
                nxt = request.args.get("next", "")
                return redirect(nxt if nxt.startswith("/") and not nxt.startswith("//") else url_for("index"))
            _note_failure(_client_ip())
            time.sleep(0.6)
            err = "Identifiant ou mot de passe incorrect."
    body = '<div class="err">%s</div>' % err if err else ""
    return Response(LOGIN_PAGE.replace("__ERR__", body), mimetype="text/html",
                    status=401 if err else 200)


@app.route("/logout")
def logout():
    session.clear()
    return redirect(url_for("login") if AUTH_ON else url_for("index"))


@app.route("/")
@login_required
def index():
    out = PAGE.replace("__V__", __version__)
    out = out.replace("__LOGOUT__", ' · <a href="/logout">Deconnexion</a>' if AUTH_ON else "")
    return Response(out, mimetype="text/html")


@app.route("/api/crawl", methods=["POST"])
@login_required
def api_crawl():
    url = (request.form.get("url") or "").strip()
    if not url:
        return jsonify(error="URL manquante"), 400
    if not url.startswith("http"):
        url = "https://" + url
    jid = uuid.uuid4().hex[:10]
    os.makedirs(OUT, exist_ok=True)

    gsc_path = None
    up = request.files.get("gsc")
    if up and up.filename:
        gsc_path = os.path.join(OUT, "%s-gsc%s" % (jid, os.path.splitext(up.filename)[1] or ".csv"))
        up.save(gsc_path)

    opts = {
        "max_pages": max(1, min(MAX_PAGES, int(request.form.get("max_pages") or 500))),
        "threads": int(request.form.get("threads") or 8),
        "max_depth": int(request.form.get("max_depth") or 15),
        "exclude_re": (request.form.get("exclude") or "").strip() or None,
    }
    JOBS[jid] = {"state": "running", "pct": 0, "message": "Demarrage…", "file": None}
    threading.Thread(target=_run_job, args=(jid, url, opts, gsc_path), daemon=True).start()
    return jsonify(id=jid)


def _run_job(jid, url, opts, gsc_path):
    job = JOBS[jid]
    try:
        def progress(crawled=0, queued=0, url="", **kw):
            job["pct"] = min(99, crawled / max(1, opts["max_pages"]) * 100)
            job["message"] = "%d URL crawlees · %d en file · %s" % (crawled, queued, url[-70:])

        crawler = Crawler(url, progress=progress, user_agent=DEFAULT_UA, **opts)
        result = crawler.run()
        items = None
        if gsc_path:
            from .gsc import load_gsc, cross
            job["message"] = "Croisement Search Console…"
            items = cross(result, load_gsc(gsc_path))
        job["message"] = "Generation du rapport…"
        data = build_data(result, items)
        fname = "rapport-%s-%s.html" % (result.host.replace(":", "_"), time.strftime("%Y%m%d-%H%M%S"))
        path = os.path.join(OUT, fname)
        render_html(data, path)
        export_csv(data, os.path.join(OUT, fname[:-5] + "-csv"))
        job.update(state="done", pct=100, file=path,
                   message="%d URL analysees." % data["resume"]["total"])
    except Exception as exc:
        traceback.print_exc()
        job.update(state="error", message="%s: %s" % (type(exc).__name__, exc))


@app.route("/api/status/<jid>")
@login_required
def api_status(jid):
    return jsonify(JOBS.get(jid, {"state": "error", "pct": 0, "message": "Job inconnu"}))


@app.route("/rapport/<jid>")
@login_required
def rapport(jid):
    job = JOBS.get(jid)
    if not job or not job.get("file"):
        return "Rapport introuvable", 404
    return send_file(job["file"])


@app.route("/api/rapports")
@login_required
def api_rapports():
    if not os.path.isdir(OUT):
        return jsonify([])
    files = [f for f in os.listdir(OUT) if f.endswith(".html")]
    files.sort(key=lambda f: os.path.getmtime(os.path.join(OUT, f)), reverse=True)
    return jsonify([{"f": f, "d": time.strftime("%d/%m %H:%M",
                    time.localtime(os.path.getmtime(os.path.join(OUT, f))))} for f in files[:15]])


@app.route("/fichier/<path:name>")
@login_required
def fichier(name):
    p = os.path.join(OUT, os.path.basename(name))
    if not os.path.isfile(p):
        return "Introuvable", 404
    return send_file(p)


def run(host="127.0.0.1", port=5005, open_browser=True):
    url = "http://%s:%d" % (host, port)
    print("⚙️  WebEngine Crawler — interface web sur %s" % url)
    print("    Rapports enregistres dans %s" % OUT)
    if open_browser:
        threading.Timer(1.0, lambda: webbrowser.open(url)).start()
    app.run(host=host, port=port, threaded=True, debug=False)
