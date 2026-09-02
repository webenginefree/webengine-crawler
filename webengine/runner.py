# -*- coding: utf-8 -*-
"""Execution d'un crawl dans un process dedie.

Lance par l'interface web : python -m webengine.runner <job_id>

L'isolation par process est volontaire : un crawl qui plante, sature la memoire
ou part en boucle ne peut pas emporter l'interface ni les crawls des autres
comptes. Le process est plafonne en memoire et desprioritise (nice).
"""
from __future__ import annotations

import json
import os
import resource
import signal
import sys
import time

from . import db
from .crawler import Crawler, DEFAULT_UA
from .report import build_data, export_csv, render_html

MEM_MB = int(os.environ.get("WEBENGINE_JOB_MEM_MB", "1024"))
NICE = int(os.environ.get("WEBENGINE_JOB_NICE", "5"))


def _limit_self():
    try:
        soft = MEM_MB * 1024 * 1024
        resource.setrlimit(resource.RLIMIT_AS, (soft, soft))
    except (ValueError, OSError):
        pass
    try:
        os.nice(NICE)
    except OSError:
        pass


def user_dir(user_id):
    base = os.path.join(os.environ.get("WEBENGINE_OUT", "webengine-rapports"),
                        "users", str(user_id))
    os.makedirs(base, exist_ok=True)
    return base


def run(jid):
    job = db.get_job(jid)
    if not job:
        print("job inconnu: %s" % jid, file=sys.stderr)
        return 2
    _limit_self()
    db.update_job(jid, state="running", pid=os.getpid(), message="Demarrage…")

    params = json.loads(job["params"] or "{}")
    stop = {"flag": False}

    def _term(*_a):
        stop["flag"] = True
    signal.signal(signal.SIGTERM, _term)

    crawler = None
    last = [0.0]

    def progress(crawled=0, queued=0, url="", **kw):
        now = time.time()
        if now - last[0] < 1.5:
            return
        last[0] = now
        db.update_job(jid, pct=min(99.0, crawled / max(1, params["max_pages"]) * 100),
                      crawled=crawled,
                      message="%d URL crawlees · %d en file · %s" % (crawled, queued, url[-70:]))
        fresh = db.get_job(jid)
        if stop["flag"] or (fresh and fresh["cancel"]):
            if crawler:
                crawler.cancel()

    try:
        crawler = Crawler(job["url"], progress=progress, user_agent=DEFAULT_UA,
                          max_pages=params["max_pages"], threads=params["threads"],
                          max_depth=params["max_depth"], delay=params.get("delay", 0.0),
                          exclude_re=params.get("exclude_re") or None,
                          include_re=params.get("include_re") or None,
                          respect_robots=params.get("respect_robots", True))
        result = crawler.run()

        if db.get_job(jid)["cancel"] or stop["flag"]:
            db.update_job(jid, state="cancelled", message="Crawl annule.",
                          finished_at=time.time(), pct=100)
            return 0

        items = None
        if job["gsc_path"] and os.path.exists(job["gsc_path"]):
            from .gsc import cross, load_gsc
            db.update_job(jid, message="Croisement Search Console…")
            items = cross(result, load_gsc(job["gsc_path"]))

        db.update_job(jid, message="Generation du rapport…")
        data = build_data(result, items)
        host = result.host.replace(":", "_")
        base = "rapport-%s-%s" % (host, time.strftime("%Y%m%d-%H%M%S"))
        out = user_dir(job["user_id"])
        report = os.path.join(out, base + ".html")
        render_html(data, report)
        csv_dir = os.path.join(out, base + "-csv")
        export_csv(data, csv_dir)
        crawl_file = os.path.join(out, base + ".json.gz")
        try:
            from .store import save
            save(result, crawl_file)          # sert au croisement avec la Search Console
        except Exception:
            crawl_file = None

        db.update_job(jid, state="done", pct=100, report=report, csv_dir=csv_dir,
                      crawl_file=crawl_file,
                      crawled=data["resume"]["total"], finished_at=time.time(),
                      message="%d URL analysees, %d erreur(s), %d groupe(s) de H1 en double."
                              % (data["resume"]["total"], data["resume"]["erreurs_4xx"]
                                 + data["resume"]["erreurs_5xx"], data["resume"]["h1_dupliques"]))
        return 0
    except MemoryError:
        db.update_job(jid, state="error", finished_at=time.time(),
                      message="Crawl interrompu : limite memoire de %d Mo atteinte." % MEM_MB)
        return 1
    except Exception as exc:
        db.update_job(jid, state="error", finished_at=time.time(),
                      message="%s: %s" % (type(exc).__name__, str(exc)[:200]))
        return 1
    finally:
        if job["gsc_path"] and os.path.exists(job["gsc_path"]):
            try:
                os.remove(job["gsc_path"])
            except OSError:
                pass


if __name__ == "__main__":
    if len(sys.argv) != 2:
        print("usage: python -m webengine.runner <job_id>", file=sys.stderr)
        sys.exit(2)
    sys.exit(run(sys.argv[1]))
