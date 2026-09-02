# -*- coding: utf-8 -*-
"""Rapport d'indexation de la Search Console : lecture, historique, alertes.

L'export du rapport « Pages » (Indexation) contient plus que l'etat courant :
le fichier Chart.csv porte la courbe des ~3 derniers mois. On peut donc detecter
une hausse des pages non indexees des le premier import, sans attendre un second.

Trois fichiers possibles dans l'export :
  * Chart.csv  : Date, Non indexees, Indexees        -> courbe
  * Table.csv  : Raison, Source, Validation, Pages   -> repartition par motif
  * Table.csv d'un motif : URL, Derniere exploration -> liste d'URL concernees
"""
from __future__ import annotations

import csv
import io
import os
import re
import zipfile
from datetime import datetime

NUM = re.compile(r"[^0-9,.\-]")

# Motifs Search Console -> (cle interne, gravite, explication)
# La correspondance se fait par mots-cles : les libelles varient selon la langue
# et Google les reformule regulierement.
REASONS = [
    ("soft404",        ("soft 404",),                                           "critique",
     "Page qui repond 200 mais que Google juge vide ou inexistante."),
    ("404",            ("introuvable", "404", "not found"),                     "critique",
     "La page renvoie 404. Si elle recevait du trafic ou des liens, il faut la rediriger."),
    ("5xx",            ("erreur de serveur", "server error", "5xx"),            "critique",
     "Le serveur repond en erreur : Google ne peut pas indexer, et finit par desindexer."),
    ("403",            ("acces interdit", "forbidden", "403"),                  "eleve",
     "Acces refuse a Googlebot. Souvent un pare-feu ou une protection anti-bot trop stricte."),
    ("401",            ("non autorisee", "unauthorized", "401"),                "eleve",
     "Page protegee par authentification."),
    ("redirect",       ("redirection", "redirect"),                             "moyen",
     "Page redirigee : normal apres une refonte, anormal si le volume grimpe."),
    ("robots",         ("robots.txt",),                                         "eleve",
     "Bloquee par le robots.txt. A verifier : un blocage involontaire coute tout le trafic."),
    ("noindex",        ("noindex",),                                            "eleve",
     "Balise noindex. Verifiez qu'elle est intentionnelle."),
    ("canonical_user", ("double sans url canonique", "duplicate without user-selected",
                        "sans url canonique selectionnee"),                     "moyen",
     "Google a choisi une autre URL comme canonique : contenu duplique mal gere."),
    ("canonical_ok",   ("balise canonique correcte", "proper canonical", "alternate page"), "info",
     "Variante canonique correctement declaree : comportement normal."),
    ("canonical_diff", ("google a choisi", "google chose", "different canonical"), "moyen",
     "Google ignore votre canonique et en a choisi une autre."),
    ("crawled_no",     ("exploree, actuellement non indexee", "crawled - currently not indexed",
                        "exploree - actuellement non indexee"),                 "eleve",
     "Google a lu la page et n'en veut pas : signal de qualite ou de contenu trop mince."),
    ("discovered_no",  ("detectee, actuellement non indexee", "discovered - currently not indexed",
                        "detectee - actuellement non indexee"),                 "eleve",
     "Google connait l'URL mais n'est jamais venu : budget de crawl ou maillage insuffisant."),
    ("blocked_other",  ("bloquee", "blocked"),                                  "moyen",
     "Blocage divers signale par la Search Console."),
]
PROBLEM_KEYS = {"404", "5xx", "soft404", "403", "401", "robots", "crawled_no",
                "discovered_no", "canonical_user", "canonical_diff", "soft404"}


def _strip_accents(s):
    table = str.maketrans("àâäéèêëîïôöùûüçÀÂÄÉÈÊËÎÏÔÖÙÛÜÇ", "aaaeeeeiioouuucAAAEEEEIIOOUUUC")
    return (s or "").translate(table)


def classify(label):
    """Retourne (cle, gravite, explication) pour un libelle de motif."""
    low = _strip_accents((label or "").lower())
    for key, needles, sev, help_txt in REASONS:
        if any(n in low for n in needles):
            return key, sev, help_txt
    return "autre", "info", ""


