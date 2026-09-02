# -*- coding: utf-8 -*-
"""Interface web : comptes, administration, crawls isoles en process dedies.

Deux modes :
  * local (defaut)  : aucun compte, l'interface s'ouvre directement ;
  * multi-comptes   : des qu'un compte existe, ou si WEBENGINE_AUTH=1.
    Le premier demarrage propose alors la creation du compte administrateur.
"""
from __future__ import annotations

import os
import secrets
import signal
import subprocess
import sys
import time
import uuid
import webbrowser
from functools import wraps

from flask import (Flask, abort, flash, get_flashed_messages, jsonify, redirect,
                   render_template_string, request, send_file, session, url_for)
from werkzeug.middleware.proxy_fix import ProxyFix
from werkzeug.utils import secure_filename

from . import __version__, db

OUT = os.path.abspath(os.environ.get("WEBENGINE_OUT", "webengine-rapports"))
os.environ["WEBENGINE_OUT"] = OUT
MAX_PAGES = int(os.environ.get("WEBENGINE_MAX_PAGES", "5000"))
MAX_GLOBAL = int(os.environ.get("WEBENGINE_MAX_GLOBAL", "3"))
FORCE_AUTH = os.environ.get("WEBENGINE_AUTH", "") == "1"
MAX_TRIES, WINDOW, BAN = 8, 600, 900

app = Flask(__name__)
app.wsgi_app = ProxyFix(app.wsgi_app, x_for=1, x_proto=1, x_host=1)
app.secret_key = os.environ.get("WEBENGINE_SECRET") or secrets.token_hex(32)
app.config.update(
    SESSION_COOKIE_HTTPONLY=True,
    SESSION_COOKIE_SAMESITE="Lax",
    SESSION_COOKIE_SECURE=os.environ.get("WEBENGINE_HTTPS", "") == "1",
    PERMANENT_SESSION_LIFETIME=int(os.environ.get("WEBENGINE_SESSION_HOURS", "12")) * 3600,
    MAX_CONTENT_LENGTH=50 * 1024 * 1024,
)

db.init()
db.orphan_jobs()


# --------------------------------------------------------------------- socle
def _migrate_env_user():
    """Reprend un compte defini par variables d'environnement (ancienne version)."""
    user, hsh = os.environ.get("WEBENGINE_USER"), os.environ.get("WEBENGINE_PASSWORD_HASH")
    if not (user and hsh) or db.list_users():
        return
    con = db.connect()
    with con:
        con.execute("INSERT INTO users (username,password_hash,role,active,max_pages,"
                    "max_parallel,created_at) VALUES (?,?,'admin',1,?,3,?)",
                    (user, hsh, MAX_PAGES, time.time()))
    con.close()


_migrate_env_user()


def auth_required():
    return FORCE_AUTH or bool(db.list_users())


def local_user():
    u = db.get_user(username="local")
    if not u:
        con = db.connect()
        with con:
            con.execute("INSERT OR IGNORE INTO users (username,password_hash,role,active,"
                        "max_pages,max_parallel,created_at) VALUES ('local','-','admin',1,?,?,?)",
                        (MAX_PAGES, MAX_GLOBAL, time.time()))
        con.close()
        u = db.get_user(username="local")
    return u


def current_user():
    if not auth_required():
        return local_user()
    uid = session.get("uid")
    if not uid:
        return None
    u = db.get_user(user_id=uid)
    if not u or not u["active"]:
        session.clear()
        return None
    return u


def login_required(fn):
    @wraps(fn)
    def wrapper(*a, **kw):
        u = current_user()
        if not u:
            if not db.list_users():
                return redirect(url_for("setup"))
            if request.path.startswith("/api/"):
                return jsonify(error="Session expiree, reconnectez-vous."), 401
            return redirect(url_for("login", next=request.path))
        request.user = u
        return fn(*a, **kw)
    return wrapper


def admin_required(fn):
    @wraps(fn)
    @login_required
    def wrapper(*a, **kw):
        if request.user["role"] != "admin":
            abort(403)
        return fn(*a, **kw)
    return wrapper


def csrf_token():
    if "csrf" not in session:
        session["csrf"] = secrets.token_urlsafe(24)
    return session["csrf"]


def check_csrf():
    sent = request.form.get("csrf") or request.headers.get("X-CSRF")
    if not sent or not secrets.compare_digest(sent, session.get("csrf", "")):
        abort(400, "Jeton de securite invalide, rechargez la page.")


app.jinja_env.globals["csrf_token"] = csrf_token


# ------------------------------------------------------------ anti-bruteforce
def _throttle_state(ip):
    con = db.connect()
    row = con.execute("SELECT fails, first FROM login_attempts WHERE ip=?", (ip,)).fetchone()
    con.close()
    if not row:
        return 0
    fails, first = row["fails"], row["first"]
    if fails >= MAX_TRIES and time.time() - first < BAN:
        return int(first + BAN - time.time())
    if time.time() - first > max(WINDOW, BAN):
        con = db.connect()
        with con:
            con.execute("DELETE FROM login_attempts WHERE ip=?", (ip,))
        con.close()
    return 0


def _note_failure(ip):
    con = db.connect()
    with con:
        con.execute("INSERT INTO login_attempts (ip,fails,first) VALUES (?,1,?) "
                    "ON CONFLICT(ip) DO UPDATE SET fails=fails+1", (ip, time.time()))
    con.close()


def _clear_failures(ip):
    con = db.connect()
    with con:
        con.execute("DELETE FROM login_attempts WHERE ip=?", (ip,))
    con.close()


