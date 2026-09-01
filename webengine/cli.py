# -*- coding: utf-8 -*-
"""Interface en ligne de commande de WebEngine Crawler."""
from __future__ import annotations

import argparse
import os
import sys
import time
import webbrowser

from . import __version__
from .analyze import analyze, summary
from .crawler import Crawler, DEFAULT_UA
from .report import build_data, export_csv, render_html


class Ticker:
    """Barre de progression minimaliste."""
    def __init__(self, total):
        self.total = total
        self.last = 0
        self.t0 = time.time()

    def __call__(self, crawled=0, queued=0, url="", status=0, **kw):
        now = time.time()
        if now - self.last < 0.15 and crawled % 25:
            return
        self.last = now
        bar_w = 24
        frac = min(1.0, crawled / max(1, self.total))
        bar = "#" * int(frac * bar_w)
        rate = crawled / max(0.1, now - self.t0)
        sys.stderr.write("\r  [%-24s] %4d URL  file:%-4d  %4.1f/s  %s" % (
            bar, crawled, queued, rate, (url[-52:] if url else "")[:52].ljust(52)))
        sys.stderr.flush()

    def done(self):
        sys.stderr.write("\r" + " " * 118 + "\r")
        sys.stderr.flush()


def _out_paths(args, host):
    base = args.output or ("rapport-%s-%s.html" % (host.replace(":", "_"),
                                                   time.strftime("%Y%m%d-%H%M")))
    if not base.endswith(".html"):
        base += ".html"
    return base


def cmd_crawl(args):
    print("⚙️  WebEngine Crawler %s — crawl de %s" % (__version__, args.url), flush=True)
    tick = Ticker(args.max_pages)
    crawler = Crawler(
        args.url, max_pages=args.max_pages, max_depth=args.max_depth,
        threads=args.threads, delay=args.delay, user_agent=args.user_agent,
        timeout=args.timeout, include_subdomains=args.subdomains,
        respect_robots=not args.ignore_robots, include_re=args.include,
        exclude_re=args.exclude, use_sitemaps=not args.no_sitemap,
        check_external=args.check_external, progress=tick,
        auth=tuple(args.auth.split(":", 1)) if args.auth else None)
    result = crawler.run()
    tick.done()

    gsc_items = None
    if args.gsc:
        from .gsc import load_gsc, cross
        rows = load_gsc(args.gsc)
        print("   Search Console : %d URL importees, verification en cours…" % len(rows), flush=True)
        gsc_items = cross(result, rows, check_missing=not args.no_check_gsc,
                          user_agent=args.user_agent, threads=args.threads)

    data = build_data(result, gsc_items, params=vars(args))
    out = _out_paths(args, result.host)
    render_html(data, out)
    print_summary(data)
    print("\n📄  Rapport : %s" % os.path.abspath(out))

    if args.csv:
        files = export_csv(data, args.csv)
        print("📊  %d exports CSV dans %s/" % (len(files), os.path.abspath(args.csv)))
    if args.save:
        from .store import save
        save(result, args.save)
        print("💾  Crawl sauvegarde : %s" % os.path.abspath(args.save))
    if not args.no_open:
        try:
            webbrowser.open("file://" + os.path.abspath(out))
        except Exception:
            pass
    return 0


def cmd_gsc(args):
    from .gsc import load_gsc, cross
    from .store import load
    result = load(args.crawl)
    rows = load_gsc(args.csv_file)
    print("⚙️  %d URL Search Console croisees avec %d pages crawlees…"
          % (len(rows), len(result.pages)))
    items = cross(result, rows, check_missing=not args.no_check_gsc)
    data = build_data(result, items, params=vars(args))
    out = _out_paths(args, result.host)
    render_html(data, out)
    g = data["gsc"]["resume"]
    print("\n  URL saines .................. %d" % g["ok"])
    print("  URL cassees (4xx/5xx) ....... %d" % g["cassees"])
    print("  URL redirigees .............. %d" % g["redirigees"])
    print("  URL orphelines .............. %d" % g["orphelines"])
    print("  Clics sur URL a problemes ... %d" % g["clics_perdus"])
    print("\n📄  Rapport : %s" % os.path.abspath(out))
    if args.csv:
        export_csv(data, args.csv)
    if not args.no_open:
        try:
            webbrowser.open("file://" + os.path.abspath(out))
        except Exception:
            pass
    return 0