def _num(v):
    s = NUM.sub("", str(v or "")).replace(" ", "").replace(" ", "")
    if not s:
        return 0
    if s.count(",") and s.count("."):
        s = s.replace(",", "")
    elif s.count(","):
        s = s.replace(",", ".")
    try:
        f = float(s)
        return int(f) if f.is_integer() else f
    except ValueError:
        return 0


def _read_csv(data):
    sample = data[:4096]
    try:
        delim = csv.Sniffer().sniff(sample, delimiters=",;\t").delimiter
    except csv.Error:
        delim = "\t" if "\t" in sample else (";" if sample.count(";") > sample.count(",") else ",")
    rows = [r for r in csv.reader(io.StringIO(data), delimiter=delim)
            if any((c or "").strip() for c in r)]
    return rows


def _files(path):
    """Retourne [(nom, contenu_texte)] depuis un .zip ou un .csv."""
    if path.lower().endswith(".zip"):
        out = []
        with zipfile.ZipFile(path) as z:
            for n in z.namelist():
                if n.lower().endswith((".csv", ".tsv")):
                    out.append((os.path.basename(n), z.read(n).decode("utf-8-sig", "replace")))
        return out
    with open(path, "rb") as fh:
        return [(os.path.basename(path), fh.read().decode("utf-8-sig", "replace"))]


def _parse_date(v):
    v = (v or "").strip()
    for fmt in ("%Y-%m-%d", "%d/%m/%Y", "%m/%d/%Y", "%d/%m/%y", "%Y/%m/%d"):
        try:
            return datetime.strptime(v, fmt).date().isoformat()
        except ValueError:
            continue
    return None


def parse_export(path):
    """Lit un export d'indexation. Retourne un dict avec ce qui a ete reconnu."""
    out = {"courbe": [], "motifs": [], "urls": [], "fichiers": [], "inconnus": []}
    for name, data in _files(path):
        rows = _read_csv(data)
        if len(rows) < 2:
            continue
        header = [(_strip_accents(c).strip().lower()) for c in rows[0]]
        body = rows[1:]
        kind = None

        i_date = next((i for i, h in enumerate(header) if h in ("date", "jour", "day")), None)
        i_url = next((i for i, h in enumerate(header)
                      if "url" in h or h in ("page", "adresse", "address")), None)
        i_reason = next((i for i, h in enumerate(header)
                         if "raison" in h or "reason" in h or "motif" in h or "etat" in h
                         or h == "status"), None)
        i_pages = next((i for i, h in enumerate(header)
                        if h in ("pages", "page", "nombre", "count", "impressions totales")), None)
        i_crawl = next((i for i, h in enumerate(header)
                        if "exploration" in h or "crawled" in h), None)
        i_src = next((i for i, h in enumerate(header) if h in ("source",)), None)

        # ------------------------------------------------------------- courbe
        if i_date is not None and len(header) >= 2:
            i_no = next((i for i, h in enumerate(header)
                         if "non index" in h or "not indexed" in h), None)
            i_yes = next((i for i, h in enumerate(header)
                          if ("index" in h and i != i_no and "non" not in h
                              and "not" not in h)), None)
            if i_no is not None or i_yes is not None:
                kind = "courbe"
                for r in body:
                    d = _parse_date(r[i_date]) if i_date < len(r) else None
                    if not d:
                        continue
                    out["courbe"].append({
                        "date": d,
                        "non_indexees": _num(r[i_no]) if i_no is not None and i_no < len(r) else None,
                        "indexees": _num(r[i_yes]) if i_yes is not None and i_yes < len(r) else None,
                    })

        # ------------------------------------------------- repartition motifs
        if kind is None and i_reason is not None and i_pages is not None:
            kind = "motifs"
            for r in body:
                if i_reason >= len(r):
                    continue
                label = (r[i_reason] or "").strip()
                if not label:
                    continue
                key, sev, help_txt = classify(label)
                out["motifs"].append({
                    "libelle": label, "cle": key, "gravite": sev, "aide": help_txt,
                    "pages": _num(r[i_pages]) if i_pages < len(r) else 0,
                    "source": (r[i_src].strip() if i_src is not None and i_src < len(r) else ""),
                })

        # ------------------------------------------------------- liste d'URL
        if kind is None and i_url is not None:
            vals = [r[i_url] for r in body if i_url < len(r)]
            if any(str(v).strip().lower().startswith("http") for v in vals[:20]):
                kind = "urls"
                label = re.sub(r"\.csv$", "", name, flags=re.I)
                for r in body:
                    if i_url >= len(r) or not str(r[i_url]).strip().lower().startswith("http"):
                        continue
                    reason = (r[i_reason].strip() if i_reason is not None and i_reason < len(r)
                              else label)
                    key, sev, _h = classify(reason)
                    out["urls"].append({
                        "url": r[i_url].strip(), "motif": reason, "cle": key, "gravite": sev,
                        "derniere_exploration": (_parse_date(r[i_crawl])
                                                 if i_crawl is not None and i_crawl < len(r) else None),
                    })

        out["fichiers"].append({"nom": name, "type": kind or "non reconnu",
                                "colonnes": rows[0], "lignes": len(body)})
        if kind is None:
            out["inconnus"].append(name)

    out["courbe"].sort(key=lambda x: x["date"])
    out["motifs"].sort(key=lambda x: -x["pages"])
    return out