# ------------------------------------------------------------------ gabarits
BASE = """<!doctype html><html lang="fr"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><title>{{ title }}</title>
<style>
:root{--bg:#f6f7f9;--panel:#fff;--ink:#12161c;--muted:#66707d;--line:#e2e6ec;--accent:#2f7d4f;
--accent-soft:#e8f4ec;--err:#c0362c;--err-soft:#fbeae8;--warn:#976409;--warn-soft:#fbf3e4;--serie:#976409;--serie-soft:#9764091f}
@media(prefers-color-scheme:dark){:root{--bg:#0f1216;--panel:#161b22;--ink:#e6edf3;--muted:#8b97a6;
--line:#242c36;--accent:#4ea87a;--accent-soft:#16261e;--err:#ff7b6b;--err-soft:#2a1614;
--warn:#dda63f;--warn-soft:#291f0e;--serie:#b8862f;--serie-soft:#b8862f2e}}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--ink);
font:15px/1.55 -apple-system,BlinkMacSystemFont,"Segoe UI",Inter,Roboto,sans-serif}
a{color:var(--accent)}
header{background:var(--panel);border-bottom:1px solid var(--line);padding:0 22px;
display:flex;align-items:center;gap:20px;height:56px;position:sticky;top:0;z-index:5}
header .b{font-weight:700;font-size:16px}
header nav{margin-left:auto;display:flex;gap:18px;align-items:center;font-size:14px}
header nav a{color:var(--muted);text-decoration:none}header nav a:hover{color:var(--ink)}
header .who{font-size:13px;color:var(--muted)}
.tag{font-size:11px;padding:1px 7px;border-radius:20px;background:var(--accent-soft);
color:var(--accent);font-weight:600}
main{max-width:1000px;margin:0 auto;padding:28px 22px 60px}
h1{font-size:21px;margin:0 0 4px}h2{font-size:17px;margin:30px 0 12px}
p.sub{color:var(--muted);margin:0 0 22px;font-size:14px}
.card{background:var(--panel);border:1px solid var(--line);border-radius:13px;padding:20px;margin-bottom:18px}
label{display:block;font-size:12px;text-transform:uppercase;letter-spacing:.06em;color:var(--muted);margin:14px 0 5px}
label:first-child{margin-top:0}
input,select{width:100%;padding:9px 11px;border:1px solid var(--line);border-radius:9px;
background:var(--bg);color:var(--ink);font:inherit}
.row{display:flex;gap:12px;flex-wrap:wrap}.row>div{flex:1;min-width:120px}
button,.btn{margin-top:16px;padding:10px 16px;border:0;border-radius:9px;background:var(--accent);
color:#fff;font:600 15px inherit;cursor:pointer;text-decoration:none;display:inline-block}
button.sm,.btn.sm{margin:0;padding:5px 10px;font-size:13px}
button.ghost{background:transparent;color:var(--muted);border:1px solid var(--line)}
button.danger{background:var(--err)}
table{width:100%;border-collapse:collapse;font-size:13.5px}
th{text-align:left;padding:8px 10px;border-bottom:1px solid var(--line);color:var(--muted);
font-weight:600;white-space:nowrap}
td{padding:8px 10px;border-bottom:1px solid var(--line);vertical-align:middle}
tr:last-child td{border-bottom:0}
.pill{display:inline-block;padding:1px 8px;border-radius:20px;font-size:11.5px;font-weight:600}
.s-done{background:var(--accent-soft);color:var(--accent)}
.s-running,.s-queued{background:var(--warn-soft);color:var(--warn)}
.s-error,.s-cancelled{background:var(--err-soft);color:var(--err)}
.bar{height:6px;background:var(--bg);border-radius:4px;overflow:hidden;margin-top:5px;min-width:90px}
.bar i{display:block;height:100%;background:var(--accent);transition:width .4s}
.msg{padding:10px 13px;border-radius:9px;margin-bottom:14px;font-size:14px}
.msg.ok{background:var(--accent-soft);color:var(--accent)}
.msg.ko{background:var(--err-soft);color:var(--err)}
.mut{color:var(--muted)}small{color:var(--muted)}
.wrap{overflow-x:auto}
</style></head><body>
{% if user %}<header>
  <span class="b">⚙️ WebEngine Crawler</span>
  <nav>
    <a href="{{ url_for('index') }}">Crawler</a>
    <a href="{{ url_for('indexation') }}">Indexation</a>
    {% if user.role == 'admin' and auth %}<a href="{{ url_for('admin') }}">Comptes</a>{% endif %}
    {% if auth %}<span class="who">{{ user.username }}
      {% if user.role == 'admin' %}<span class="tag">admin</span>{% endif %}</span>{% endif %}
    {% if auth %}<a href="{{ url_for('logout') }}">Deconnexion</a>{% endif %}
  </nav>
</header>{% endif %}
<main>
{% for cat, m in messages %}<div class="msg {{ cat }}">{{ m }}</div>{% endfor %}
{{ body|safe }}
</main></body></html>"""


def page(body_html, title="WebEngine Crawler", user=None, **ctx):
    msgs = get_flashed_messages(with_categories=True)
    body = render_template_string(body_html, user=user, auth=auth_required(), **ctx)
    return render_template_string(BASE, title=title, user=user, auth=auth_required(),
                                  messages=msgs, body=body)


LOGIN_BODY = """
<div class="card" style="max-width:380px;margin:60px auto">
  <h1>⚙️ WebEngine Crawler</h1><p class="sub">{{ intro }}</p>
  <form method="post">
    <input type="hidden" name="csrf" value="{{ csrf_token() }}">
    <label>Identifiant</label>
    <input name="username" autocomplete="username" autofocus required>
    <label>Mot de passe</label>
    <input name="password" type="password" autocomplete="{{ ac }}" required>
    {% if setup %}<label>Confirmer le mot de passe</label>
    <input name="password2" type="password" autocomplete="new-password" required>{% endif %}
    <button type="submit" style="width:100%">{{ cta }}</button>
  </form>
</div>"""


