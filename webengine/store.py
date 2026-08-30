# -*- coding: utf-8 -*-
"""Sauvegarde / rechargement d'un crawl (pour rejouer un croisement GSC sans recrawler)."""
from __future__ import annotations

import gzip
import json

from .crawler import CrawlResult, Page


def save(result, path):
    payload = {
        "start_url": result.start_url,
        "host": result.host,
        "started": result.started,
        "finished": result.finished,
        "pages": {u: p.as_dict() for u, p in result.pages.items()},
        "inlinks": result.inlinks,
        "outlinks": {k: list(dict.fromkeys(v)) for k, v in result.outlinks.items()},
        "external": result.external,
        "sitemap_urls": sorted(result.sitemap_urls),
        "robots_blocked": sorted(result.robots_blocked),
    }
    op = gzip.open if path.endswith(".gz") else open
    with op(path, "wt", encoding="utf-8") as fh:
        json.dump(payload, fh, ensure_ascii=False)
    return path


def load(path):
    op = gzip.open if path.endswith(".gz") else open
    with op(path, "rt", encoding="utf-8") as fh:
        d = json.load(fh)
    r = CrawlResult(d["start_url"])
    r.host = d.get("host", r.host)
    r.started = d.get("started", 0)
    r.finished = d.get("finished", 0)
    for u, pd in d["pages"].items():
        p = Page(url=u)
        for k, v in pd.items():
            setattr(p, k, v)
        r.pages[u] = p
    r.inlinks = d.get("inlinks", {})
    r.outlinks = d.get("outlinks", {})
    r.external = d.get("external", {})
    r.sitemap_urls = set(d.get("sitemap_urls", []))
    r.robots_blocked = set(d.get("robots_blocked", []))
    return r
