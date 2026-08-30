# -*- coding: utf-8 -*-
"""Construction des donnees et generation du rapport HTML autonome."""
from __future__ import annotations

import csv
import datetime as dt
import json
import os

from . import __version__
from .analyze import analyze, summary

TPL = os.path.join(os.path.dirname(os.path.abspath(__file__)), "report_template.html")
MAX_INLINKS = 150


def build_data(result, gsc_items=None, params=None):
    an = analyze(result)
    res = summary(result, an)

    inlinks = {}
    for url, links in result.inlinks.items():
        if url in result.pages or (gsc_items and any(g["url"] == url for g in gsc_items)):
            seen, keep = set(), []
            for l in links:
                key = (l["from"], l["anchor"])
                if key in seen:
                    continue
                seen.add(key)
                keep.append(l)
                if len(keep) >= MAX_INLINKS:
                    break
            inlinks[url] = keep

    outlinks = {}
    for src, targets in result.outlinks.items():
        outlinks[src] = list(dict.fromkeys(targets))[:200]

    data = {
        "meta": {
            "start_url": result.start_url,
            "host": result.host,
            "date": dt.datetime.now().strftime("%d/%m/%Y %H:%M"),
            "duree": result.duration,
            "version": __version__,
            "params": params or {},
        },
        "resume": res,
        "issues": an["issues"],
        "doublons": an["doublons"],
        "pages": [p.as_dict() for p in sorted(result.pages.values(),
                                              key=lambda x: (x.depth, x.url))],
        "inlinks": inlinks,
        "outlinks": outlinks,
        "externes": sorted(
            [{"url": u, **d} for u, d in result.external.items()],
            key=lambda x: -x["count"])[:2000],
        "sitemap": sorted(result.sitemap_urls),
        "gsc": None,
    }
    if gsc_items is not None:
        from .gsc import gsc_summary
        data["gsc"] = {"items": gsc_items, "resume": gsc_summary(gsc_items)}
    return data


def render_html(data, path):
    with open(TPL, encoding="utf-8") as fh:
        tpl = fh.read()
    payload = json.dumps(data, ensure_ascii=False, default=str)
    payload = payload.replace("</", "<\\/")   # ne casse pas la balise script
    title = "WebEngine Crawler - %s" % data["meta"]["host"]
    html = tpl.replace("__TITLE__", title).replace("__DATA__", payload)
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(html)
    return path


PAGE_COLS = [
    ("url", "URL"), ("status", "Statut"), ("indexability", "Indexabilite"),
    ("title", "Title"), ("meta_description", "Meta description"), ("h1", "H1"),
    ("canonical", "Canonical"), ("meta_robots", "Meta robots"),
    ("word_count", "Mots"), ("depth", "Profondeur"), ("links_internal", "Liens internes"),
    ("images_no_alt", "Images sans alt"), ("response_time", "Temps (s)"),
    ("size", "Poids"), ("redirect_to", "Redirige vers"),
]


def export_csv(data, outdir):
    os.makedirs(outdir, exist_ok=True)
    written = []

    def w(name, header, rows):
        p = os.path.join(outdir, name)
        with open(p, "w", newline="", encoding="utf-8-sig") as fh:
            wr = csv.writer(fh, delimiter=";")
            wr.writerow(header)
            wr.writerows(rows)
        written.append(p)

    inl = data["inlinks"]
    w("urls.csv", [c[1] for c in PAGE_COLS] + ["Liens entrants"],
      [[" | ".join(p[k]) if isinstance(p[k], list) else p[k] for k, _ in PAGE_COLS]
       + [len([l for l in inl.get(p["url"], []) if l["type"] == "lien"])]
       for p in data["pages"]])

    w("erreurs.csv", ["URL", "Statut", "Liens entrants", "Page source", "Ancre"],
      [[p["url"], p["status"], len(inl.get(p["url"], [])), l["from"], l["anchor"]]
       for p in data["pages"] if p["status"] >= 400 or p["status"] == 0
       for l in (inl.get(p["url"]) or [{"from": "", "anchor": ""}])])

    for key, name in (("h1", "h1_doubles.csv"), ("title", "titles_doubles.csv"),
                      ("description", "descriptions_doubles.csv")):
        w(name, ["Valeur", "Nb pages", "URL"],
          [[g["valeur"], g["count"], u] for g in data["doublons"][key] for u in g["urls"]])

    w("problemes.csv", ["Probleme", "Gravite", "URL"],
      [[i["label"], i["severite"], u] for i in data["issues"] for u in i["urls"]])

    w("liens_entrants.csv", ["URL cible", "Page source", "Ancre", "Type", "Rel"],
      [[t, l["from"], l["anchor"], l["type"], l["rel"]] for t, ls in inl.items() for l in ls])

    if data.get("gsc"):
        w("search_console.csv",
          ["URL", "Clics", "Impressions", "Position", "Statut", "Diagnostic",
           "Liens entrants", "Provenance", "Dans sitemap", "Exemple de page source"],
          [[i["url"], i["clics"], i["impressions"], i["position"], i["statut"], i["verdict"],
            i["liens_entrants"], i["provenance"], "oui" if i["dans_sitemap"] else "non",
            i["sources"][0]["from"] if i["sources"] else ""]
           for i in data["gsc"]["items"]])
    return written