INDEX_BODY = """
<h1>Lancer un crawl</h1>
<p class="sub">Chaque crawl s'execute dans un process isole. Plafond de votre compte :
  {{ user.max_pages }} URL par crawl, {{ user.max_parallel }} crawl(s) simultane(s).</p>

<div class="card">
  <form id="f" enctype="multipart/form-data">
    <input type="hidden" name="csrf" value="{{ csrf_token() }}">
    <label>URL du site</label>
    <input name="url" placeholder="https://exemple.fr" required>
    <div class="row">
      <div><label>Pages max</label>
        <input name="max_pages" type="number" value="{{ [500, user.max_pages]|min }}"
               min="1" max="{{ user.max_pages }}"></div>
      <div><label>Threads</label><input name="threads" type="number" value="8" min="1" max="16"></div>
      <div><label>Profondeur</label><input name="max_depth" type="number" value="15" min="1"></div>
      <div><label>Delai (s)</label><input name="delay" type="number" value="0" min="0" max="5" step="0.1"></div>
    </div>
    <label>Export Search Console (optionnel : .csv, .zip, .txt)</label>
    <input type="file" name="gsc" accept=".csv,.zip,.txt,.tsv">
    <label>Exclure (expression reguliere, optionnel)</label>
    <input name="exclude" placeholder="/panier|\\?filtre=">
    <button id="go" type="submit">Lancer le crawl</button>
  </form>
</div>

<h2>{{ 'Tous les crawls' if scope_all else 'Mes crawls' }}</h2>
<div class="card" style="padding:6px 8px"><div class="wrap"><table id="jobs">
  <thead><tr><th>Site</th>{% if scope_all %}<th>Compte</th>{% endif %}
    <th>Etat</th><th>Avancement</th><th>Quand</th><th></th></tr></thead>
  <tbody><tr><td colspan="6" class="mut" style="padding:16px">chargement…</td></tr></tbody>
</table></div></div>
{% if user.role == 'admin' and auth %}
<p><small>Vue administrateur :
  <a href="{{ url_for('index', all=1) }}">tous les comptes</a> ·
  <a href="{{ url_for('index') }}">seulement les miens</a></small></p>
{% endif %}

<script>
const CSRF = "{{ csrf_token() }}", ALL = {{ 1 if scope_all else 0 }};
const esc = s => String(s??'').replace(/[&<>"]/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c]));
const AGO = t => { const d = Date.now()/1000 - t;
  if (d < 60) return "a l'instant"; if (d < 3600) return Math.floor(d/60)+" min";
  if (d < 86400) return Math.floor(d/3600)+" h"; return Math.floor(d/86400)+" j"; };
const LBL = {queued:'en attente', running:'en cours', done:'termine', error:'erreur', cancelled:'annule'};

async function refresh(){
  const r = await fetch('/api/jobs' + (ALL ? '?all=1' : ''));
  if (r.status === 401) { location.href = '/login'; return; }
  const jobs = await r.json();
  const tb = document.querySelector('#jobs tbody');
  if (!jobs.length){ tb.innerHTML = '<tr><td colspan="6" class="mut" style="padding:16px">Aucun crawl pour le moment.</td></tr>'; return; }
  tb.innerHTML = jobs.map(j => {
    const act = (j.state === 'done')
      ? `<a class="btn sm" href="/rapport/${j.id}" target="_blank">Rapport</a>`
      : (j.state === 'running' || j.state === 'queued')
        ? `<button class="sm ghost" onclick="cancel('${j.id}')">Annuler</button>` : '';
    return `<tr>
      <td style="max-width:280px;word-break:break-all">${esc(j.url)}</td>
      ${ALL ? '<td>'+esc(j.username)+'</td>' : ''}
      <td><span class="pill s-${j.state}">${LBL[j.state]||j.state}</span></td>
      <td style="min-width:220px"><div class="mut" style="font-size:12px">${esc((j.message||'').slice(0,90))}</div>
        ${j.state==='running'||j.state==='queued' ? `<div class="bar"><i style="width:${j.pct||0}%"></i></div>` : ''}</td>
      <td class="mut" style="white-space:nowrap">${AGO(j.created_at)}</td>
      <td style="text-align:right">${act}</td></tr>`;
  }).join('');
}
async function cancel(id){
  await fetch('/api/cancel/' + id, {method:'POST', headers:{'X-CSRF': CSRF}});
  refresh();
}
document.getElementById('f').onsubmit = async e => {
  e.preventDefault();
  const b = document.getElementById('go'); b.disabled = true; b.textContent = 'Lancement…';
  const r = await fetch('/api/crawl', {method:'POST', body:new FormData(e.target)});
  const j = await r.json();
  b.disabled = false; b.textContent = 'Lancer le crawl';
  if (j.error) alert(j.error); else e.target.url.value = '';
  refresh();
};
refresh(); setInterval(refresh, 2500);
</script>"""

