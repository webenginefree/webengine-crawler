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

## 3. Identifiants

Le mot de passe n'est jamais stocké en clair, seulement son empreinte :

```bash
cd /opt/webengine-crawler
./.venv/bin/python -m webengine hashpass          # demande ou génère un mot de passe
```

Puis `/etc/webengine-crawler.env` (droits `640`, `root:webengine`) :

```ini
WEBENGINE_USER=webengine
WEBENGINE_PASSWORD_HASH=scrypt:32768:8:1$...
WEBENGINE_SECRET=<64 caractères hexadécimaux aléatoires>
WEBENGINE_OUT=/var/lib/webengine-crawler
WEBENGINE_MAX_PAGES=5000
WEBENGINE_SESSION_HOURS=12
WEBENGINE_HTTPS=1
```

| Variable | Rôle |
|---|---|
| `WEBENGINE_USER` + `WEBENGINE_PASSWORD_HASH` | activent l'authentification. Absentes, l'interface est ouverte (usage local) |
| `WEBENGINE_SECRET` | signature des cookies de session. En changer déconnecte tout le monde |
| `WEBENGINE_HTTPS=1` | marque le cookie de session `Secure` (à ne mettre qu'avec HTTPS) |
| `WEBENGINE_MAX_PAGES` | plafond serveur du nombre d'URL par crawl |

## 4. Service systemd

`/etc/systemd/system/webengine-crawler.service` : gunicorn en **un seul worker** avec 8 threads.
C'est volontaire — l'état des crawls en cours vit en mémoire du process ; plusieurs workers
renverraient une progression incohérente.

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
- **File d'attente en mémoire.** Un redémarrage du service perd les crawls en cours ; les rapports
  déjà écrits sur disque, eux, sont conservés.
- **Rétention.** Rien n'est purgé automatiquement. Pour ne garder que 30 jours :
  `find /var/lib/webengine-crawler -mtime +30 -delete` dans une tâche cron.