# ------------------------------------------------------------------- analyse
def _at(courbe, days_back):
    """Valeur de non-indexees il y a N jours (la plus proche disponible)."""
    if not courbe:
        return None
    idx = max(0, len(courbe) - 1 - days_back)
    return courbe[idx]


def _pct(new, old):
    if not old:
        return None
    return round((new - old) / old * 100, 1)


def analyse(parsed, precedent=None):
    """Croise la courbe, la repartition par motif et l'import precedent.

    `precedent` : liste de motifs d'un import anterieur (meme format) ou None.
    """
    courbe = [c for c in parsed["courbe"] if c.get("non_indexees") is not None]
    motifs = parsed["motifs"]
    alertes = []
    resume = {}

    # ------------------------------------------------------------ la courbe
    if courbe:
        dernier = courbe[-1]
        resume = {
            "date": dernier["date"],
            "non_indexees": dernier["non_indexees"],
            "indexees": dernier["indexees"],
            "jours": len(courbe),
        }
        for label, back in (("7j", 7), ("30j", 30), ("90j", 90)):
            ref = _at(courbe, back)
            if ref and ref["date"] != dernier["date"]:
                resume["delta_" + label] = dernier["non_indexees"] - ref["non_indexees"]
                resume["pct_" + label] = _pct(dernier["non_indexees"], ref["non_indexees"])
                resume["ref_" + label] = ref["date"]

        # plus forte hausse d'un jour a l'autre : souvent la date de l'incident
        saut, saut_date = 0, None
        for a, b in zip(courbe, courbe[1:]):
            d = (b["non_indexees"] or 0) - (a["non_indexees"] or 0)
            if d > saut:
                saut, saut_date = d, b["date"]
        if saut_date and saut >= max(5, 0.05 * (dernier["non_indexees"] or 1)):
            resume["saut"] = {"date": saut_date, "pages": saut}

        for label, back, seuil_pct, seuil_abs in (("7 jours", 7, 5, 10), ("30 jours", 30, 10, 25)):
            pct = resume.get("pct_" + label.split()[0] + "j")
            delta = resume.get("delta_" + label.split()[0] + "j")
            if pct is None or delta is None:
                continue
            if delta >= seuil_abs and pct >= seuil_pct:
                alertes.append({
                    "gravite": "critique" if pct >= 25 else "eleve",
                    "titre": "Les pages non indexees augmentent : +%d en %s (+%.1f %%)"
                             % (delta, label, pct),
                    "detail": "Passe de %d a %d entre le %s et le %s."
                              % (resume["non_indexees"] - delta, resume["non_indexees"],
                                 resume.get("ref_" + label.split()[0] + "j", "?"), resume["date"]),
                })
        if resume.get("saut"):
            alertes.append({
                "gravite": "eleve",
                "titre": "Hausse brutale le %s : +%d pages non indexees en un jour"
                         % (resume["saut"]["date"], resume["saut"]["pages"]),
                "detail": "Cherchez ce qui a change ce jour-la : mise en ligne, migration, "
                          "modification du robots.txt ou du template.",
            })

    # ----------------------------------------------- comparaison des motifs
    avant = {m["libelle"]: m["pages"] for m in (precedent or [])}
    for m in motifs:
        if m["libelle"] in avant:
            m["avant"] = avant[m["libelle"]]
            m["delta"] = m["pages"] - avant[m["libelle"]]
            m["pct"] = _pct(m["pages"], avant[m["libelle"]])
        else:
            m["avant"] = m["delta"] = m["pct"] = None

    for m in motifs:
        if m["cle"] not in PROBLEM_KEYS:
            continue
        d = m.get("delta")
        if d is not None and d > 0 and (d >= 10 or (m["pct"] or 0) >= 20):
            alertes.append({
                "gravite": "critique" if m["gravite"] == "critique" else "eleve",
                "titre": "« %s » : +%d pages depuis le dernier import" % (m["libelle"], d),
                "detail": m["aide"] or "",
            })
        elif precedent is None and m["gravite"] == "critique" and m["pages"] > 0:
            alertes.append({
                "gravite": "eleve",
                "titre": "« %s » : %d page(s)" % (m["libelle"], m["pages"]),
                "detail": m["aide"] or "",
            })

    ordre = {"critique": 0, "eleve": 1, "moyen": 2, "info": 3}
    alertes.sort(key=lambda a: ordre.get(a["gravite"], 9))
    total_pb = sum(m["pages"] for m in motifs if m["cle"] in PROBLEM_KEYS)
    resume["pages_a_problemes"] = total_pb
    resume["total_motifs"] = sum(m["pages"] for m in motifs)
    return {"resume": resume, "courbe": courbe, "motifs": motifs, "alertes": alertes,
            "urls": parsed["urls"], "fichiers": parsed["fichiers"]}