ADMIN_BODY = """
<h1>Comptes</h1>
<p class="sub">Chaque compte a ses propres rapports et ses propres quotas.
  Les crawls tournent dans des process separes, plafonnes en memoire.</p>

<div class="card">
  <h2 style="margin-top:0">Creer un compte</h2>
  <form method="post" action="{{ url_for('admin_create') }}">
    <input type="hidden" name="csrf" value="{{ csrf_token() }}">
    <div class="row">
      <div><label>Identifiant</label><input name="username" required></div>
      <div><label>Mot de passe (10 car. min.)</label>
        <input name="password" value="{{ suggestion }}" required></div>
      <div><label>Role</label><select name="role">
        <option value="user">utilisateur</option><option value="admin">administrateur</option>
      </select></div>
      <div><label>URL max / crawl</label><input name="max_pages" type="number" value="1000" min="1"></div>
      <div><label>Crawls simultanes</label><input name="max_parallel" type="number" value="1" min="1" max="5"></div>
    </div>
    <button type="submit">Creer le compte</button>
  </form>
</div>

<div class="card" style="padding:6px 8px"><div class="wrap"><table>
<thead><tr><th>Compte</th><th>Role</th><th>Etat</th><th>Quotas</th><th>Crawls</th>
<th>Derniere connexion</th><th></th></tr></thead><tbody>
{% for u in users %}
<tr>
  <td><b>{{ u.username }}</b></td>
  <td>{% if u.role == 'admin' %}<span class="tag">admin</span>{% else %}utilisateur{% endif %}</td>
  <td>{% if u.active %}<span class="pill s-done">actif</span>
      {% else %}<span class="pill s-error">desactive</span>{% endif %}</td>
  <td class="mut">{{ u.max_pages }} URL · {{ u.max_parallel }} //</td>
  <td class="mut">{{ u.jobs_count }}</td>
  <td class="mut">{{ u.last_login_h }}</td>
  <td style="text-align:right;white-space:nowrap">
    <form method="post" action="{{ url_for('admin_action') }}" style="display:inline">
      <input type="hidden" name="csrf" value="{{ csrf_token() }}">
      <input type="hidden" name="uid" value="{{ u.id }}">
      <button class="sm ghost" name="action" value="toggle" type="submit">
        {{ 'Desactiver' if u.active else 'Activer' }}</button>
      <button class="sm ghost" name="action" value="reset" type="submit"
        onclick="return confirm('Generer un nouveau mot de passe pour {{ u.username }} ?')">Nouveau mdp</button>
      <button class="sm danger" name="action" value="delete" type="submit"
        onclick="return confirm('Supprimer {{ u.username }} et tous ses crawls ?')">Supprimer</button>
    </form>
  </td>
</tr>
{% endfor %}
</tbody></table></div></div>"""


IDX_BODY = """
<h1>Suivi d'indexation</h1>
<p class="sub">Deposez l'export <b>Search Console &rsaquo; Indexation &rsaquo; Pages</b>.
  L'export contient la courbe des 90 derniers jours : la hausse est detectee des le premier import.</p>

<div class="card">
  <form method="post" enctype="multipart/form-data">
    <input type="hidden" name="csrf" value="{{ csrf_token() }}">
    <div class="row">
      <div style="flex:2"><label>Export (.zip ou .csv)</label>
        <input type="file" name="export" accept=".zip,.csv,.tsv" required></div>
      <div><label>Site (pour l'historique)</label>
        <input name="site" placeholder="exemple.fr" value="{{ dernier_site }}"></div>
    </div>
    <button type="submit">Analyser</button>
  </form>
  <p style="margin:14px 0 0"><small>Dans la Search Console : <b>Indexation &rsaquo; Pages</b>,
    bouton <b>Exporter</b> en haut a droite. Pour la liste des URL d'un motif, ouvrez le motif
    puis exportez : le croisement avec votre dernier crawl devient possible.</small></p>
</div>

{% if historique %}
<h2>Imports precedents</h2>
<div class="card" style="padding:6px 8px"><div class="wrap"><table>
<thead><tr><th>Site</th><th>Etat au</th><th>Indexees</th><th>Non indexees</th>
<th>Alertes</th><th>Importe</th></tr></thead><tbody>
{% for h in historique %}<tr>
  <td><a href="{{ url_for('indexation_detail', sid=h.id) }}">{{ h.site }}</a></td>
  <td class="mut">{{ h.date_gsc or '—' }}</td>
  <td>{{ h.indexees if h.indexees is not none else '—' }}</td>
  <td>{{ h.non_indexees if h.non_indexees is not none else '—' }}</td>
  <td>{% if h.n_alertes %}<span class="pill s-error">{{ h.n_alertes }}</span>
      {% else %}<span class="pill s-done">0</span>{% endif %}</td>
  <td class="mut">{{ h.quand }}</td>
</tr>{% endfor %}
</tbody></table></div></div>
{% endif %}"""


