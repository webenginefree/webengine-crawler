# -*- coding: utf-8 -*-
"""Detection des problemes SEO a partir d'un crawl."""
from __future__ import annotations

from collections import defaultdict

# Seuils (modifiables)
TITLE_MAX = 65
TITLE_MIN = 30
DESC_MAX = 160
DESC_MIN = 70
H1_MAX = 70
THIN_CONTENT = 200
SLOW = 1.5
DEEP = 4
URL_MAX = 115

SEV_ORDER = {"critique": 0, "eleve": 1, "moyen": 2, "info": 3}


def _norm(s):
    return (s or "").strip().lower()


def duplicate_groups(pages, key, min_len=1):
    """Regroupe les pages indexables partageant la meme valeur."""
    buckets = defaultdict(list)
    for url, p in pages.items():
        if not p.is_html or p.status != 200:
            continue
        vals = key(p)
        if isinstance(vals, str):
            vals = [vals] if vals else []
        for v in vals:
            v = _norm(v)
            if len(v) >= min_len:
                buckets[v].append(url)
    groups = [{"valeur": v, "count": len(us), "urls": sorted(us)}
              for v, us in buckets.items() if len(us) > 1]
    groups.sort(key=lambda g: -g["count"])
    return groups


def analyze(result):
    pages = result.pages
    inlinks = result.inlinks
    issues = []

    def add(iid, label, severity, urls, help_txt="", extra=None):
        urls = sorted(set(urls))
        if not urls:
            return
        issues.append({"id": iid, "label": label, "severite": severity,
                       "count": len(urls), "urls": urls, "aide": help_txt,
                       "extra": extra or {}})

    html200 = {u: p for u, p in pages.items() if p.is_html and p.status == 200}

    # ------------------------------------------------------------- statuts
    add("erreur_connexion", "Erreurs de connexion (timeout, DNS)", "critique",
        [u for u, p in pages.items() if p.status == 0],
        "L'URL n'a pas repondu. Verifiez le serveur ou l'orthographe du lien.")
    add("http_4xx", "Pages en erreur 4xx (404, 403, 410...)", "critique",
        [u for u, p in pages.items() if 400 <= p.status < 500],
        "Liens casses : corrigez ou redirigez. La colonne 'liens entrants' indique les pages a modifier.")
    add("http_5xx", "Erreurs serveur 5xx", "critique",
        [u for u, p in pages.items() if p.status >= 500],
        "Le serveur plante sur ces URL.")
    add("redirect_301", "Redirections 301 (permanentes)", "moyen",
        [u for u, p in pages.items() if p.status == 301],
        "Mettez a jour les liens internes pour pointer directement sur la cible.")
    add("redirect_302", "Redirections temporaires (302, 307)", "eleve",
        [u for u, p in pages.items() if p.status in (302, 307)],
        "Une 302 ne transmet pas le signal de permanence : preferez une 301 si le changement est definitif.")

    # chaines et boucles de redirection
    chains, loops = [], []
    for u, p in pages.items():
        if not p.redirect_to:
            continue
        seen, cur, hops = [u], p.redirect_to, 0
        while cur and hops < 8:
            hops += 1
            if cur in seen:
                loops.append(u)
                break
            seen.append(cur)
            nxt = pages.get(cur)
            if not nxt or not nxt.redirect_to:
                break
            cur = nxt.redirect_to
        if hops >= 2 and u not in loops:
            chains.append(u)
    add("chaine_redirection", "Chaines de redirection (2 sauts ou plus)", "moyen", chains,
        "Chaque saut fait perdre du temps de crawl et un peu de jus.")
    add("boucle_redirection", "Boucles de redirection", "critique", loops)

    # liens internes vers des URL cassees / redirigees
    to_broken = {u for u, p in pages.items() if p.status >= 400 or p.status == 0}
    src_broken = sorted({l["from"] for u in to_broken for l in inlinks.get(u, [])})
    add("liens_vers_erreur", "Pages contenant des liens vers des URL en erreur", "critique",
        src_broken, "Ce sont les pages a corriger pour supprimer les 404.")
    to_redir = {u for u, p in pages.items() if 300 <= p.status < 400}
    src_redir = sorted({l["from"] for u in to_redir for l in inlinks.get(u, [])
                        if l["type"] == "lien"})
    add("liens_vers_redirection", "Pages liant vers une redirection", "moyen", src_redir)

    # ----------------------------------------------------------------- H1
    add("h1_manquant", "H1 manquant", "eleve",
        [u for u, p in html200.items() if not [h for h in p.h1 if h]],
        "Chaque page doit avoir un H1 unique qui decrit son sujet.")
    add("h1_multiple", "Plusieurs H1 sur la page", "moyen",
        [u for u, p in html200.items() if len([h for h in p.h1 if h]) > 1])
    add("h1_vide", "H1 present mais vide", "moyen",
        [u for u, p in html200.items() if p.h1 and not any(h.strip() for h in p.h1)])
    add("h1_long", "H1 trop long (> %d caracteres)" % H1_MAX, "info",
        [u for u, p in html200.items() if any(len(h) > H1_MAX for h in p.h1)])

    dup_h1 = duplicate_groups(html200, lambda p: p.h1, min_len=1)
    add("h1_duplique", "H1 en double (meme H1 sur plusieurs pages)", "eleve",
        [u for g in dup_h1 for u in g["urls"]],
        "Deux pages avec le meme H1 se cannibalisent : differenciez-les ou fusionnez-les.",
        {"groupes": len(dup_h1)})

    # --------------------------------------------------------------- Title
    add("title_manquant", "Balise title manquante ou vide", "critique",
        [u for u, p in html200.items() if not p.title])
    add("title_long", "Title trop long (> %d car.)" % TITLE_MAX, "moyen",
        [u for u, p in html200.items() if len(p.title) > TITLE_MAX])
    add("title_court", "Title trop court (< %d car.)" % TITLE_MIN, "info",
        [u for u, p in html200.items() if 0 < len(p.title) < TITLE_MIN])
    dup_title = duplicate_groups(html200, lambda p: p.title)
    add("title_duplique", "Titles en double", "eleve",
        [u for g in dup_title for u in g["urls"]],
        "Un title unique par page, sinon Google choisit lui-meme la page a afficher.",
        {"groupes": len(dup_title)})
    add("title_egal_h1", "Title strictement identique au H1", "info",
        [u for u, p in html200.items()
         if p.title and p.h1 and _norm(p.title) == _norm(p.h1[0])])

    # ------------------------------------------------------- meta description
    add("desc_manquante", "Meta description manquante", "moyen",
        [u for u, p in html200.items() if not p.meta_description])
    add("desc_longue", "Meta description trop longue (> %d car.)" % DESC_MAX, "info",
        [u for u, p in html200.items() if len(p.meta_description) > DESC_MAX])
    add("desc_courte", "Meta description trop courte (< %d car.)" % DESC_MIN, "info",
        [u for u, p in html200.items() if 0 < len(p.meta_description) < DESC_MIN])
    dup_desc = duplicate_groups(html200, lambda p: p.meta_description, min_len=10)
    add("desc_dupliquee", "Meta descriptions en double", "moyen",
        [u for g in dup_desc for u in g["urls"]], "", {"groupes": len(dup_desc)})

    # ---------------------------------------------------- indexabilite / canon
    add("noindex", "Pages en noindex", "eleve",
        [u for u, p in pages.items() if "noindex" in
         ("%s %s" % (p.meta_robots, p.x_robots)).lower()],
        "Verifiez que ce n'est pas une erreur : ces pages ne peuvent pas etre positionnees.")
    add("canonical_manquant", "Canonical absente", "info",
        [u for u, p in html200.items() if not p.canonical])
    add("canonicalisee", "Page canonicalisee vers une autre URL", "moyen",
        [u for u, p in html200.items() if p.canonical and p.canonical != u])
    bad_canon = []
    for u, p in html200.items():
        if p.canonical and p.canonical in pages:
            tgt = pages[p.canonical]
            if tgt.status != 200:
                bad_canon.append(u)
    add("canonical_cassee", "Canonical pointant vers une URL non 200", "eleve", bad_canon)

    # ------------------------------------------------------------- contenu
    dup_content = duplicate_groups(html200, lambda p: p.content_hash or "", min_len=8)
    add("contenu_duplique", "Contenu strictement identique sur plusieurs URL", "eleve",
        [u for g in dup_content for u in g["urls"]], "", {"groupes": len(dup_content)})
    add("contenu_pauvre", "Contenu pauvre (< %d mots)" % THIN_CONTENT, "moyen",
        [u for u, p in html200.items() if p.word_count < THIN_CONTENT])
    add("images_sans_alt", "Pages avec des images sans attribut alt", "info",
        [u for u, p in html200.items() if p.images_no_alt > 0])
    add("lente", "Pages lentes (> %ss de reponse)" % SLOW, "moyen",
        [u for u, p in pages.items() if p.response_time > SLOW])
    add("profonde", "Pages a plus de %d clics de la home" % DEEP, "info",
        [u for u, p in html200.items() if p.depth > DEEP],
        "Plus une page est profonde, moins elle est crawlee. Remontez-la dans le maillage.")
    add("url_longue", "URL trop longue (> %d car.)" % URL_MAX, "info",
        [u for u in html200 if len(u) > URL_MAX])
    add("sans_lien_entrant", "Pages sans aucun lien entrant interne", "eleve",
        [u for u, p in html200.items()
         if u != result.start_url and not [l for l in inlinks.get(u, []) if l["type"] == "lien"]],
        "Pages orphelines : accessibles par le sitemap ou une redirection, mais liees nulle part.")

    # ---------------------------------------------------------------- sitemap
    crawled = set(pages)
    add("sitemap_orpheline", "URL du sitemap jamais rencontree dans le maillage", "moyen",
        [u for u in result.sitemap_urls
         if u in crawled and not [l for l in inlinks.get(u, []) if l["type"] == "lien"]
         and u != result.start_url])
    add("sitemap_non_200", "URL du sitemap qui ne renvoie pas 200", "eleve",
        [u for u in result.sitemap_urls if u in crawled and pages[u].status != 200])

    # liens externes casses (si verifies)
    ext_broken = [u for u, d in result.external.items()
                  if d.get("status") is not None and (d["status"] >= 400 or d["status"] == 0)]
    add("lien_externe_casse", "Liens sortants casses", "moyen", ext_broken)

    issues.sort(key=lambda i: (SEV_ORDER.get(i["severite"], 9), -i["count"]))

    return {
        "issues": issues,
        "doublons": {
            "h1": dup_h1,
            "title": dup_title,
            "description": dup_desc,
            "contenu": [dict(g, valeur="(contenu identique)") for g in dup_content],
        },
    }