def cmd_hashpass(args):
    """Genere le hash a mettre dans WEBENGINE_PASSWORD_HASH."""
    import getpass
    import secrets as _secrets
    from werkzeug.security import generate_password_hash
    pwd = args.password
    if not pwd:
        pwd = getpass.getpass("Mot de passe (vide = en generer un) : ")
    if not pwd:
        alpha = "abcdefghijkmnopqrstuvwxyzABCDEFGHJKLMNPQRSTUVWXYZ23456789"
        pwd = "".join(_secrets.choice(alpha) for _ in range(20))
        print("Mot de passe genere : %s" % pwd)
    print("WEBENGINE_PASSWORD_HASH='%s'" % generate_password_hash(pwd))
    return 0


def cmd_users(args):
    """Gestion des comptes en ligne de commande (bootstrap, depannage)."""
    import getpass
    import secrets as _secrets
    import time as _time
    from . import db
    db.init()
    action = args.action

    def _gen():
        alpha = "abcdefghijkmnopqrstuvwxyzABCDEFGHJKLMNPQRSTUVWXYZ23456789"
        return "".join(_secrets.choice(alpha) for _ in range(16))

    if action == "list":
        users = db.list_users()
        if not users:
            print("Aucun compte. Le premier demarrage de l'interface web en proposera la creation.")
            return 0
        print("%-20s %-8s %-10s %8s %6s %8s  %s" % ("COMPTE", "ROLE", "ETAT", "URL/CRAWL",
                                                    "PARAL", "CRAWLS", "DERNIERE CONNEXION"))
        for u in users:
            last = (_time.strftime("%d/%m/%Y %H:%M", _time.localtime(u["last_login"]))
                    if u["last_login"] else "jamais")
            print("%-20s %-8s %-10s %8d %6d %8d  %s" % (
                u["username"], u["role"], "actif" if u["active"] else "desactive",
                u["max_pages"], u["max_parallel"], u["jobs_count"], last))
        return 0

    if not args.username:
        print("Nom d'utilisateur requis.")
        return 1

    if action == "add":
        pwd = args.password or getpass.getpass("Mot de passe (vide = genere) : ") or _gen()
        try:
            db.create_user(args.username, pwd, role="admin" if args.admin else "user",
                           max_pages=args.max_pages, max_parallel=args.parallel)
        except ValueError as exc:
            print("Erreur : %s" % exc)
            return 1
        print("Compte « %s » cree (%s)." % (args.username, "admin" if args.admin else "utilisateur"))
        print("Mot de passe : %s" % pwd)
        return 0

    u = db.get_user(username=args.username)
    if not u:
        print("Compte introuvable : %s" % args.username)
        return 1

    if action == "passwd":
        pwd = args.password or getpass.getpass("Nouveau mot de passe (vide = genere) : ") or _gen()
        db.set_password(u["id"], pwd)
        print("Mot de passe de « %s » modifie : %s" % (args.username, pwd))
    elif action in ("enable", "disable"):
        if action == "disable" and u["role"] == "admin" and db.count_admins() <= 1:
            print("Refus : c'est le dernier administrateur actif.")
            return 1
        db.update_user(u["id"], active=1 if action == "enable" else 0)
        print("Compte « %s » %s." % (args.username, "active" if action == "enable" else "desactive"))
    elif action == "delete":
        if u["role"] == "admin" and db.count_admins() <= 1:
            print("Refus : c'est le dernier administrateur actif.")
            return 1
        db.delete_user(u["id"])
        print("Compte « %s » supprime." % args.username)
    elif action == "admin":
        db.update_user(u["id"], role="admin")
        print("« %s » est desormais administrateur." % args.username)
    return 0


def cmd_serve(args):
    from .web import run
    run(host=args.host, port=args.port, open_browser=not args.no_open)
    return 0


def print_summary(data):
    r = data["resume"]
    print("\n  ── Resume " + "─" * 46)
    print("  URL crawlees ............ %d  (%d indexables)" % (r["total"], r["indexables"]))
    print("  200 OK .................. %d" % r["ok"])
    print("  Redirections 3xx ........ %d" % r["redirections"])
    print("  Erreurs 4xx ............. %d" % r["erreurs_4xx"])
    print("  Erreurs 5xx / KO ........ %d" % r["erreurs_5xx"])
    print("  Groupes de H1 en double . %d" % r["h1_dupliques"])
    print("  Pages sans H1 ........... %d" % r["sans_h1"])
    print("  Titles en double ........ %d" % r["titles_dupliques"])
    if data.get("gsc"):
        g = data["gsc"]["resume"]
        print("  GSC : %d URL — %d cassees, %d orphelines, %d hors crawl"
              % (g["total"], g["cassees"], g["orphelines"], g["hors_crawl"]))
    print("  " + "─" * 56)
    top = [i for i in data["issues"] if i["severite"] in ("critique", "eleve")][:6]
    if top:
        print("\n  A traiter en priorite :")
        for i in top:
            print("   • %-52s %5d URL" % (i["label"][:52], i["count"]))


