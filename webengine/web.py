# -*- coding: utf-8 -*-
"""Petite interface web locale : on colle une URL, on clique, on lit le rapport."""
from __future__ import annotations

import os
import threading
import time
import traceback
import uuid
import webbrowser

from flask import Flask, jsonify, request, send_file, Response

from . import __version__
from .crawler import Crawler, DEFAULT_UA
from .report import build_data, export_csv, render_html

OUT = os.path.abspath(os.environ.get("WEBENGINE_OUT", "webengine-rapports"))
JOBS = {}
app = Flask(__name__)

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
<small>WebEngine Crawler __V__ — tout reste sur votre machine.</small>
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


@app.route("/")
def index():
    return Response(PAGE.replace("__V__", __version__), mimetype="text/html")


@app.route("/api/crawl", methods=["POST"])
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
        "max_pages": int(request.form.get("max_pages") or 500),
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
def api_status(jid):
    return jsonify(JOBS.get(jid, {"state": "error", "pct": 0, "message": "Job inconnu"}))


@app.route("/rapport/<jid>")
def rapport(jid):
    job = JOBS.get(jid)
    if not job or not job.get("file"):
        return "Rapport introuvable", 404
    return send_file(job["file"])


@app.route("/api/rapports")
def api_rapports():
    if not os.path.isdir(OUT):
        return jsonify([])
    files = [f for f in os.listdir(OUT) if f.endswith(".html")]
    files.sort(key=lambda f: os.path.getmtime(os.path.join(OUT, f)), reverse=True)
    return jsonify([{"f": f, "d": time.strftime("%d/%m %H:%M",
                    time.localtime(os.path.getmtime(os.path.join(OUT, f))))} for f in files[:15]])


@app.route("/fichier/<path:name>")
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
