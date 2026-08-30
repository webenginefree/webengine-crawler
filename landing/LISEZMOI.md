# Page de présentation à publier sur votre site

Contenu de ce dossier : `index.html` (page autonome) et `img/` (5 captures).

## À personnaliser avant publication

Deux placeholders à remplacer :

Le pseudo GitHub est déjà renseigné (`webenginefree`). Restent le domaine et le nom :

```bash
cd landing
sed -i 's|votre-site.fr|VOTRE-DOMAINE.fr|g; s|votre nom|VOTRE NOM|g; s|vous@|contact@|g' index.html
```

Puis vérifiez à la main :

- la balise `<link rel="canonical">` (chemin exact où la page sera publiée) ;
- l'URL `og:image` (doit être absolue pour l'aperçu sur les réseaux) ;
- le JSON-LD en bas de page (`url`).

## Où la mettre

Idéalement à une URL courte et stable, par exemple `/webengine-crawler` ou `/outils/webengine-crawler`.
C'est **cette page** que vous partagez partout — pas le dépôt GitHub, dont tous les liens
sortants sont en `nofollow`. Le README GitHub, lui, pointe vers cette page.

## Poids

~24 Ko de HTML + 340 Ko d'images, sans aucune dépendance à part la police Google Fonts
(supprimez la balise `<link>` vers fonts.googleapis.com si vous préférez des polices système).