def croiser_urls(analyse_res, crawl_result):
    """Confronte les URL non indexees au dernier crawl : que dit le site ?"""
    if not crawl_result:
        return analyse_res
    pages, inlinks = crawl_result.pages, crawl_result.inlinks
    for u in analyse_res["urls"]:
        p = pages.get(u["url"])
        liens = [l for l in inlinks.get(u["url"], []) if l["type"] == "lien"]
        u["dans_crawl"] = p is not None
        u["statut_reel"] = p.status if p else None
        u["indexabilite"] = p.indexability if p else ""
        u["liens_entrants"] = len(liens)
        u["sources"] = liens[:20]
        u["dans_sitemap"] = u["url"] in crawl_result.sitemap_urls
        u["diagnostic"] = _diag(u, p)
    return analyse_res


def _diag(u, p):
    """Confrontation entre ce que dit Google et ce que dit le site aujourd'hui."""
    if p is None:
        return "Non rencontree lors du dernier crawl (orpheline ou hors perimetre)."
    if u["cle"] == "404" and p.status == 200:
        return "Google la voit en 404 mais elle repond 200 aujourd'hui : demandez une reindexation."
    if u["cle"] == "404" and p.status >= 400:
        return ("Toujours en %d. %d lien(s) interne(s) pointent encore dessus."
                % (p.status, u["liens_entrants"]))
    if u["cle"] == "noindex" and "noindex" not in ("%s %s" % (p.meta_robots, p.x_robots)).lower():
        return "Le noindex n'est plus present : demandez une reindexation."
    if u["cle"] == "robots":
        return "Verifiez la regle du robots.txt qui bloque cette URL."
    if u["cle"] in ("crawled_no", "discovered_no"):
        if u["liens_entrants"] == 0:
            return "Aucun lien interne : Google la juge peu importante. Maillez-la."
        if p.word_count < 200:
            return "Contenu tres court (%d mots) : etoffez la page." % p.word_count
        return "%d lien(s) interne(s), %d mots. Renforcez le maillage ou le contenu." % (
            u["liens_entrants"], p.word_count)
    if u["cle"] in ("canonical_user", "canonical_diff"):
        return "Canonique declaree : %s" % (p.canonical or "aucune")
    if p.status != 200:
        return "Repond %d lors du crawl." % p.status
    return "Repond 200, %d lien(s) entrant(s)." % u["liens_entrants"]
