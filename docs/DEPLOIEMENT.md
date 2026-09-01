# Déploiement de l'interface web derrière un reverse proxy

Procédure appliquée sur `tdcstaging` (Ubuntu 26.04, Apache déjà en place).
Aucun secret n'est stocké dans ce dépôt : tout passe par `/etc/webengine-crawler.env`.

## Principe

```
navigateur ──HTTPS──> Apache (vhost, Let's Encrypt) ──HTTP──> gunicorn 127.0.0.1:8088
                                                                  │
                                                    utilisateur système « webengine »
                                                    rapports dans /var/lib/webengine-crawler
```

Gunicorn n'écoute que sur la boucle locale : l'application n'est joignable que par Apache.

## 1. Dépendances et code

```bash
sudo apt-get install -y python3-venv git
sudo mkdir -p /opt/webengine-crawler && sudo chown "$USER" /opt/webengine-crawler
git clone https://github.com/webenginefree/webengine-crawler.git /opt/webengine-crawler
cd /opt/webengine-crawler
python3 -m venv .venv
./.venv/bin/pip install -r requirements.txt gunicorn
```

## 2. Utilisateur de service et données

```bash
sudo useradd --system --no-create-home --shell /usr/sbin/nologin webengine
sudo mkdir -p /var/lib/webengine-crawler
sudo chown webengine:webengine /var/lib/webengine-crawler
```

Le code reste la propriété de l'administrateur (mise à jour par `git pull`), le service n'a
d'accès en écriture que sur le dossier des rapports.

## 3. Comptes

Les comptes vivent dans SQLite (`$WEBENGINE_OUT/webengine.db`), pas dans la configuration.
Au premier démarrage, si aucun compte n'existe, l'interface propose la création du compte
administrateur. En ligne de commande :

```bash
cd /opt/webengine-crawler
sudo -u webengine ./.venv/bin/python -m webengine users add patron --admin
sudo -u webengine ./.venv/bin/python -m webengine users list
sudo -u webengine ./.venv/bin/python -m webengine users passwd patron
```

L'administrateur crée ensuite les autres comptes depuis l'onglet **Comptes**, avec pour chacun
son quota d'URL par crawl et son nombre de crawls simultanés. Chaque compte ne voit que ses
propres rapports ; un administrateur voit tout.

`/etc/webengine-crawler.env` (droits `640`, `root:webengine`) :

```ini
WEBENGINE_AUTH=1
WEBENGINE_SECRET=<64 caractères hexadécimaux aléatoires>
WEBENGINE_OUT=/var/lib/webengine-crawler
WEBENGINE_MAX_PAGES=5000
WEBENGINE_SESSION_HOURS=12
WEBENGINE_HTTPS=1
```

| Variable | Rôle |
|---|---|
| `WEBENGINE_AUTH=1` | impose la connexion même avant la création du premier compte |
| `WEBENGINE_SECRET` | signature des cookies de session. En changer déconnecte tout le monde |
| `WEBENGINE_HTTPS=1` | marque le cookie de session `Secure` (à ne mettre qu'avec HTTPS) |
| `WEBENGINE_MAX_PAGES` | plafond serveur, au-dessus des quotas par compte |
| `WEBENGINE_MAX_GLOBAL` | nombre de crawls simultanés, tous comptes confondus |
| `WEBENGINE_JOB_MEM_MB` | plafond mémoire de chaque process de crawl (défaut 1024) |

## 4. Service systemd

`/etc/systemd/system/webengine-crawler.service` : gunicorn sert l'interface, et **chaque crawl
s'exécute dans un process séparé** (`python -m webengine.runner <job>`), plafonné en mémoire
et déprioritisé. Un crawl qui plante ou sature la RAM n'affecte ni l'interface ni les autres
comptes, et le bouton « Annuler » tue réellement le process.

L'état partagé (comptes, jobs, avancement) est en SQLite : l'interface peut donc tourner sur
plusieurs workers, et un redémarrage ne perd que les crawls en cours, marqués comme interrompus.

```bash
sudo systemctl enable --now webengine-crawler
sudo systemctl status webengine-crawler
sudo journalctl -u webengine-crawler -f
```

## 5. Vhost Apache

```apache
<VirtualHost *:80>
    ServerName crawler.example.fr
    ProxyPreserveHost On
    ProxyPass        / http://127.0.0.1:8088/ timeout=600
    ProxyPassReverse / http://127.0.0.1:8088/
    RequestHeader set X-Forwarded-Proto "http"
    LimitRequestBody 52428800
</VirtualHost>
```

```bash
sudo a2enmod proxy proxy_http headers
sudo a2ensite crawler.example.fr.conf && sudo systemctl reload apache2
sudo certbot --apache --redirect -d crawler.example.fr
```

Après certbot, passer `X-Forwarded-Proto` à `https` dans le vhost `-le-ssl.conf` généré :
sans ça l'application se croit en HTTP et refuse d'émettre le cookie `Secure`.

Le `timeout=600` du `ProxyPass` est nécessaire : un crawl volumineux dépasse largement les
60 secondes par défaut d'Apache.

## Mise à jour

```bash
cd /opt/webengine-crawler && git pull
./.venv/bin/pip install -r requirements.txt
sudo systemctl restart webengine-crawler
```

## Points d'attention

- **Pas de protection SSRF.** L'outil accepte n'importe quelle URL, adresses privées comprises —
  c'est voulu pour auditer des préproductions internes. Ne jamais exposer l'interface sans
  authentification : ce serait un relais vers le réseau interne du serveur.
- **Crawls en cours et redémarrage.** systemd tue les process de crawl avec le service ; ils sont
  marqués « interrompus » au démarrage suivant. Les rapports déjà écrits sont conservés.
- **Cloisonnement.** Les comptes sont isolés au niveau des données (un dossier de rapports par
  compte, contrôle du propriétaire à chaque téléchargement) et des ressources (process séparé,
  quotas par compte). Ce n'est pas une isolation système : tous les crawls tournent sous le même
  utilisateur Unix. Pour aller plus loin il faudrait un conteneur par compte.
- **Rétention.** Rien n'est purgé automatiquement. Pour ne garder que 30 jours :
  `find /var/lib/webengine-crawler -mtime +30 -delete` dans une tâche cron.
