# -*- coding: utf-8 -*-
"""Import d'un export Search Console et croisement avec le crawl.

Repond a la question : "d'ou vient cette URL que je vois dans la Search Console ?"
-> quelles pages du site pointent vers elle, avec quelle ancre, est-elle dans le
sitemap, est-elle orpheline, et quel est son statut HTTP reel.
"""
from __future__ import annotations

import csv
import io
import os
import re
import threading
import zipfile

import requests

from .crawler import DEFAULT_UA, normalize_url

URL_COLS = ("url", "page", "adresse", "address", "landing", "lien", "top pages",
            "pages les plus", "principales pages")
NUM = re.compile(r"[^0-9.,-]")


def _num(v):
    if v is None:
        return 0
    s = NUM.sub("", str(v)).replace(" ", "").replace(" ", "")
    if not s:
        return 0
    if s.count(",") and s.count("."):
        s = s.replace(",", "")
    else:
        s = s.replace(",", ".")
    try:
        f = float(s)
        return int(f) if f.is_integer() else f
    except ValueError:
        return 0


def _open_rows(path):
    """Renvoie (entetes, lignes) depuis un csv/tsv/txt/zip d'export GSC."""
    if path.lower().endswith(".zip"):
        with zipfile.ZipFile(path) as z:
            names = z.namelist()
            pref = [n for n in names if re.search(r"page|url", n, re.I) and n.lower().endswith(".csv")]
            name = (pref or [n for n in names if n.lower().endswith(".csv")] or names)[0]
            data = z.read(name).decode("utf-8-sig", "replace")
    else:
        with open(path, "rb") as fh:
            data = fh.read().decode("utf-8-sig", "replace")

    sample = data[:4096]
    try:
        dialect = csv.Sniffer().sniff(sample, delimiters=",;\t")
        delim = dialect.delimiter
    except csv.Error:
        delim = "\t" if "\t" in sample else (";" if sample.count(";") > sample.count(",") else ",")
    reader = csv.reader(io.StringIO(data), delimiter=delim)
    rows = [r for r in reader if any((c or "").strip() for c in r)]
    if not rows:
        return [], []
    header = [(c or "").strip() for c in rows[0]]
    if any(h.lower().startswith(("http://", "https://")) for h in header):
        return [], rows          # simple liste d'URL sans entete
    return header, rows[1:]


def load_gsc(path):
    """Retourne une liste de dicts {url, clics, impressions, ctr, position}."""
    header, rows = _open_rows(path)
    lower = [h.lower() for h in header]

    def find(*keys):
        for i, h in enumerate(lower):
            if any(k in h for k in keys):
                return i
        return None

    iu = None
    for i, h in enumerate(lower):
        if any(k in h for k in URL_COLS):
            iu = i
            break
    if iu is None:
        iu = 0
        for r in rows[:20]:
            for i, c in enumerate(r):
                if (c or "").strip().lower().startswith("http"):
                    iu = i
                    break
            else:
                continue
            break

    ic = find("clic", "click")
    ii = find("impression")
    ir = find("ctr", "taux de clic")
    ip = find("position")

    out, seen = [], set()
    for r in rows:
        if iu >= len(r):
            continue
        raw = (r[iu] or "").strip()
        if not raw.lower().startswith("http"):
            continue
        url = normalize_url(raw)
        if not url or url in seen:
            continue
        seen.add(url)
        out.append({
            "url": url,
            "clics": _num(r[ic]) if ic is not None and ic < len(r) else 0,
            "impressions": _num(r[ii]) if ii is not None and ii < len(r) else 0,
            "ctr": _num(r[ir]) if ir is not None and ir < len(r) else 0,
            "position": _num(r[ip]) if ip is not None and ip < len(r) else 0,
        })
    return out


def _live_status(urls, user_agent=DEFAULT_UA, threads=8, timeout=15):
    """Verifie en direct le statut des URL GSC absentes du crawl."""
    res = {}
    lock = threading.Lock()
    sem = threading.Semaphore(threads)

    def check(u):
        with sem:
            status, final = 0, ""
            try:
                s = requests.Session()
                s.headers["User-Agent"] = user_agent
                r = s.get(u, timeout=timeout, allow_redirects=True, stream=True)
                status = r.status_code
                final = r.url if r.url != u else ""
                r.close()
            except requests.RequestException:
                status = 0
            with lock:
                res[u] = {"status": status, "final": final}

    ts = [threading.Thread(target=check, args=(u,), daemon=True) for u in urls]
    for t in ts:
        t.start()
    for t in ts:
        t.join(timeout=timeout + 5)
    return res