IDX_DETAIL = """
<h1>{{ snap.site }}</h1>
<p class="sub">Import du {{ quand }}{% if r.date %} — etat Search Console au {{ r.date }}{% endif %}
  {% if precedent %} · comparaison avec l'import du {{ precedent_quand }}{% endif %}</p>

{% if alertes %}
<div class="card" style="border-color:var(--err)">
  <h2 style="margin:0 0 12px">Ce qui doit vous alerter</h2>
  {% for a in alertes %}
  <div style="display:flex;gap:10px;padding:9px 0;border-top:1px solid var(--line)">
    <span class="pill s-{{ 'error' if a.gravite == 'critique' else 'running' }}"
          style="height:fit-content;white-space:nowrap">
      {{ '⛔ critique' if a.gravite == 'critique' else '⚠ a verifier' }}</span>
    <div><b>{{ a.titre }}</b>{% if a.detail %}<div class="mut" style="font-size:13px">{{ a.detail }}</div>{% endif %}</div>
  </div>
  {% endfor %}
</div>
{% else %}
<div class="msg ok">Aucune hausse anormale detectee sur cet import.</div>
{% endif %}

<div class="row" style="gap:14px;margin-bottom:18px">
  <div class="card" style="margin:0"><div class="mut" style="font-size:12px">PAGES INDEXEES</div>
    <div style="font-size:27px;font-weight:650">{{ r.indexees if r.indexees is not none else '—' }}</div></div>
  <div class="card" style="margin:0"><div class="mut" style="font-size:12px">PAGES NON INDEXEES</div>
    <div style="font-size:27px;font-weight:650;color:var(--serie)">{{ r.non_indexees if r.non_indexees is not none else '—' }}</div></div>
  <div class="card" style="margin:0"><div class="mut" style="font-size:12px">SUR 30 JOURS</div>
    <div style="font-size:27px;font-weight:650;{% if (r.delta_30j or 0) > 0 %}color:var(--err){% endif %}">
      {% if r.delta_30j is defined %}{{ '+' if r.delta_30j >= 0 }}{{ r.delta_30j }}{% else %}—{% endif %}</div></div>
  <div class="card" style="margin:0"><div class="mut" style="font-size:12px">PAGES A PROBLEMES</div>
    <div style="font-size:27px;font-weight:650">{{ r.pages_a_problemes or 0 }}</div></div>
</div>

{% if courbe %}
<div class="card">
  <h2 style="margin:0 0 2px">Pages non indexees</h2>
  <p class="mut" style="margin:0 0 10px;font-size:13px">{{ courbe|length }} jours d'historique fournis par la Search Console.</p>
  <div id="chartwrap" style="position:relative">
    <svg id="chart" viewBox="0 0 720 210" preserveAspectRatio="none" role="img"
         aria-label="Evolution du nombre de pages non indexees sur {{ courbe|length }} jours"
         style="width:100%;height:210px;display:block"></svg>
    <div id="tip" style="position:absolute;display:none;pointer-events:none;background:var(--panel);
         border:1px solid var(--line);border-radius:8px;padding:6px 9px;font-size:12.5px;
         box-shadow:0 4px 14px rgba(0,0,0,.12);white-space:nowrap"></div>
  </div>
  <details style="margin-top:10px"><summary class="mut" style="cursor:pointer;font-size:13px">Voir les valeurs</summary>
    <div class="wrap" style="max-height:220px;overflow:auto;margin-top:8px"><table>
      <thead><tr><th>Date</th><th>Non indexees</th><th>Indexees</th></tr></thead><tbody>
      {% for c in courbe|reverse %}<tr><td>{{ c.date }}</td><td>{{ c.non_indexees }}</td>
        <td class="mut">{{ c.indexees if c.indexees is not none else '—' }}</td></tr>{% endfor %}
      </tbody></table></div>
  </details>
</div>
{% endif %}

{% if motifs %}
<h2>Pourquoi ces pages ne sont pas indexees</h2>
<div class="card" style="padding:6px 8px"><div class="wrap"><table>
<thead><tr><th>Motif</th><th>Pages</th><th>Evolution</th><th>Gravite</th><th>Que faire</th></tr></thead><tbody>
{% for m in motifs %}<tr>
  <td><b>{{ m.libelle }}</b></td>
  <td style="font-variant-numeric:tabular-nums">{{ m.pages }}</td>
  <td style="font-variant-numeric:tabular-nums">
    {% if m.delta is not none %}
      <span {% if m.delta > 0 %}style="color:var(--err);font-weight:600"{% elif m.delta < 0 %}style="color:var(--accent)"{% endif %}>
      {{ '+' if m.delta > 0 }}{{ m.delta }}</span>
    {% else %}<span class="mut">1er import</span>{% endif %}</td>
  <td><span class="pill s-{{ 'error' if m.gravite == 'critique' else ('running' if m.gravite == 'eleve' else 'done') }}">{{ m.gravite }}</span></td>
  <td class="mut" style="font-size:12.5px;max-width:340px">{{ m.aide }}</td>
</tr>{% endfor %}
</tbody></table></div></div>
{% endif %}

{% if urls %}
<h2>URL concernees {% if croise %}<span class="mut" style="font-size:13px;font-weight:400">— confrontees a votre crawl du {{ crawl_quand }}</span>{% endif %}</h2>
{% if not croise %}<div class="msg" style="background:var(--warn-soft);color:var(--warn)">
  Aucun crawl de ce domaine dans l'outil : lancez-en un pour savoir ce que repondent ces URL aujourd'hui.</div>{% endif %}
<div class="card" style="padding:6px 8px"><div class="wrap"><table>
<thead><tr><th>URL</th><th>Motif Google</th>{% if croise %}<th>Statut reel</th><th>Liens entrants</th>
  <th>Diagnostic</th>{% endif %}</tr></thead><tbody>
{% for u in urls %}<tr>
  <td style="max-width:320px;word-break:break-all"><a href="{{ u.url }}" target="_blank" rel="noopener">{{ u.url }}</a></td>
  <td class="mut" style="font-size:12.5px">{{ u.motif }}</td>
  {% if croise %}
  <td>{% if u.statut_reel %}<span class="pill s-{{ 'done' if u.statut_reel == 200 else 'error' }}">{{ u.statut_reel }}</span>
      {% else %}<span class="mut">absente</span>{% endif %}</td>
  <td style="font-variant-numeric:tabular-nums">{{ u.liens_entrants if u.liens_entrants is not none else '—' }}</td>
  <td class="mut" style="font-size:12.5px;max-width:320px">{{ u.diagnostic }}</td>
  {% endif %}
</tr>{% endfor %}
</tbody></table></div></div>
{% endif %}

<script>
const SERIE = {{ courbe_json|safe }}, SAUT = {{ saut_json|safe }};
if (SERIE.length > 1) {
  const svg = document.getElementById('chart'), tip = document.getElementById('tip');
  const W = 720, H = 210, ML = 46, MR = 14, MT = 14, MB = 26;
  const iw = W - ML - MR, ih = H - MT - MB;
  const vals = SERIE.map(d => d.non_indexees);
  const vmax = Math.max(...vals), vmin = Math.min(...vals);
  const top = vmax + Math.max(1, (vmax - vmin) * 0.15), bot = Math.max(0, vmin - (vmax - vmin) * 0.15);
  const X = i => ML + (SERIE.length === 1 ? iw / 2 : i * iw / (SERIE.length - 1));
  const Y = v => MT + ih - (v - bot) / Math.max(1, top - bot) * ih;
  const fr = n => n.toLocaleString('fr-FR');
  const NS = 'http://www.w3.org/2000/svg';
  const el = (t, a) => { const e = document.createElementNS(NS, t);
    for (const k in a) e.setAttribute(k, a[k]); return e; };

  // grille et graduations, volontairement discretes
  for (let g = 0; g <= 2; g++) {
    const v = bot + (top - bot) * g / 2, y = Y(v);
    svg.appendChild(el('line', {x1: ML, x2: W - MR, y1: y, y2: y,
      stroke: 'var(--line)', 'stroke-width': 1}));
    const t = el('text', {x: ML - 8, y: y + 4, 'text-anchor': 'end',
      fill: 'var(--muted)', 'font-size': 11}); t.textContent = fr(Math.round(v));
    svg.appendChild(t);
  }
  [[0, 'start'], [SERIE.length - 1, 'end']].forEach(([i, anc]) => {
    const t = el('text', {x: X(i), y: H - 8, 'text-anchor': anc, fill: 'var(--muted)', 'font-size': 11});
    t.textContent = SERIE[i].date; svg.appendChild(t);
  });

  const d = SERIE.map((p, i) => (i ? 'L' : 'M') + X(i).toFixed(1) + ' ' + Y(p.non_indexees).toFixed(1)).join(' ');
  svg.appendChild(el('path', {d: d + ` L${X(SERIE.length-1)} ${MT+ih} L${X(0)} ${MT+ih} Z`,
    fill: 'var(--serie-soft)', stroke: 'none'}));
  svg.appendChild(el('path', {d: d, fill: 'none', stroke: 'var(--serie)', 'stroke-width': 2,
    'stroke-linejoin': 'round', 'stroke-linecap': 'round'}));

  // le jour de la hausse brutale : repere + etiquette, jamais la couleur seule
  if (SAUT) {
    const i = SERIE.findIndex(p => p.date === SAUT.date);
    if (i > 0) {
      svg.appendChild(el('line', {x1: X(i), x2: X(i), y1: MT, y2: MT + ih,
        stroke: 'var(--err)', 'stroke-width': 1, 'stroke-dasharray': '3 3'}));
      const t = el('text', {x: Math.min(X(i) + 6, W - MR - 100), y: MT + 12,
        fill: 'var(--err)', 'font-size': 11, 'font-weight': 600});
      t.textContent = '+' + SAUT.pages + ' le ' + SAUT.date; svg.appendChild(t);
    }
  }

  // derniere valeur etiquetee directement
  const last = SERIE.length - 1;
  svg.appendChild(el('circle', {cx: X(last), cy: Y(vals[last]), r: 4, fill: 'var(--serie)',
    stroke: 'var(--panel)', 'stroke-width': 2}));

  const cur = el('line', {x1: 0, x2: 0, y1: MT, y2: MT + ih, stroke: 'var(--muted)',
    'stroke-width': 1, opacity: 0, 'pointer-events': 'none'});
  const dot = el('circle', {r: 4.5, fill: 'var(--serie)', stroke: 'var(--panel)',
    'stroke-width': 2, opacity: 0, 'pointer-events': 'none'});
  svg.appendChild(cur); svg.appendChild(dot);
  const hit = el('rect', {x: ML, y: MT, width: iw, height: ih, fill: 'transparent'});
  svg.appendChild(hit);
  hit.addEventListener('mousemove', e => {
    const box = svg.getBoundingClientRect();
    const px = (e.clientX - box.left) / box.width * W;
    let i = Math.round((px - ML) / iw * (SERIE.length - 1));
    i = Math.max(0, Math.min(SERIE.length - 1, i));
    const p = SERIE[i];
    cur.setAttribute('x1', X(i)); cur.setAttribute('x2', X(i)); cur.setAttribute('opacity', .5);
    dot.setAttribute('cx', X(i)); dot.setAttribute('cy', Y(p.non_indexees)); dot.setAttribute('opacity', 1);
    tip.style.display = 'block';
    tip.innerHTML = '<b>' + fr(p.non_indexees) + '</b> non indexees<br><span style="color:var(--muted)">' + p.date + '</span>';
    const wrap = document.getElementById('chartwrap').getBoundingClientRect();
    tip.style.left = Math.min(wrap.width - tip.offsetWidth - 4,
                              Math.max(0, (X(i) / W * wrap.width) - tip.offsetWidth / 2)) + 'px';
    tip.style.top = Math.max(0, (Y(p.non_indexees) / H * 210) - tip.offsetHeight - 10) + 'px';
  });
  hit.addEventListener('mouseleave', () => {
    cur.setAttribute('opacity', 0); dot.setAttribute('opacity', 0); tip.style.display = 'none';
  });
}
</script>"""


