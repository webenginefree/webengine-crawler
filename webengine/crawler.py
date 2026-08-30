# -*- coding: utf-8 -*-
"""Moteur de crawl de WebEngine Crawler.

Parcourt un site en largeur, enregistre pour chaque URL les donnees SEO
(titre, H1, meta, canonical, statut HTTP...) ET surtout la liste des liens
entrants (quelle page pointe vers quelle URL, avec quelle ancre).
"""
from __future__ import annotations

import hashlib
import queue
import re
import threading
import time
from dataclasses import dataclass, field
from urllib.parse import urljoin, urlsplit, urlunsplit
from urllib.robotparser import RobotFileParser

import requests
from bs4 import BeautifulSoup

DEFAULT_UA = ("Mozilla/5.0 (compatible; WebEngineCrawler/1.0; +crawler SEO local)")
BAD_SCHEMES = ("mailto:", "tel:", "javascript:", "data:", "sms:", "ftp:", "file:", "#")
HTML_TYPES = ("text/html", "application/xhtml+xml")
MAX_BODY = 4 * 1024 * 1024  # 4 Mo max par page parsee
WS = re.compile(r"\s+")


def clean(txt):
    return WS.sub(" ", (txt or "")).strip()


def normalize_url(url, base=None):
    """Absolutise + nettoie une URL. Retourne None si elle n'est pas crawlable."""
    if not url:
        return None
    url = url.strip()
    low = url.lower()
    if low.startswith(BAD_SCHEMES):
        return None
    if base:
        url = urljoin(base, url)
    try:
        p = urlsplit(url)
    except ValueError:
        return None
    if p.scheme not in ("http", "https"):
        return None
    netloc = p.netloc.lower()
    if p.scheme == "http" and netloc.endswith(":80"):
        netloc = netloc[:-3]
    if p.scheme == "https" and netloc.endswith(":443"):
        netloc = netloc[:-4]
    path = p.path or "/"
    return urlunsplit((p.scheme, netloc, path, p.query, ""))


def registrable(host):
    """Domaine 'racine' approximatif (suffit pour regrouper les sous-domaines)."""
    host = (host or "").lower().split(":")[0]
    parts = host.split(".")
    if len(parts) <= 2:
        return host
    # gere les suffixes composes les plus courants (.co.uk, .com.br...)
    if parts[-2] in ("co", "com", "org", "net", "gov", "edu", "ac") and len(parts[-1]) == 2:
        return ".".join(parts[-3:])
    return ".".join(parts[-2:])


@dataclass
class Page:
    url: str
    depth: int = 0
    status: int = 0
    status_text: str = ""
    content_type: str = ""
    size: int = 0
    response_time: float = 0.0
    redirect_to: str = ""
    title: str = ""
    meta_description: str = ""
    h1: list = field(default_factory=list)
    h2: list = field(default_factory=list)
    canonical: str = ""
    meta_robots: str = ""
    x_robots: str = ""
    lang: str = ""
    word_count: int = 0
    text_ratio: float = 0.0
    images: int = 0
    images_no_alt: int = 0
    links_internal: int = 0
    links_external: int = 0
    content_hash: str = ""
    hreflang: list = field(default_factory=list)
    error: str = ""
    is_html: bool = False
    source: str = "lien"  # lien | sitemap | depart | gsc

    # calcule apres coup
    indexable: bool = True
    indexability: str = "Indexable"

    def as_dict(self):
        d = dict(self.__dict__)
        return d


class CrawlResult:
    def __init__(self, start_url):
        self.start_url = start_url
        self.pages = {}            # url -> Page
        self.inlinks = {}          # url cible -> [ {from, anchor, rel, type} ]
        self.outlinks = {}         # url source -> [urls cibles internes]
        self.external = {}         # url externe -> {status, inlinks: n}
        self.sitemap_urls = set()
        self.robots_blocked = set()
        self.started = time.time()
        self.finished = None
        self.host = urlsplit(start_url).netloc.lower()

    @property
    def duration(self):
        return round((self.finished or time.time()) - self.started, 1)