def summary(result, analysis):
    pages = result.pages
    html200 = [p for p in pages.values() if p.is_html and p.status == 200]
    counts = defaultdict(int)
    for p in pages.values():
        if p.status == 0:
            counts["erreur"] += 1
        elif p.status < 300:
            counts["ok"] += 1
        elif p.status < 400:
            counts["redirection"] += 1
        elif p.status < 500:
            counts["client"] += 1
        else:
            counts["serveur"] += 1
    return {
        "total": len(pages),
        "ok": counts["ok"],
        "redirections": counts["redirection"],
        "erreurs_4xx": counts["client"],
        "erreurs_5xx": counts["serveur"] + counts["erreur"],
        "indexables": sum(1 for p in pages.values() if p.indexable),
        "h1_dupliques": len(analysis["doublons"]["h1"]),
        "titles_dupliques": len(analysis["doublons"]["title"]),
        "sans_h1": sum(1 for p in html200 if not [h for h in p.h1 if h]),
        "profondeur_max": max([p.depth for p in pages.values()] or [0]),
        "mots_moyen": int(sum(p.word_count for p in html200) / max(1, len(html200))),
        "temps_moyen": round(sum(p.response_time for p in pages.values()) / max(1, len(pages)), 2),
        "duree": result.duration,
        "sitemap": len(result.sitemap_urls),
        "externes": len(result.external),
        "bloquees_robots": len(result.robots_blocked),
    }