# --------------------------------------------------------------------- routes
@app.route("/setup", methods=["GET", "POST"])
def setup():
    if db.list_users():
        return redirect(url_for("login"))
    if request.method == "POST":
        check_csrf()
        pwd = request.form.get("password") or ""
        if pwd != (request.form.get("password2") or ""):
            flash("Les deux mots de passe different.", "ko")
        else:
            try:
                db.create_user(request.form.get("username"), pwd, role="admin",
                               max_pages=MAX_PAGES, max_parallel=MAX_GLOBAL)
                flash("Compte administrateur cree, connectez-vous.", "ok")
                return redirect(url_for("login"))
            except ValueError as exc:
                flash(str(exc), "ko")
    return page(LOGIN_BODY, "Installation", None, intro="Creation du compte administrateur.",
                cta="Creer le compte", ac="new-password", setup=True)


@app.route("/login", methods=["GET", "POST"])
def login():
    if not auth_required():
        return redirect(url_for("index"))
    if not db.list_users():
        return redirect(url_for("setup"))
    if session.get("uid"):
        return redirect(url_for("index"))
    if request.method == "POST":
        check_csrf()
        ip = request.remote_addr or "?"
        wait = _throttle_state(ip)
        if wait:
            flash("Trop de tentatives. Reessayez dans %d minute(s)." % max(1, wait // 60), "ko")
        else:
            u = db.check_login((request.form.get("username") or "").strip(),
                               request.form.get("password") or "")
            if u:
                _clear_failures(ip)
                session.clear()
                session.permanent = True
                session["uid"] = u["id"]
                session["csrf"] = secrets.token_urlsafe(24)
                nxt = request.args.get("next", "")
                return redirect(nxt if nxt.startswith("/") and not nxt.startswith("//")
                                else url_for("index"))
            _note_failure(ip)
            time.sleep(0.6)
            flash("Identifiant ou mot de passe incorrect.", "ko")
    return page(LOGIN_BODY, "Connexion", None, intro="Acces reserve.",
                cta="Se connecter", ac="current-password", setup=False)


@app.route("/logout")
def logout():
    session.clear()
    return redirect(url_for("login"))


@app.route("/")
@login_required
def index():
    scope_all = bool(request.args.get("all")) and request.user["role"] == "admin"
    return page(INDEX_BODY, "WebEngine Crawler", request.user, scope_all=scope_all)


@app.route("/admin")
@admin_required
def admin():
    users = db.list_users()
    for u in users:
        u["last_login_h"] = (time.strftime("%d/%m %H:%M", time.localtime(u["last_login"]))
                             if u["last_login"] else "jamais")
    alpha = "abcdefghijkmnopqrstuvwxyzABCDEFGHJKLMNPQRSTUVWXYZ23456789"
    return page(ADMIN_BODY, "Comptes", request.user, users=users,
                suggestion="".join(secrets.choice(alpha) for _ in range(16)))


@app.route("/admin/create", methods=["POST"])
@admin_required
def admin_create():
    check_csrf()
    try:
        db.create_user(request.form.get("username"), request.form.get("password") or "",
                       role="admin" if request.form.get("role") == "admin" else "user",
                       max_pages=max(1, min(MAX_PAGES, int(request.form.get("max_pages") or 1000))),
                       max_parallel=max(1, min(5, int(request.form.get("max_parallel") or 1))))
        flash("Compte « %s » cree. Mot de passe : %s"
              % (request.form.get("username"), request.form.get("password")), "ok")
    except ValueError as exc:
        flash(str(exc), "ko")
    return redirect(url_for("admin"))


@app.route("/admin/action", methods=["POST"])
@admin_required
def admin_action():
    check_csrf()
    uid = int(request.form.get("uid") or 0)
    action = request.form.get("action")
    target = db.get_user(user_id=uid)
    if not target:
        flash("Compte introuvable.", "ko")
        return redirect(url_for("admin"))
    last_admin = target["role"] == "admin" and db.count_admins() <= 1
    if action == "toggle":
        if target["active"] and last_admin:
            flash("Impossible : c'est le dernier administrateur actif.", "ko")
        else:
            db.update_user(uid, active=0 if target["active"] else 1)
            flash("Compte « %s » %s." % (target["username"],
                  "desactive" if target["active"] else "reactive"), "ok")
    elif action == "reset":
        alpha = "abcdefghijkmnopqrstuvwxyzABCDEFGHJKLMNPQRSTUVWXYZ23456789"
        pwd = "".join(secrets.choice(alpha) for _ in range(16))
        db.set_password(uid, pwd)
        flash("Nouveau mot de passe pour « %s » : %s" % (target["username"], pwd), "ok")
    elif action == "delete":
        if last_admin:
            flash("Impossible : c'est le dernier administrateur actif.", "ko")
        elif uid == request.user["id"]:
            flash("Vous ne pouvez pas supprimer votre propre compte.", "ko")
        else:
            db.delete_user(uid)
            flash("Compte « %s » supprime." % target["username"], "ok")
    return redirect(url_for("admin"))


# ------------------------------------------------------------------------ API
@app.route("/api/jobs")
@login_required
def api_jobs():
    scope_all = bool(request.args.get("all")) and request.user["role"] == "admin"
    return jsonify(db.list_jobs(None if scope_all else request.user["id"]))


@app.route("/api/crawl", methods=["POST"])
@login_required
def api_crawl():
    check_csrf()
    u = request.user
    url = (request.form.get("url") or "").strip()
    if not url:
        return jsonify(error="URL manquante."), 400
    if not url.startswith("http"):
        url = "https://" + url
    if db.running_count(u["id"]) >= u["max_parallel"]:
        return jsonify(error="Vous avez deja %d crawl(s) en cours." % u["max_parallel"]), 429
    if db.running_count() >= MAX_GLOBAL:
        return jsonify(error="Le serveur traite deja %d crawls. Reessayez dans un instant."
                             % MAX_GLOBAL), 429

    jid = uuid.uuid4().hex[:10]
    gsc_path = None
    up = request.files.get("gsc")
    if up and up.filename:
        os.makedirs(os.path.join(OUT, "uploads"), exist_ok=True)
        ext = os.path.splitext(up.filename)[1].lower()
        ext = ext if ext in (".csv", ".zip", ".txt", ".tsv") else ".csv"
        gsc_path = os.path.join(OUT, "uploads", jid + ext)
        up.save(gsc_path)

    params = {
        "max_pages": max(1, min(u["max_pages"], int(request.form.get("max_pages") or 500))),
        "threads": max(1, min(16, int(request.form.get("threads") or 8))),
        "max_depth": max(1, min(50, int(request.form.get("max_depth") or 15))),
        "delay": max(0.0, min(5.0, float(request.form.get("delay") or 0))),
        "exclude_re": (request.form.get("exclude") or "").strip() or None,
    }
    db.create_job(jid, u["id"], url, params, gsc_path)
    subprocess.Popen([sys.executable, "-m", "webengine.runner", jid],
                     cwd=os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                     env=dict(os.environ), start_new_session=True,
                     stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    return jsonify(id=jid)


@app.route("/api/cancel/<jid>", methods=["POST"])
@login_required
def api_cancel(jid):
    check_csrf()
    job = db.get_job(jid)
    if not job or (job["user_id"] != request.user["id"] and request.user["role"] != "admin"):
        return jsonify(error="Crawl introuvable."), 404
    db.update_job(jid, cancel=1, message="Annulation demandee…")
    if job["pid"]:
        try:
            os.kill(job["pid"], signal.SIGTERM)
        except OSError:
            pass
    return jsonify(ok=True)


@app.route("/api/status/<jid>")
@login_required
def api_status(jid):
    job = db.get_job(jid)
    if not job or (job["user_id"] != request.user["id"] and request.user["role"] != "admin"):
        return jsonify(error="Crawl introuvable."), 404
    return jsonify(job)


@app.route("/rapport/<jid>")
@login_required
def rapport(jid):
    job = db.get_job(jid)
    if not job or (job["user_id"] != request.user["id"] and request.user["role"] != "admin"):
        abort(404)
    if not job["report"] or not os.path.isfile(job["report"]):
        abort(404)
    return send_file(job["report"])


# ------------------------------------------------------ suivi d'indexation
def _quand(ts):
    return time.strftime("%d/%m/%Y %H:%M", time.localtime(ts)) if ts else "—"


@app.route("/indexation", methods=["GET", "POST"])
@login_required
def indexation():
    u = request.user
    if request.method == "POST":
        check_csrf()
        from .gsc_index import analyse, croiser_urls, parse_export
        up = request.files.get("export")
        if not up or not up.filename:
            flash("Aucun fichier fourni.", "ko")
            return redirect(url_for("indexation"))
        # Le nom du fichier porte le motif ("Introuvable (404).csv") : on le conserve,
        # c'est lui qui permet de classer les URL quand l'export n'a pas de colonne motif.
        d = os.path.join(OUT, "uploads", uuid.uuid4().hex[:8])
        os.makedirs(d, exist_ok=True)
        name = secure_filename(up.filename) or "export.csv"
        if not name.lower().endswith((".zip", ".csv", ".tsv")):
            name += ".csv"
        tmp = os.path.join(d, name)
        up.save(tmp)
        try:
            parsed = parse_export(tmp)
        except Exception as exc:
            flash("Fichier illisible (%s). Attendu : l'export du rapport Pages." % type(exc).__name__, "ko")
            import shutil
            shutil.rmtree(os.path.dirname(tmp), ignore_errors=True)
            return redirect(url_for("indexation"))
        finally:
            pass

        if not (parsed["courbe"] or parsed["motifs"] or parsed["urls"]):
            flash("Rien de reconnu dans ce fichier. Colonnes vues : %s"
                  % " | ".join("%s (%s)" % (f["nom"], ", ".join(f["colonnes"][:4]))
                               for f in parsed["fichiers"]), "ko")
            import shutil
            shutil.rmtree(os.path.dirname(tmp), ignore_errors=True)
            return redirect(url_for("indexation"))

        site = (request.form.get("site") or "").strip()
        if not site and parsed["urls"]:
            from urllib.parse import urlsplit
            site = urlsplit(parsed["urls"][0]["url"]).netloc
        site = site or "sans nom"

        prev = db.last_index_snapshot(u["id"], site)
        import json as _json
        res = analyse(parsed, _json.loads(prev["motifs"]) if prev else None)

        job = db.last_crawl_for_host(u["id"], site, u["role"] == "admin") if parsed["urls"] else None
        if job and job.get("crawl_file") and os.path.isfile(job["crawl_file"]):
            from .store import load
            try:
                res = croiser_urls(res, load(job["crawl_file"]))
                res["resume"]["crawl_job"] = job["id"]
                res["resume"]["crawl_quand"] = _quand(job["finished_at"])
            except Exception:
                pass

        sid = db.add_index_snapshot(u["id"], site, res)
        try:
            import shutil
            shutil.rmtree(os.path.dirname(tmp), ignore_errors=True)
        except OSError:
            pass
        n = len([a for a in res["alertes"] if a["gravite"] in ("critique", "eleve")])
        flash("Import analyse : %d point(s) d'alerte." % n if n else "Import analyse, rien d'anormal.",
              "ko" if n else "ok")
        return redirect(url_for("indexation_detail", sid=sid))

    import json as _json
    hist = db.list_index_snapshots(u["id"])
    for h in hist:
        h["quand"] = _quand(h["imported_at"])
        h["n_alertes"] = len([a for a in _json.loads(h["alertes"] or "[]")
                              if a["gravite"] in ("critique", "eleve")])
    sites = db.index_sites(u["id"])
    return page(IDX_BODY, "Indexation", u, historique=hist,
                dernier_site=sites[0]["site"] if sites else "")


@app.route("/indexation/<int:sid>")
@login_required
def indexation_detail(sid):
    import json as _json
    snap = db.get_index_snapshot(sid, request.user["id"], request.user["role"] == "admin")
    if not snap:
        abort(404)
    r = _json.loads(snap["resume"] or "{}")
    courbe = _json.loads(snap["courbe"] or "[]")
    motifs = _json.loads(snap["motifs"] or "[]")
    alertes = _json.loads(snap["alertes"] or "[]")
    urls = _json.loads(snap["urls"] or "[]")
    prev = db.last_index_snapshot(snap["user_id"], snap["site"], before=sid)
    return page(IDX_DETAIL, "Indexation — %s" % snap["site"], request.user,
                snap=snap, r=r, courbe=courbe, motifs=motifs, alertes=alertes, urls=urls,
                quand=_quand(snap["imported_at"]),
                precedent=bool(prev), precedent_quand=_quand(prev["imported_at"]) if prev else "",
                croise=any("statut_reel" in u for u in urls),
                crawl_quand=r.get("crawl_quand", ""),
                courbe_json=_json.dumps(courbe), saut_json=_json.dumps(r.get("saut")))


def run(host="127.0.0.1", port=5005, open_browser=True):
    url = "http://%s:%d" % (host, port)
    print("⚙️  WebEngine Crawler — interface web sur %s" % url)
    print("    Rapports enregistres dans %s" % OUT)
    if auth_required():
        print("    Comptes actives : connexion requise.")
    if open_browser:
        import threading
        threading.Timer(1.0, lambda: webbrowser.open(url)).start()
    app.run(host=host, port=port, threaded=True, debug=False)