class Crawler:
    def __init__(self, start_url, max_pages=500, max_depth=10, threads=8, delay=0.0,
                 user_agent=DEFAULT_UA, timeout=20, include_subdomains=False,
                 respect_robots=True, include_re=None, exclude_re=None,
                 use_sitemaps=True, check_external=False, progress=None, auth=None):
        start = normalize_url(start_url)
        if not start:
            raise ValueError("URL de depart invalide: %s" % start_url)
        self.start_url = start
        self.max_pages = max_pages
        self.max_depth = max_depth
        self.threads = max(1, threads)
        self.delay = delay
        self.user_agent = user_agent
        self.timeout = timeout
        self.include_subdomains = include_subdomains
        self.respect_robots = respect_robots
        self.include_re = re.compile(include_re) if include_re else None
        self.exclude_re = re.compile(exclude_re) if exclude_re else None
        self.use_sitemaps = use_sitemaps
        self.check_external = check_external
        self.progress = progress or (lambda **kw: None)
        self.auth = auth

        self.result = CrawlResult(self.start_url)
        self.root = registrable(urlsplit(self.start_url).netloc)
        self.host = urlsplit(self.start_url).netloc.lower()

        self._lock = threading.Lock()
        self._seen = set()
        self._q = queue.Queue()
        self._stop = threading.Event()
        self._local = threading.local()
        self._robots = None
        self.cancelled = False

    # ------------------------------------------------------------------ HTTP
    @property
    def session(self):
        s = getattr(self._local, "session", None)
        if s is None:
            s = requests.Session()
            s.headers.update({
                "User-Agent": self.user_agent,
                "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
                "Accept-Language": "fr-FR,fr;q=0.9,en;q=0.8",
            })
            if self.auth:
                s.auth = self.auth
            self._local.session = s
        return s

    # --------------------------------------------------------------- robots
    def _load_robots(self):
        base = "%s://%s" % (urlsplit(self.start_url).scheme, self.host)
        rp = RobotFileParser()
        rp.set_url(base + "/robots.txt")
        sitemaps = []
        try:
            r = self.session.get(base + "/robots.txt", timeout=self.timeout)
            if r.status_code == 200 and r.text:
                rp.parse(r.text.splitlines())
                for line in r.text.splitlines():
                    if line.lower().startswith("sitemap:"):
                        sm = normalize_url(line.split(":", 1)[1].strip())
                        if sm:
                            sitemaps.append(sm)
            else:
                rp.parse([])
        except Exception:
            rp.parse([])
        self._robots = rp
        return sitemaps

    def _allowed_by_robots(self, url):
        if not self.respect_robots or self._robots is None:
            return True
        try:
            return self._robots.can_fetch(self.user_agent, url)
        except Exception:
            return True

    # -------------------------------------------------------------- sitemaps
    def _fetch_sitemaps(self, urls, seen=None, depth=0):
        """Recupere les URL declarees dans les sitemaps (pour reperer les orphelines)."""
        seen = seen if seen is not None else set()
        found = set()
        for sm in urls:
            if sm in seen or depth > 3:
                continue
            seen.add(sm)
            try:
                r = self.session.get(sm, timeout=self.timeout)
                if r.status_code != 200:
                    continue
                soup = BeautifulSoup(r.content, "xml")
                children = [normalize_url(l.get_text(strip=True))
                            for l in soup.select("sitemap > loc")]
                children = [c for c in children if c]
                if children:
                    found |= self._fetch_sitemaps(children, seen, depth + 1)
                for loc in soup.select("url > loc"):
                    u = normalize_url(loc.get_text(strip=True))
                    if u:
                        found.add(u)
            except Exception:
                continue
        return found

    # ------------------------------------------------------------- perimetre
    def is_internal(self, url):
        host = urlsplit(url).netloc.lower()
        if self.include_subdomains:
            return registrable(host) == self.root
        return host == self.host

    def should_crawl(self, url):
        if not self.is_internal(url):
            return False
        if self.exclude_re and self.exclude_re.search(url):
            return False
        if self.include_re and not self.include_re.search(url):
            return False
        return True

    # ------------------------------------------------------------------ file
    def _enqueue(self, url, depth, source="lien"):
        if depth > self.max_depth:
            return
        with self._lock:
            if url in self._seen:
                return
            if len(self._seen) >= self.max_pages:
                return
            if not self._allowed_by_robots(url):
                self.result.robots_blocked.add(url)
                return
            self._seen.add(url)
        self._q.put((url, depth, source))

    def _add_inlink(self, target, source, anchor, rel="", kind="lien"):
        with self._lock:
            self.result.inlinks.setdefault(target, []).append({
                "from": source, "anchor": anchor, "rel": rel, "type": kind})
            self.result.outlinks.setdefault(source, []).append(target)

    # --------------------------------------------------------------- workers
    def _worker(self):
        while not self._stop.is_set():
            try:
                url, depth, source = self._q.get(timeout=0.3)
            except queue.Empty:
                continue
            try:
                self._process(url, depth, source)
            except Exception as exc:  # ne jamais tuer un worker
                with self._lock:
                    p = Page(url=url, depth=depth, source=source)
                    p.error = "%s: %s" % (type(exc).__name__, exc)
                    p.status_text = "Erreur"
                    self.result.pages[url] = p
            finally:
                self._q.task_done()
                if self.delay:
                    time.sleep(self.delay)

    def _process(self, url, depth, source):
        t0 = time.time()
        page = Page(url=url, depth=depth, source=source)
        try:
            r = self.session.get(url, timeout=self.timeout, allow_redirects=False, stream=True)
        except requests.RequestException as exc:
            page.status = 0
            page.status_text = "Connexion impossible"
            page.error = type(exc).__name__
            page.response_time = round(time.time() - t0, 3)
            self._store(page)
            return

        page.status = r.status_code
        page.status_text = r.reason or ""
        page.content_type = (r.headers.get("Content-Type") or "").split(";")[0].strip().lower()
        page.x_robots = r.headers.get("X-Robots-Tag", "")
        body = b""
        try:
            if page.content_type in HTML_TYPES or (not page.content_type and r.status_code < 400):
                for chunk in r.iter_content(65536):
                    body += chunk
                    if len(body) > MAX_BODY:
                        break
                page.is_html = True
            else:
                cl = r.headers.get("Content-Length")
                page.size = int(cl) if cl and cl.isdigit() else 0
        finally:
            r.close()

        page.response_time = round(time.time() - t0, 3)
        if body:
            page.size = len(body)

        # ------------------------------------------------------- redirections
        if 300 <= page.status < 400:
            loc = r.headers.get("Location")
            target = normalize_url(loc, url) if loc else None
            page.redirect_to = target or (loc or "")
            page.is_html = False
            self._store(page)
            if target:
                self._add_inlink(target, url, "(redirection %d)" % page.status, kind="redirection")
                if self.should_crawl(target):
                    self._enqueue(target, depth, "redirection")
            return

        if not page.is_html or not body:
            self._store(page)
            return

        # ------------------------------------------------------------- parsing
        try:
            soup = BeautifulSoup(body, "lxml")
        except Exception:
            soup = BeautifulSoup(body, "html.parser")

        page.title = clean(soup.title.get_text()) if soup.title else ""
        md = soup.find("meta", attrs={"name": re.compile(r"^description$", re.I)})
        page.meta_description = clean(md.get("content")) if md and md.get("content") else ""
        mr = soup.find("meta", attrs={"name": re.compile(r"^robots$", re.I)})
        page.meta_robots = clean(mr.get("content")) if mr and mr.get("content") else ""
        can = soup.find("link", attrs={"rel": re.compile(r"canonical", re.I)})
        page.canonical = normalize_url(can.get("href"), url) or "" if can and can.get("href") else ""
        page.h1 = [clean(h.get_text()) for h in soup.find_all("h1")]
        page.h2 = [clean(h.get_text()) for h in soup.find_all("h2")][:30]
        html_tag = soup.find("html")
        page.lang = (html_tag.get("lang") or "").strip() if html_tag else ""
        for alt in soup.find_all("link", attrs={"rel": re.compile(r"alternate", re.I)}):
            if alt.get("hreflang"):
                page.hreflang.append({"lang": alt.get("hreflang"),
                                      "url": normalize_url(alt.get("href"), url) or ""})

        imgs = soup.find_all("img")
        page.images = len(imgs)
        page.images_no_alt = sum(1 for i in imgs if not (i.get("alt") or "").strip())

        for bad in soup(["script", "style", "noscript", "template", "svg"]):
            bad.decompose()
        text = clean(soup.get_text(" "))
        page.word_count = len([w for w in text.split(" ") if w])
        page.text_ratio = round(len(text) / max(1, len(body)) * 100, 1)
        norm = re.sub(r"[^a-z0-9]+", " ", text.lower()).strip()
        page.content_hash = hashlib.sha1(norm.encode("utf-8", "ignore")).hexdigest()[:16] if norm else ""

        # ---------------------------------------------------------- liens
        internal = external = 0
        for a in soup.find_all("a", href=True):
            raw = a.get("href")
            target = normalize_url(raw, url)
            if not target:
                continue
            rel = " ".join(a.get("rel") or [])
            anchor = clean(a.get_text()) or clean(a.get("title")) or "(sans ancre)"
            if len(anchor) > 120:
                anchor = anchor[:117] + "..."
            if self.is_internal(target):
                internal += 1
                self._add_inlink(target, url, anchor, rel)
                if self.should_crawl(target):
                    self._enqueue(target, depth + 1)
            else:
                external += 1
                with self._lock:
                    ext = self.result.external.setdefault(
                        target, {"status": None, "count": 0, "sources": []})
                    ext["count"] += 1
                    if len(ext["sources"]) < 20:
                        ext["sources"].append({"from": url, "anchor": anchor, "rel": rel})
        page.links_internal = internal
        page.links_external = external

        if page.canonical and page.canonical != url and self.should_crawl(page.canonical):
            self._add_inlink(page.canonical, url, "(canonical)", kind="canonical")
            self._enqueue(page.canonical, depth, "canonical")

        self._store(page)

    def _store(self, page):
        with self._lock:
            self.result.pages[page.url] = page
            n = len(self.result.pages)
        self.progress(crawled=n, queued=self._q.qsize(), url=page.url, status=page.status)

    # ------------------------------------------------------------------- run
    def cancel(self):
        self.cancelled = True
        self._stop.set()

    def run(self):
        sitemaps = self._load_robots()
        if self.use_sitemaps:
            base = "%s://%s" % (urlsplit(self.start_url).scheme, self.host)
            candidates = list(dict.fromkeys(sitemaps + [base + "/sitemap.xml",
                                                        base + "/sitemap_index.xml"]))
            self.result.sitemap_urls = {u for u in self._fetch_sitemaps(candidates)
                                        if self.is_internal(u)}

        self._enqueue(self.start_url, 0, "depart")
        for u in sorted(self.result.sitemap_urls):
            if self.should_crawl(u):
                self._enqueue(u, 1, "sitemap")

        workers = [threading.Thread(target=self._worker, daemon=True) for _ in range(self.threads)]
        for w in workers:
            w.start()
        try:
            while True:
                if self._q.unfinished_tasks == 0:
                    break
                if self._stop.is_set():
                    break
                time.sleep(0.2)
        except KeyboardInterrupt:
            self.cancelled = True
        self._stop.set()
        for w in workers:
            w.join(timeout=2)

        if self.check_external:
            self._check_external_links()

        finalize(self.result)
        self.result.finished = time.time()
        return self.result

    def _check_external_links(self):
        urls = list(self.result.external.keys())[:400]

        def check(u):
            try:
                r = self.session.head(u, timeout=10, allow_redirects=True)
                if r.status_code >= 400 or r.status_code == 405:
                    r = self.session.get(u, timeout=10, allow_redirects=True, stream=True)
                    r.close()
                self.result.external[u]["status"] = r.status_code
            except requests.RequestException:
                self.result.external[u]["status"] = 0

        threads = []
        sem = threading.Semaphore(self.threads)

        def runner(u):
            with sem:
                check(u)

        for u in urls:
            t = threading.Thread(target=runner, args=(u,), daemon=True)
            t.start()
            threads.append(t)
        for t in threads:
            t.join(timeout=15)


def finalize(result):
    """Calcule l'indexabilite de chaque page une fois le crawl termine."""
    for url, p in result.pages.items():
        robots = ("%s %s" % (p.meta_robots, p.x_robots)).lower()
        if p.status == 0:
            p.indexable, p.indexability = False, "Erreur de connexion"
        elif 300 <= p.status < 400:
            p.indexable, p.indexability = False, "Redirection (%d)" % p.status
        elif p.status >= 400:
            p.indexable, p.indexability = False, "Statut %d" % p.status
        elif not p.is_html:
            p.indexable, p.indexability = False, "Non HTML"
        elif "noindex" in robots:
            p.indexable, p.indexability = False, "noindex"
        elif p.canonical and p.canonical != url:
            p.indexable, p.indexability = False, "Canonique vers une autre URL"
        else:
            p.indexable, p.indexability = True, "Indexable"
    return result
