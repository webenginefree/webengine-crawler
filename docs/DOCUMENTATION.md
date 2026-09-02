# ⚙️ WebEngine Crawler

Crawler SEO local, gratuit et sans limite de pages — une version allégée de Screaming Frog.
Tout tourne sur ta machine, rien n'est envoyé ailleurs.

Il répond à trois questions :

1. **Quels H1 (et titles, descriptions, contenus) sont en double ?**
2. **Quelles URL renvoient une 404 / une erreur — et surtout quelles pages pointent dessus ?**
3. **D'où viennent les URL que je vois dans la Search Console ?** (liens internes, sitemap,
   redirection… ou rien du tout = page orpheline)

---

## Installation

```bash
cd webengine-crawler
pip install -r requirements.txt      # requests, beautifulsoup4, lxml, flask
```

Optionnel, pour avoir la commande `webengine` partout :

```bash
pip install -e .
```

## Utilisation

### 1. Interface web (le plus simple)

```bash
./webengine.sh serve
```

Ouvre `http://127.0.0.1:5005` : tu colles l'URL du site, tu déposes éventuellement ton export
Search Console, tu cliques. Le rapport s'ouvre automatiquement à la fin.

### 2. Ligne de commande

```bash
# crawl simple
./webengine.sh crawl https://monsite.fr

# crawl complet + croisement Search Console + exports CSV + sauvegarde du crawl
./webengine.sh crawl https://monsite.fr \
    -n 5000 -t 10 --delay 0.2 \
    --gsc ~/Téléchargements/Pages.csv \
    --csv ./exports \
    --save crawl-monsite.json.gz

# rejouer un croisement Search Console sans re-crawler
./webengine.sh gsc crawl-monsite.json.gz ~/Téléchargements/nouvel-export.csv
```

Options utiles :

| Option | Effet |
|---|---|
| `-n, --max-pages` | nombre max d'URL (défaut 500) |
| `-t, --threads` | requêtes en parallèle (défaut 8) |
| `--delay 0.3` | pause entre requêtes, pour ménager un petit serveur |
| `--exclude "REGEX"` | ignorer des URL (`"/panier\|\?filtre="`) |
| `--include "REGEX"` | ne crawler qu'une section (`"/blog/"`) |
| `--subdomains` | inclure les sous-domaines |
| `--ignore-robots` | ignorer robots.txt (recette, préprod) |
| `--auth user:mdp` | authentification basique (préprod protégée) |
| `--check-external` | vérifier aussi les liens sortants |
| `--csv DOSSIER` | exporter tous les tableaux en CSV |
| `--save FICHIER` | sauvegarder le crawl pour le rejouer |

## Le rapport

Un seul fichier HTML autonome (aucune connexion, aucun CDN) avec :

- **Vue d'ensemble** : compteurs cliquables et top des problèmes par gravité.
- **Problèmes détectés** : ~35 contrôles (404, 5xx, chaînes/boucles de redirection, H1 manquant /
  multiple / dupliqué, title & description manquants / trop longs / dupliqués, noindex, canonical
  cassée, contenu dupliqué, contenu pauvre, images sans alt, pages lentes, pages profondes,
  pages sans lien entrant, URL du sitemap non 200…).
- **404 & erreurs** : chaque URL cassée avec **la liste des pages qui pointent dessus et l'ancre
  utilisée** — c'est-à-dire exactement quoi corriger.
- **H1 / Titles / Descriptions / Contenus en double** : regroupés par valeur, dépliables.
- **Search Console** : ton export croisé avec le crawl.
- **Toutes les URL** : tri, filtre, export CSV.

Un **clic sur n'importe quelle ligne** ouvre le détail de l'URL : title, H1, H2, canonical, robots,
poids, temps de réponse… et la **liste complète de ses liens entrants** (page source + ancre),
navigable de proche en proche.

## Search Console : d'où vient cette URL ?

Dans la Search Console → *Résultats de recherche* → onglet **Pages** → **Exporter** (CSV ou ZIP).
Passe le fichier à `--gsc` (les exports français comme anglais sont reconnus).

Pour chaque URL, WebEngine Crawler indique :

- son **statut HTTP réel** (les URL absentes du crawl sont testées en direct) ;
- son **diagnostic** : `OK`, `Erreur 404 : URL morte encore dans la Search Console`,
  `Redirection 301`, `Orpheline : aucun lien interne`, `Non indexable : noindex`… ;
- sa **provenance** : nombre de liens internes, présence dans le sitemap, arrivée par redirection
  ou via une balise canonical — et la liste des pages sources avec leurs ancres ;
- les **clics perdus** : le total de clics qui tombent sur des URL à problème.

Les trois cas typiques que ça révèle :

- une URL avec du trafic qui répond **404** → à rediriger ;
- une URL **orpheline** (Google la connaît, aucun lien interne n'y mène) → à remailler ;
- une URL **redirigée** encore listée partout → à mettre à jour dans les liens internes.

## Suivi d'indexation

L'export **Search Console › Indexation › Pages** (bouton *Exporter*, en `.zip`) contient :

| Fichier | Contenu | Ce qu'on en tire |
|---|---|---|
| `Chart.csv` | courbe indexées / non indexées sur ~90 jours | tendance 7/30/90 jours, date de la hausse brutale |
| `Table.csv` | répartition par motif (404, noindex, robots.txt…) | gravité, comparaison avec l'import précédent |
| `<motif>.csv` | liste d'URL d'un motif (export depuis le détail) | croisement avec le dernier crawl |

Les libellés français et anglais sont reconnus. **Conservez le nom du fichier** exporté depuis
un motif (`Introuvable (404).csv`) : c'est lui qui porte le motif quand l'export n'a pas de
colonne dédiée.

```bash
./webengine.sh index export.zip                       # analyse simple
./webengine.sh index export.zip --compare ancien.zip  # évolution entre deux exports
./webengine.sh index export.zip --crawl crawl.json.gz # confrontation au site
```

Dans l'interface web, l'onglet **Indexation** historise chaque import par site : la comparaison
avec le précédent est automatique, et les crawls lancés depuis l'outil servent au croisement
sans manipulation.

Seuils d'alerte : hausse d'au moins 10 pages **et** 5 % sur 7 jours, ou 25 pages et 10 % sur
30 jours ; sur un motif, +10 pages ou +20 %.

## Exports CSV

`--csv exports/` produit : `urls.csv`, `erreurs.csv` (avec pages sources et ancres),
`h1_doubles.csv`, `titles_doubles.csv`, `descriptions_doubles.csv`, `problemes.csv`,
`liens_entrants.csv` et `search_console.csv`. Séparateur `;`, UTF-8 BOM : ça s'ouvre direct
dans Excel / LibreOffice.

## Bonnes manières

`robots.txt` est respecté par défaut, les sitemaps sont lus automatiquement, et `--delay`
permet de ralentir. Sur un site de production, `-t 5 --delay 0.2` est un bon réglage.

## Structure

```
webengine/
  crawler.py            moteur de crawl (threads, robots.txt, sitemaps, liens entrants)
  analyze.py            détection des problèmes et des doublons
  gsc.py                import Search Console + croisement
  report.py             construction des données, HTML et CSV
  report_template.html  interface du rapport
  web.py                interface web locale (Flask)
  cli.py                ligne de commande
  store.py              sauvegarde / rechargement d'un crawl
```
