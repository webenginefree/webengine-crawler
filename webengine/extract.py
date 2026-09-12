# -*- coding: utf-8 -*-
"""Extraction personnalisee : recuperer n'importe quelle valeur de la page.

Syntaxe d'un extracteur, une par ligne ou une par option --extract :

    nom=css:selecteur            texte de l'element
    nom=css:selecteur@attribut   valeur d'un attribut (href, content, datetime...)
    nom=xpath:expression         XPath complet (texte, attribut ou noeud)
    nom=regex:motif              1er groupe capturant, sinon la correspondance

Exemples :
    Prix=css:.product-price
    Canonique=css:link[rel=canonical]@href
    Auteur=xpath://meta[@name="author"]/@content
    Reference=regex:REF-([0-9]{6})
"""
from __future__ import annotations

import re

TYPES = ("css", "xpath", "regex")
SEP = 120          # longueur max conservee par valeur


class Extracteur:
    def __init__(self, nom, type_, expression, attribut=None):
        self.nom = nom
        self.type = type_
        self.expression = expression
        self.attribut = attribut

    def __repr__(self):
        return "<%s %s:%s>" % (self.nom, self.type, self.expression)


def parse(lignes):
    """Transforme des chaines en extracteurs. Leve ValueError si la syntaxe est fausse."""
    if isinstance(lignes, str):
        lignes = lignes.splitlines()
    out = []
    for brut in lignes:
        ligne = (brut or "").strip()
        if not ligne or ligne.startswith("#"):
            continue
        if "=" not in ligne:
            raise ValueError("« %s » : format attendu nom=type:expression" % ligne[:60])
        nom, reste = ligne.split("=", 1)
        nom = nom.strip()
        if ":" not in reste:
            raise ValueError("« %s » : type manquant (css, xpath ou regex)" % ligne[:60])
        type_, expression = reste.split(":", 1)
        type_ = type_.strip().lower()
        if type_ not in TYPES:
            raise ValueError("« %s » : type inconnu, attendu css, xpath ou regex" % type_)
        attribut = None
        if type_ == "css" and "@" in expression:
            expression, attribut = expression.rsplit("@", 1)
            attribut = attribut.strip()
        expression = expression.strip()
        if not nom or not expression:
            raise ValueError("« %s » : nom ou expression vide" % ligne[:60])
        if type_ == "regex":
            try:
                re.compile(expression)
            except re.error as exc:
                raise ValueError("« %s » : expression reguliere invalide (%s)" % (nom, exc))
        out.append(Extracteur(nom, type_, expression, attribut))
    return out


def _texte(valeur):
    if valeur is None:
        return ""
    if isinstance(valeur, str):
        t = valeur
    elif hasattr(valeur, "get_text"):                  # element BeautifulSoup
        t = valeur.get_text(" ", strip=True)
    elif hasattr(valeur, "text_content"):              # element lxml
        t = valeur.text_content()
    else:
        t = str(valeur)
    t = re.sub(r"\s+", " ", t).strip()
    return t[:SEP] + "…" if len(t) > SEP else t


def appliquer(extracteurs, soup, html_text, arbre_lxml=None):
    """Retourne {nom: [valeurs]}. Une extraction qui echoue ne fait jamais tomber le crawl."""
    resultats = {}
    for ex in extracteurs:
        valeurs = []
        try:
            if ex.type == "css":
                for el in soup.select(ex.expression):
                    v = el.get(ex.attribut, "") if ex.attribut else el
                    t = _texte(v)
                    if t:
                        valeurs.append(t)
            elif ex.type == "xpath":
                if arbre_lxml is not None:
                    for v in arbre_lxml.xpath(ex.expression):
                        t = _texte(v)
                        if t:
                            valeurs.append(t)
            else:
                motif = re.compile(ex.expression, re.I | re.S)
                for m in motif.finditer(html_text):
                    t = _texte(m.group(1) if m.groups() else m.group(0))
                    if t:
                        valeurs.append(t)
        except Exception as exc:                        # selecteur invalide, page biscornue…
            valeurs = ["⚠ %s" % type(exc).__name__]
        resultats[ex.nom] = valeurs[:20]
    return resultats


def besoin_lxml(extracteurs):
    return any(e.type == "xpath" for e in extracteurs)