def build_parser():
    p = argparse.ArgumentParser(
        prog="webengine", description="⚙️ WebEngine Crawler — crawler SEO local et gratuit "
                                 "(H1 en double, 404, liens entrants, Search Console).")
    p.add_argument("--version", action="version", version="WebEngine Crawler " + __version__)
    sub = p.add_subparsers(dest="cmd")

    c = sub.add_parser("crawl", help="crawler un site et generer le rapport")
    c.add_argument("url")
    c.add_argument("-n", "--max-pages", type=int, default=500, help="nb max d'URL (defaut 500)")
    c.add_argument("-d", "--max-depth", type=int, default=15, help="profondeur max (defaut 15)")
    c.add_argument("-t", "--threads", type=int, default=8, help="threads (defaut 8)")
    c.add_argument("--delay", type=float, default=0.0, help="pause entre requetes, en s")
    c.add_argument("--timeout", type=int, default=20)
    c.add_argument("-o", "--output", help="fichier HTML de sortie")
    c.add_argument("--csv", help="dossier ou exporter les CSV")
    c.add_argument("--save", help="sauvegarder le crawl (.json/.json.gz) pour le rejouer")
    c.add_argument("--gsc", help="export Search Console (.csv/.zip/.txt) a croiser")
    c.add_argument("--no-check-gsc", action="store_true",
                   help="ne pas verifier en direct les URL GSC absentes du crawl")
    c.add_argument("--include", help="regex : ne crawler que les URL qui matchent")
    c.add_argument("--exclude", help="regex : ignorer les URL qui matchent")
    c.add_argument("--subdomains", action="store_true", help="inclure les sous-domaines")
    c.add_argument("--ignore-robots", action="store_true", help="ignorer robots.txt")
    c.add_argument("--no-sitemap", action="store_true", help="ne pas lire les sitemaps")
    c.add_argument("--check-external", action="store_true", help="verifier les liens sortants")
    c.add_argument("--user-agent", default=DEFAULT_UA)
    c.add_argument("--auth", help="auth basique user:motdepasse")
    c.add_argument("--no-open", action="store_true", help="ne pas ouvrir le navigateur")
    c.set_defaults(func=cmd_crawl)

    g = sub.add_parser("gsc", help="croiser un crawl sauvegarde avec un export Search Console")
    g.add_argument("crawl", help="fichier .json(.gz) produit par --save")
    g.add_argument("csv_file", help="export Search Console")
    g.add_argument("-o", "--output")
    g.add_argument("--csv", help="dossier d'export CSV")
    g.add_argument("--no-check-gsc", action="store_true")
    g.add_argument("--no-open", action="store_true")
    g.set_defaults(func=cmd_gsc)

    us = sub.add_parser("users", help="gerer les comptes de l'interface web")
    us.add_argument("action", choices=["list", "add", "passwd", "enable", "disable",
                                       "delete", "admin"])
    us.add_argument("username", nargs="?")
    us.add_argument("--password", help="sinon demande, ou genere")
    us.add_argument("--admin", action="store_true", help="creer un administrateur")
    us.add_argument("--max-pages", type=int, default=1000, dest="max_pages")
    us.add_argument("--parallel", type=int, default=1, help="crawls simultanes autorises")
    us.set_defaults(func=cmd_users)

    hp = sub.add_parser("hashpass", help="generer le hash d'un mot de passe pour l'interface web")
    hp.add_argument("password", nargs="?", help="mot de passe (sinon demande, ou genere)")
    hp.set_defaults(func=cmd_hashpass)

    s = sub.add_parser("serve", help="lancer l'interface web locale")
    s.add_argument("-p", "--port", type=int, default=5005)
    s.add_argument("--host", default="127.0.0.1")
    s.add_argument("--no-open", action="store_true")
    s.set_defaults(func=cmd_serve)
    return p


def main(argv=None):
    parser = build_parser()
    args = parser.parse_args(argv)
    if not getattr(args, "func", None):
        parser.print_help()
        return 1
    try:
        return args.func(args)
    except KeyboardInterrupt:
        print("\nInterrompu.")
        return 130


if __name__ == "__main__":
    sys.exit(main())