def cross(result, rows, check_missing=True, user_agent=DEFAULT_UA, threads=8, progress=None):
    """Croise les URL de la Search Console avec le crawl."""
    pages = result.pages
    inlinks = result.inlinks
    sitemap = result.sitemap_urls

    missing = [r["url"] for r in rows if r["url"] not in pages]
    live = {}
    if check_missing and missing:
        if progress:
            progress(phase="gsc", total=len(missing))
        live = _live_status(missing[:500], user_agent=user_agent, threads=threads)

    out = []
    for r in rows:
        u = r["url"]
        p = pages.get(u)
        links = [l for l in inlinks.get(u, []) if l["type"] == "lien"]
        autres = [l for l in inlinks.get(u, []) if l["type"] != "lien"]
        item = dict(r)
        item.update({
            "dans_crawl": p is not None,
            "statut": p.status if p else (live.get(u, {}).get("status", None)),
            "url_finale": (p.redirect_to if p and p.redirect_to else live.get(u, {}).get("final", "")),
            "indexabilite": p.indexability if p else "",
            "title": p.title if p else "",
            "h1": (p.h1[0] if p and p.h1 else ""),
            "profondeur": p.depth if p else None,
            "mots": p.word_count if p else None,
            "liens_entrants": len(links),
            "sources": links[:50],
            "autres_sources": autres[:20],
            "dans_sitemap": u in sitemap,
        })
        item["provenance"] = _provenance(item)
        item["verdict"], item["gravite"] = _verdict(item)
        out.append(item)

    out.sort(key=lambda x: (-(x["clics"] or 0), -(x["impressions"] or 0)))
    return out


def _provenance(it):
    src = []
    if it["liens_entrants"]:
        src.append("%d lien(s) interne(s)" % it["liens_entrants"])
    if it["dans_sitemap"]:
        src.append("sitemap.xml")
    for a in it["autres_sources"]:
        lab = {"redirection": "redirection", "canonical": "balise canonical"}.get(a["type"], a["type"])
        if lab not in src:
            src.append(lab)
    if not src:
        src.append("aucune source interne (orpheline)")
    return " + ".join(src)


def _verdict(it):
    st = it["statut"]
    if st is None:
        return "Non verifiee", "info"
    if st == 0:
        return "Injoignable", "critique"
    if st >= 500:
        return "Erreur serveur %d" % st, "critique"
    if st >= 400:
        return "Erreur %d : URL morte encore dans la Search Console" % st, "critique"
    if 300 <= st < 400:
        return "Redirection %d" % st, "eleve"
    if not it["dans_crawl"]:
        return "Orpheline : repond 200 mais aucun lien interne trouve", "eleve"
    if it["indexabilite"] and it["indexabilite"] != "Indexable":
        return "Non indexable : %s" % it["indexabilite"], "eleve"
    if it["liens_entrants"] == 0:
        return "Orpheline : atteinte uniquement via sitemap/redirection", "eleve"
    if it["profondeur"] is not None and it["profondeur"] > 4:
        return "Page profonde (%d clics)" % it["profondeur"], "moyen"
    return "OK", "ok"


def gsc_summary(items):
    return {
        "total": len(items),
        "ok": sum(1 for i in items if i["gravite"] == "ok"),
        "orphelines": sum(1 for i in items if i["liens_entrants"] == 0),
        "cassees": sum(1 for i in items if (i["statut"] or 0) >= 400 or i["statut"] == 0),
        "redirigees": sum(1 for i in items if i["statut"] and 300 <= i["statut"] < 400),
        "hors_crawl": sum(1 for i in items if not i["dans_crawl"]),
        "clics": sum(i["clics"] or 0 for i in items),
        "clics_perdus": sum((i["clics"] or 0) for i in items if i["gravite"] in ("critique", "eleve")),
    }
