# -*- coding: utf-8 -*-
"""Detection des pages rendues cote client.

Un crawler qui lit le HTML brut ne voit rien d'un site rendu en JavaScript :
pas de H1, pas de liens, trois mots de contenu. Le piege est qu'il ne s'en rend
pas compte et rapporte « H1 manquant, contenu pauvre » avec aplomb.

Ce module ne rend pas le JavaScript : il repere les pages ou l'analyse est
probablement fausse, pour qu'on previenne au lieu de mentir.
"""
from __future__ import annotations

import re

# Conteneurs applicatifs classiques, vides tant que le JS n'a pas tourne
ROOT_IDS = {"root", "app", "__next", "__nuxt", "q-app", "application", "main-app",
            "svelte", "vue-app", "react-root", "ember-app"}
ROOT_TAGS = {"app-root", "ion-app"}

# Signatures de frameworks : (motif, nom, rendu_serveur)
SIGNATURES = [
    (re.compile(r"__NEXT_DATA__"), "Next.js", None),
    (re.compile(r"window\.__NUXT__"), "Nuxt", None),
    (re.compile(r"window\.__INITIAL_STATE__|__PRELOADED_STATE__"), "SPA (etat initial)", None),
    (re.compile(r"\bng-version\b"), "Angular", False),
    (re.compile(r"data-reactroot"), "React", True),
    (re.compile(r"data-server-rendered"), "Vue (SSR)", True),
    (re.compile(r"/_next/static/"), "Next.js", None),
    (re.compile(r"/_nuxt/"), "Nuxt", None),
    (re.compile(r"chunk-vendors|polyfills[.-][0-9a-f]{6,}\.js|runtime[.-][0-9a-f]{6,}\.js"),
     "bundle applicatif", None),
    (re.compile(r"gatsby|/_gatsby/"), "Gatsby", None),
    (re.compile(r"sveltekit|__sveltekit"), "SvelteKit", None),
]
NOSCRIPT_JS = re.compile(r"activer\s+(le\s+)?javascript|enable\s+javascript|javascript\s+est\s+"
                         r"(desactive|requis)|requires\s+javascript", re.I)

SEUIL = 60          # a partir de ce score, on considere l'analyse peu fiable


def signaux_dom(soup):
    """Constats qui doivent etre releves AVANT le retrait des <script>/<noscript>."""
    vide = None
    for el in soup.find_all(True, id=True):
        if str(el.get("id")).lower() in ROOT_IDS and len(el.find_all(True, recursive=True)) < 3:
            vide = "#" + el.get("id")
            break
    if not vide:
        for t in ROOT_TAGS:
            el = soup.find(t)
            if el is not None and len(el.find_all(True, recursive=True)) < 3:
                vide = "<%s>" % t
                break
    ns = soup.find("noscript")
    return {"conteneur_vide": vide,
            "noscript_js": bool(ns is not None
                                and NOSCRIPT_JS.search(ns.get_text(" ", strip=True) or ""))}


def analyser(dom, html_text, word_count, links_internal, taille):
    """Retourne (score 0-100, liste de constats, framework devine).

    `dom` vient de signaux_dom(), releve sur le document intact.
    """
    score, signaux, framework, ssr = 0, [], "", False

    vide = dom.get("conteneur_vide")
    if vide:
        score += 45
        signaux.append("conteneur applicatif vide (%s)" % vide)

    # --- signatures
    for motif, nom, sig_ssr in SIGNATURES:
        if motif.search(html_text):
            framework = framework or nom
            if sig_ssr:
                ssr = True
            break

    # --- contenu et maillage anormalement pauvres
    if word_count < 50:
        score += 25
        signaux.append("%d mot(s) de contenu" % word_count)
    elif word_count < 120:
        score += 12
        signaux.append("contenu tres court (%d mots)" % word_count)
    if links_internal == 0:
        score += 20
        signaux.append("aucun lien interne dans le HTML")
    elif links_internal < 3:
        score += 8
        signaux.append("seulement %d lien(s) interne(s)" % links_internal)

    # --- HTML lourd mais presque sans texte : tout est dans le JS
    if taille > 40000 and word_count < 150:
        score += 15
        signaux.append("%d Ko de HTML pour %d mots" % (taille // 1024, word_count))

    # --- le site le dit lui-meme
    if dom.get("noscript_js"):
        score += 20
        signaux.append("balise <noscript> demandant d'activer JavaScript")

    if framework and score:
        score += 10
        signaux.append("signature %s detectee" % framework)

    # --- un marqueur de rendu serveur rend le doute beaucoup moins probable
    if ssr and word_count > 120:
        score = max(0, score - 35)
        signaux.append("marqueur de rendu serveur present")

    return min(100, score), signaux, framework


def resume_site(pages):
    """Verdict a l'echelle du site : part des pages HTML suspectes."""
    html200 = [p for p in pages if getattr(p, "is_html", False) and p.status == 200]
    if not html200:
        return {"concernees": 0, "total": 0, "part": 0.0, "framework": "", "verdict": ""}
    touchees = [p for p in html200 if getattr(p, "js_risk", 0) >= SEUIL]
    part = len(touchees) / len(html200)
    frameworks = [p.js_framework for p in touchees if getattr(p, "js_framework", "")]
    fw = max(set(frameworks), key=frameworks.count) if frameworks else ""
    if part >= 0.6:
        verdict = "site tres probablement rendu cote client"
    elif part >= 0.25:
        verdict = "une partie du site est rendue cote client"
    elif touchees:
        verdict = "quelques pages rendues cote client"
    else:
        verdict = ""
    return {"concernees": len(touchees), "total": len(html200), "part": round(part, 3),
            "framework": fw, "verdict": verdict}
