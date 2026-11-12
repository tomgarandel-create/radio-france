#!/usr/bin/env python3
"""
Radio France – les sorties du jour
==================================
Lance :  python3 radiofrance_du_jour.py
Puis ouvre http://localhost:8765 (s'ouvre tout seul).

- Lit tous les flux RSS de radiofrance_podcasts_rss.json (même dossier)
- Affiche les épisodes publiés aujourd'hui (heure de Paris), du plus récent au plus ancien
- Bouton « Résumer » : récupère la transcription sur radiofrance.fr et la résume

Aucune dépendance : Python 3.9+ suffit.

Résumé IA (facultatif) – choisis avec la variable d'environnement LLM :
  GEMINI_API_KEY=...  (suffit à activer Gemini) [+ GEMINI_MODEL, défaut gemini-flash-latest]
  LLM=anthropic  + ANTHROPIC_API_KEY=...            (API Claude)
  LLM=openai     + OPENAI_API_KEY=...  [+ OPENAI_MODEL]
  LLM=ollama     (modèle local gratuit, ex. OLLAMA_MODEL=mistral)
  sans rien      -> résumé « extractif » (phrases clés), sans IA ni clé
"""
import argparse, concurrent.futures as cf, email.utils, html, json, os, re, sys
import threading, time, urllib.parse, urllib.request, webbrowser, xml.etree.ElementTree as ET
from collections import Counter
from datetime import datetime, date, timedelta
from html.parser import HTMLParser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from zoneinfo import ZoneInfo

VERSION = "2.0"
PARIS = ZoneInfo("Europe/Paris")
KEY_FILE = os.environ.get("RF_KEY_FILE") or os.path.join(
    os.path.expanduser("~"), "Library", "Application Support", "RadioFranceDuJour", "cle_gemini.txt")


PUBLIC = os.environ.get("PUBLIC", "") == "1"
DOMAINES_OK = ("radiofrance.fr", "radiofrance-podcast.net", "franceinfo.fr")
MAX_PAR_HEURE = int(os.environ.get("MAX_RESUMES_HEURE", "20"))      # par visiteur
MAX_PAR_JOUR = int(os.environ.get("MAX_RESUMES_JOUR", "300"))       # pour tout le site
_cache_resumes, _compteurs = {}, {}


def url_autorisee(url):
    h = (urllib.parse.urlparse(url).hostname or "").lower()
    return any(h == d or h.endswith("." + d) for d in DOMAINES_OK)


def quota_ok(ip):
    """Limite le nombre de résumés (protège ta clé Gemini quand le site est public)."""
    now = time.time()
    jour = time.strftime("%Y-%m-%d")
    if _compteurs.get("_jour") != jour:
        _compteurs.clear(); _compteurs["_jour"] = jour; _compteurs["_total"] = 0
    hist = [t for t in _compteurs.get(ip, []) if now - t < 3600]
    if len(hist) >= MAX_PAR_HEURE:
        return False, "Limite atteinte : réessaie dans une heure."
    if _compteurs["_total"] >= MAX_PAR_JOUR:
        return False, "Limite quotidienne du site atteinte : réessaie demain."
    hist.append(now); _compteurs[ip] = hist; _compteurs["_total"] += 1
    return True, ""


def cle_gemini():
    k = os.environ.get("GEMINI_API_KEY", "").strip()
    if not k and os.path.exists(KEY_FILE):
        k = open(KEY_FILE, encoding="utf-8").read().strip()
    return k


RGL_FILE = os.path.join(os.path.dirname(KEY_FILE), "reglages.json")
OLLAMA = os.environ.get("OLLAMA_URL", "http://localhost:11434")


def reglages():
    try:
        return json.load(open(RGL_FILE, encoding="utf-8"))
    except Exception:
        return {}


def enregistrer_reglages(**kw):
    r = reglages(); r.update(kw)
    os.makedirs(os.path.dirname(RGL_FILE), exist_ok=True)
    json.dump(r, open(RGL_FILE, "w", encoding="utf-8"))


def modele_ollama():
    return os.environ.get("OLLAMA_MODEL") or reglages().get("ollama_modele") or "mistral"


def ollama_etat():
    """(ok, message) : Ollama lancé ? modèle installé ?"""
    try:
        tags = json.loads(urllib.request.urlopen(OLLAMA + "/api/tags", timeout=3).read())
    except Exception:
        return False, "Ollama n'est pas lancé : ouvre l'app Ollama (icône de lama dans la barre du haut)"
    noms = [t["name"] for t in tags.get("models", [])]
    m = modele_ollama()
    if any(n == m or n.split(":")[0] == m.split(":")[0] for n in noms):
        return True, ""
    return False, f"le modèle « {m} » n'est pas encore téléchargé : clique sur « Télécharger le modèle »"


def enregistrer_cle(k):
    os.makedirs(os.path.dirname(KEY_FILE), exist_ok=True)
    with open(KEY_FILE, "w", encoding="utf-8") as f:
        f.write(k.strip())
    os.environ["GEMINI_API_KEY"] = k.strip()


def erreur_lisible(err):
    """Transforme une erreur HTTP de Gemini en message compréhensible."""
    msg = str(err)
    if hasattr(err, "read"):
        try:
            msg = json.loads(err.read()).get("error", {}).get("message", msg)
        except Exception:
            pass
    if "API key not valid" in msg or "API_KEY_INVALID" in msg:
        return "clé Gemini invalide : recopie-la depuis aistudio.google.com/apikey"
    if "high demand" in msg.lower() or "overloaded" in msg.lower():
        return "Gemini est surchargé en ce moment (ta clé fonctionne) : réessaie dans un instant"
    if "quota" in msg.lower() or "429" in msg:
        return "quota Gemini gratuit dépassé pour le moment : réessaie dans une minute"
    if "CERTIFICATE_VERIFY_FAILED" in msg:
        return "certificats manquants : lance « Install Certificates.command » dans Applications > Python"
    return msg[:300]
HERE = os.path.dirname(os.path.abspath(__file__))
UA = "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124 Safari/537.36"
ITUNES = "{http://www.itunes.com/dtds/podcast-1.0.dtd}"


# ----------------------------------------------------------------- réseau
def fetch(url, timeout=20):
    req = urllib.request.Request(url, headers={"User-Agent": UA, "Accept-Language": "fr-FR,fr"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read()


# ----------------------------------------------------------------- flux RSS
def load_feeds(path, top_only=False):
    with open(path, encoding="utf-8") as f:
        feeds = json.load(f)
    if top_only:
        feeds = [x for x in feeds if x.get("top51")]
    return feeds


def strip_tags(s):
    s = re.sub(r"<[^>]+>", " ", s or "")
    return re.sub(r"\s+", " ", html.unescape(s)).strip()


def parse_feed(feed, day):
    """Renvoie les épisodes du flux publiés le jour `day` (date, heure de Paris)."""
    out = []
    root = ET.fromstring(fetch(feed["rss"]))
    show = (root.findtext("channel/title") or feed["emission"]).strip()
    for it in root.iter("item"):
        pub = it.findtext("pubDate")
        if not pub:
            continue
        try:
            dt = email.utils.parsedate_to_datetime(pub).astimezone(PARIS)
        except Exception:
            continue
        if dt.date() != day:
            continue
        enc = it.find("enclosure")
        out.append({
            "chaine": feed["chaine"],
            "emission": show,
            "titre": (it.findtext("title") or "").strip(),
            "date": dt.isoformat(),
            "heure": dt.strftime("%H:%M"),
            "description": strip_tags(it.findtext("description") or it.findtext(ITUNES + "summary")),
            "lien": (it.findtext("link") or "").strip(),
            "audio": enc.get("url") if enc is not None else "",
            "duree": it.findtext(ITUNES + "duration") or "",
        })
    return out


def sorties_du_jour(feeds, day, workers=32):
    eps, erreurs, seen = [], [], set()
    with cf.ThreadPoolExecutor(workers) as ex:
        futs = {ex.submit(parse_feed, f, day): f for f in feeds}
        for fu in cf.as_completed(futs):
            try:
                for e in fu.result():
                    key = e["lien"] or e["audio"] or e["titre"]
                    if key not in seen:          # un même épisode peut être dans plusieurs flux
                        seen.add(key)
                        eps.append(e)
            except Exception as err:
                erreurs.append(f'{futs[fu]["emission"]}: {err}')
    eps.sort(key=lambda e: e["date"], reverse=True)
    return eps, erreurs


# ----------------------------------------------------------------- transcription
class _Collect(HTMLParser):
    """Récupère le texte des blocs dont la classe/id contient « transcri »."""
    def __init__(self):
        super().__init__()
        self.depth, self.parts, self.stack = 0, [], []

    def handle_starttag(self, tag, attrs):
        a = " ".join(v or "" for k, v in attrs if k in ("class", "id", "data-testid")).lower()
        if self.depth or "transcri" in a:
            self.depth += 1
            self.stack.append(tag)

    def handle_endtag(self, tag):
        if self.depth and self.stack:
            self.stack.pop()
            self.depth -= 1

    def handle_data(self, data):
        if self.depth and data.strip():
            self.parts.append(data.strip())


def _json_strings(obj, keyhint=False):
    """Parcourt un JSON et renvoie les phrases rangées sous une clé contenant « transcri »
    (texte d'un seul bloc ou liste de segments {start, text})."""
    if isinstance(obj, dict):
        for k, v in obj.items():
            yield from _json_strings(v, keyhint or "transcri" in str(k).lower())
    elif isinstance(obj, list):
        for v in obj:
            yield from _json_strings(v, keyhint)
    elif isinstance(obj, str) and keyhint and len(obj) > 15 and " " in obj and not obj.startswith("http"):
        yield obj


def _segments_regex(page):
    """Dernier recours : après le mot « transcri », récupère tous les "text":"…" du code de la page."""
    i = page.lower().find("transcri")
    if i < 0:
        return None
    morceaux = re.findall(r'"(?:text|content|sentence)"\s*:\s*"((?:[^"\\]|\\.){15,})"', page[i:])
    if not morceaux:
        return None
    txt = " ".join(json.loads('"' + m + '"') for m in morceaux)
    return txt if len(txt) > 500 else None


class _TextePage(HTMLParser):
    """Récupère le texte éditorial de la page (titres, chapô, paragraphes), sans menus ni pieds de page."""
    SKIP = {"script", "style", "nav", "footer", "header", "aside", "noscript", "svg", "button", "form"}
    GARDE = {"p", "h1", "h2", "h3", "li", "blockquote"}

    def __init__(self):
        super().__init__()
        self.skip, self.cur, self.blocs, self.ld = 0, None, [], []
        self.in_ld = False

    def handle_starttag(self, tag, attrs):
        a = dict(attrs)
        if tag == "script" and a.get("type") == "application/ld+json":
            self.in_ld = True
        if tag in self.SKIP:
            self.skip += 1
        elif tag in self.GARDE and not self.skip:
            self.cur = [tag, []]

    def handle_endtag(self, tag):
        if tag == "script":
            self.in_ld = False
        if tag in self.SKIP and self.skip:
            self.skip -= 1
        elif self.cur and tag == self.cur[0]:
            t = re.sub(r"\s+", " ", " ".join(self.cur[1])).strip()
            if t:
                self.blocs.append(t)
            self.cur = None

    def handle_data(self, data):
        if self.in_ld:
            self.ld.append(data)
        elif self.cur and not self.skip:
            self.cur[1].append(data)


BRUIT = ("vous aimez ce podcast", "rendez-vous sur l'application", "écouter plus tard", "cookies",
         "newsletter", "partager", "s'abonner", "tous droits", "radio france", "lire la suite", "à découvrir")


def texte_page(page):
    """Texte descriptif de l'épisode sur la page radiofrance.fr (JSON-LD + paragraphes)."""
    p = _TextePage()
    try:
        p.feed(page)
    except Exception:
        pass
    morceaux = []
    for raw in p.ld:                           # données structurées éventuelles
        try:
            d = json.loads(raw)
        except Exception:
            continue
        for obj in (d if isinstance(d, list) else [d]):
            if isinstance(obj, dict):
                for k in ("articleBody", "description", "abstract", "text"):
                    v = obj.get(k)
                    if isinstance(v, str) and len(v) > 80:
                        morceaux.append(strip_tags(v))
    vus = set()
    for b in p.blocs:
        bl = b.lower()
        if len(b) < 40 or any(x in bl for x in BRUIT) or bl in vus:
            continue
        vus.add(bl)
        morceaux.append(b)
    txt, vus2 = [], set()
    for m in morceaux:                         # dédoublonnage
        k = m[:80].lower()
        if k not in vus2:
            vus2.add(k); txt.append(m)
    return "\n".join(txt)


_cache_pages = {}


def page_episode(url):
    if url not in _cache_pages:
        _cache_pages[url] = fetch(url).decode("utf-8", "replace")
    return _cache_pages[url]


def transcription(page_url):
    """Essaie de trouver la transcription sur la page radiofrance.fr de l'épisode.
    La structure du site peut changer : si rien n'est trouvé, renvoie None."""
    page = page_episode(page_url)
    p = _Collect()
    p.feed(page)
    txt = " ".join(p.parts)
    if len(txt) > 500:
        return strip_tags(txt)
    for m in re.finditer(r"<script[^>]*>(.*?)</script>", page, re.S):
        body = m.group(1).strip()
        if "transcri" not in body.lower():
            continue
        try:
            data = json.loads(body)
        except Exception:
            mm = re.search(r"(\{.*\})", body, re.S)
            try:
                data = json.loads(mm.group(1)) if mm else None
            except Exception:
                data = None
        if data is not None:
            found = " ".join(_json_strings(data))
            if len(found) > 500:
                return strip_tags(found)
    return _segments_regex(page)


# ----------------------------------------------------------------- Gemini : appel robuste
TEMPORAIRE = ("surchargé", "dépassé", "high demand", "overloaded", "unavailable", "503", "500", "429", "quota", "resource_exhausted", "try again")


def modeles_gemini():
    m = [os.environ.get("GEMINI_MODEL", "").strip(), "gemini-flash-latest", "gemini-flash-lite-latest", "gemini-pro-latest"]
    return [x for i, x in enumerate(m) if x and x not in m[:i]]


def gemini_generate(parts):
    """Appelle Gemini ; si le modèle est surchargé, réessaie puis passe au modèle suivant."""
    derniere = None
    for model in modeles_gemini():
        for essai in range(3):
            try:
                r = _post_json(f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent",
                               {"contents": [{"parts": parts}]}, {"x-goog-api-key": cle_gemini()})
                txt = "".join(p.get("text", "") for p in r["candidates"][0]["content"]["parts"])
                if txt:
                    return txt
            except Exception as err:
                derniere = err
                msg = erreur_lisible(err)
                if "invalide" in msg or "certificats" in msg:
                    raise                              # inutile d'insister
                if "not found" in msg.lower() or "not supported" in msg.lower():
                    break                              # modèle inexistant : suivant
                if not any(t in msg.lower() for t in TEMPORAIRE):
                    raise
                time.sleep(2 + 3 * essai)              # surcharge : on patiente un peu
    raise derniere or RuntimeError("Gemini n'a rien renvoyé")


# ----------------------------------------------------------------- Groq : audio -> texte (Whisper, gratuit)
GROQ = "https://api.groq.com/openai/v1"
_cache_transcr = {}


def cle_groq():
    return (os.environ.get("GROQ_API_KEY") or reglages().get("groq_cle") or "").strip()


def _multipart(champs, fichier, nom="audio.mp3"):
    b = "----rf" + str(int(time.time() * 1000))
    out = []
    for k, v in champs.items():
        out.append(f'--{b}\r\nContent-Disposition: form-data; name="{k}"\r\n\r\n{v}\r\n'.encode())
    out.append(f'--{b}\r\nContent-Disposition: form-data; name="file"; filename="{nom}"\r\n'
               f'Content-Type: audio/mpeg\r\n\r\n'.encode() + fichier + b"\r\n")
    out.append(f"--{b}--\r\n".encode())
    return b"".join(out), "multipart/form-data; boundary=" + b


def groq_transcrire(audio_url):
    """Télécharge le MP3, le découpe en morceaux de 20 Mo (limite gratuite : 25 Mo) et le fait transcrire."""
    if audio_url in _cache_transcr:
        return _cache_transcr[audio_url]
    data = fetch(audio_url, timeout=300)
    taille = 20 * 1024 * 1024
    textes = []
    for i in range(0, len(data), taille):
        morceau = data[i:i + taille]
        for modele in ("whisper-large-v3-turbo", "whisper-large-v3"):
            body, ctype = _multipart({"model": modele, "language": "fr", "response_format": "text"}, morceau)
            req = urllib.request.Request(GROQ + "/audio/transcriptions", body,
                                         {"Authorization": "Bearer " + cle_groq(), "Content-Type": ctype})
            try:
                with urllib.request.urlopen(req, timeout=600) as r:
                    textes.append(r.read().decode("utf-8", "replace").strip())
                break
            except urllib.error.HTTPError as err:
                msg = err.read().decode("utf-8", "replace")
                if "model" in msg.lower() and ("not found" in msg.lower() or "decommission" in msg.lower()):
                    continue            # modèle retiré : on essaie l'autre
                raise RuntimeError(f"Groq ({err.code}) : {msg[:250]}")
    txt = " ".join(textes)
    _cache_transcr[audio_url] = txt
    return txt


def groq_etat():
    if not cle_groq():
        return False, "pas de clé"
    try:
        req = urllib.request.Request(GROQ + "/models", headers={"Authorization": "Bearer " + cle_groq()})
        urllib.request.urlopen(req, timeout=15).read()
        return True, ""
    except urllib.error.HTTPError as err:
        return False, "clé Groq invalide" if err.code == 401 else f"erreur Groq {err.code}"
    except Exception as err:
        return False, str(err)[:200]


# ----------------------------------------------------------------- Gemini écoute l'audio
GEM = "https://generativelanguage.googleapis.com"


def gemini_audio(audio_url, prompt):
    """Télécharge le MP3, l'envoie à Gemini (File API) et lui demande le résumé.
    Utilisé quand la transcription n'est pas trouvée sur la page."""
    key = cle_gemini()
    data = fetch(audio_url, timeout=300)
    # 1) ouverture d'un envoi « resumable »
    req = urllib.request.Request(GEM + "/upload/v1beta/files", json.dumps({"file": {"display_name": "episode"}}).encode(), {
        "x-goog-api-key": key, "Content-Type": "application/json",
        "X-Goog-Upload-Protocol": "resumable", "X-Goog-Upload-Command": "start",
        "X-Goog-Upload-Header-Content-Length": str(len(data)), "X-Goog-Upload-Header-Content-Type": "audio/mpeg"})
    with urllib.request.urlopen(req, timeout=60) as r:
        upload_url = r.headers["X-Goog-Upload-URL"]
    # 2) envoi du fichier
    req = urllib.request.Request(upload_url, data, {
        "Content-Length": str(len(data)), "X-Goog-Upload-Offset": "0", "X-Goog-Upload-Command": "upload, finalize"})
    with urllib.request.urlopen(req, timeout=600) as r:
        f = json.loads(r.read())["file"]
    # 3) attendre que Gemini ait traité le fichier
    for _ in range(60):
        if f.get("state") in (None, "ACTIVE"):
            break
        time.sleep(3)
        req = urllib.request.Request(f"{GEM}/v1beta/{f['name']}", headers={"x-goog-api-key": key})
        with urllib.request.urlopen(req, timeout=30) as r:
            f = json.loads(r.read())
    return gemini_generate([{"file_data": {"mime_type": "audio/mpeg", "file_uri": f["uri"]}}, {"text": prompt}])


# ----------------------------------------------------------------- résumé
STOP = set("""a à au aux avec ce ces cette dans de des du elle en et eux il ils je la le les leur lui
ma mais me même mes moi mon ne nos notre nous on ou où par pas pour qu que qui sa se ses son sur ta te tes
toi ton tu un une vos votre vous c d j l m n s t y été être avoir est sont ont fait plus très aussi bien
comme tout tous toute toutes alors donc car si ça cela ceci là ici dont entre sans sous vers chez""".split())


def resume_extractif(texte, n=6):
    phrases = re.split(r"(?<=[.!?…])\s+", texte)
    phrases = [p for p in phrases if 40 < len(p) < 400]
    if not phrases:
        return texte[:800]
    mots = lambda s: [w for w in re.findall(r"\w+", s.lower()) if w not in STOP and len(w) > 2]
    freq = Counter(w for p in phrases for w in mots(p))
    score = {i: sum(freq[w] for w in mots(p)) / (len(mots(p)) ** 0.5 or 1) for i, p in enumerate(phrases)}
    best = sorted(sorted(score, key=score.get, reverse=True)[:n])
    return " ".join(phrases[i] for i in best)


def _post_json(url, payload, headers, timeout=None):
    req = urllib.request.Request(url, json.dumps(payload).encode(), {"Content-Type": "application/json", **headers})
    with urllib.request.urlopen(req, timeout=timeout or (3600 if "/api/pull" in url else 600)) as r:
        return json.loads(r.read())


def llm_choisi():
    llm = os.environ.get("LLM", "").lower() or reglages().get("moteur", "")
    if llm == "aucun":
        return ""
    if not llm and cle_gemini():
        llm = "gemini"                      # une clé Gemini suffit à l'activer
    return llm


def consigne(titre, source="de sa transcription automatique (elle peut contenir des erreurs de mots)"):
    return (f"Tu résumes pour un auditeur l'émission de radio « {titre} » (Radio France), à partir {source}.\n"
            "But : qu'il sache tout de suite si le sujet l'intéresse, et que le résumé puisse remplacer l'écoute.\n"
            "Écris en français, sans phrase d'introduction du type « Voici le résumé », exactement dans ce format :\n"
            "**Thème** : une seule ligne qui dit de quoi parle l'épisode.\n"
            "**Résumé** : environ 10 lignes de texte suivi et précis (faits, arguments, chiffres, noms, conclusions), "
            "comme un article court. Pour un journal ou une matinale, passe les sujets en revue dans l'ordre.\n"
            "**Invités** : qui parle et à quel titre (une ligne), seulement si c'est mentionné.\n"
            "N'invente rien : uniquement ce qui figure dans la source.")


def resume_ia(texte, titre, source_txt="de sa transcription automatique (elle peut contenir des erreurs de mots)"):
    llm = llm_choisi()
    prompt = consigne(titre, source_txt) + "\n\nSOURCE :\n" + texte[:400000]
    if llm == "gemini":
        return gemini_generate([{"text": prompt}])
    if llm == "anthropic":
        r = _post_json("https://api.anthropic.com/v1/messages",
                       {"model": os.environ.get("ANTHROPIC_MODEL", "claude-haiku-5-5"), "max_tokens": 1200,
                        "messages": [{"role": "user", "content": prompt}]},
                       {"x-api-key": os.environ["ANTHROPIC_API_KEY"], "anthropic-version": "2023-06-01"})
        return "".join(b.get("text", "") for b in r["content"])
    if llm == "ollama":
        r = _post_json(OLLAMA + "/api/chat", {
            "model": modele_ollama(), "stream": False,
            "options": {"num_ctx": 16384, "temperature": 0.2},
            "messages": [{"role": "user", "content": consigne(titre, source_txt) + "\n\nSOURCE :\n" + texte[:45000]}]}, {})
        return r["message"]["content"]
    if llm == "openai":
        base = os.environ.get("OPENAI_BASE_URL", "https://api.openai.com/v1") if llm == "openai" \
            else os.environ.get("OLLAMA_URL", "http://localhost:11434") + "/v1"
        model = os.environ.get("OPENAI_MODEL", "gpt-4o-mini") if llm == "openai" \
            else os.environ.get("OLLAMA_MODEL", "mistral")
        hdr = {"Authorization": "Bearer " + os.environ.get("OPENAI_API_KEY", "ollama")}
        r = _post_json(base + "/chat/completions",
                       {"model": model, "messages": [{"role": "user", "content": prompt}]}, hdr)
        return r["choices"][0]["message"]["content"]
    return None


def resumer(lien, titre, description, audio="", colle=""):
    """Résumé à partir du texte : transcription (si dispo) + texte descriptif de la page radiofrance.fr."""
    alertes = []
    src_ia = "du texte publié sur sa page web"
    transcr, descr_page = None, ""
    if lien:
        try:
            transcr = transcription(lien)
        except Exception as err:
            print("transcription:", err, file=sys.stderr)
        try:
            descr_page = texte_page(page_episode(lien))
        except Exception as err:
            print("page:", err, file=sys.stderr)
            alertes.append("Page radiofrance.fr inaccessible : " + str(err)[:150])
    if transcr:
        source = "transcription + texte de la page radiofrance.fr"
        texte = "PRÉSENTATION DE L'ÉPISODE :\n" + descr_page + "\n\nTRANSCRIPTION :\n" + transcr
        src_ia = "de sa présentation et de sa transcription automatique (qui peut contenir des erreurs de mots)"
    elif len(descr_page) >= 150:
        source, texte = "texte de la page radiofrance.fr", descr_page
    else:
        source, texte = "courte description du flux (texte de la page introuvable)", (descr_page + "\n" + description).strip()
        src_ia = "de sa courte description"
        alertes.append("Texte de la page non trouvé : clique sur « Diagnostic » et envoie-moi le fichier pour que je corrige.")
    if not llm_choisi() and reglages().get("moteur") != "aucun":
        alertes.append("Aucune IA activée : choisis Gemini ou Ollama en haut de la page.")
    try:
        r = resume_ia(texte, titre, src_ia)
        if r:
            return {"source": source, "moteur": llm_choisi(), "resume": r, "alertes": alertes}
    except Exception as err:
        print("IA:", err, file=sys.stderr)
        alertes.append(llm_choisi() + " : " + erreur_lisible(err))
    return {"source": source, "moteur": "extractif (sans IA)", "resume": resume_extractif(texte), "alertes": alertes}


def tester_gemini():
    """Vérifie juste que la clé est valide (liste des modèles), sans consommer de quota."""
    if llm_choisi() != "gemini":
        return (False, "pas de clé") if not llm_choisi() else (True, "")
    try:
        req = urllib.request.Request("https://generativelanguage.googleapis.com/v1beta/models?pageSize=1",
                                     headers={"x-goog-api-key": cle_gemini()})
        urllib.request.urlopen(req, timeout=20).read()
        return True, ""
    except Exception as err:
        return False, erreur_lisible(err)


# ----------------------------------------------------------------- page web
PAGE = """<!doctype html><html lang="fr"><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Radio France – sorties du jour</title>
<style>
body{font:15px/1.45 system-ui,sans-serif;max-width:900px;margin:24px auto;padding:0 16px;color:#1d1d1f;background:#fafafa}
h1{font-size:22px;margin:0 0 4px} .meta{color:#666;margin-bottom:16px}
.bar{display:flex;gap:8px;flex-wrap:wrap;margin-bottom:16px}
input,select,button{font:inherit;padding:6px 10px;border:1px solid #ccc;border-radius:6px;background:#fff}
button{cursor:pointer} button:hover{background:#f0f0f0}
.ep{background:#fff;border:1px solid #e5e5e5;border-radius:10px;padding:12px 14px;margin-bottom:10px}
.top{display:flex;gap:10px;align-items:baseline;flex-wrap:wrap}
.h{font-weight:600;font-variant-numeric:tabular-nums} .c{font-size:12px;padding:2px 8px;border-radius:99px;background:#eef}
.e{color:#555} .t{font-weight:600;margin:4px 0} .d{color:#333;margin:4px 0 8px}
.actions{display:flex;gap:8px;flex-wrap:wrap;font-size:14px} .actions a{color:#0057b8}
.r{white-space:pre-wrap;background:#f6f7fb;border-radius:8px;padding:10px;margin-top:8px;display:none}
.small{font-size:12px;color:#777}
.pub .diag{display:none}
@media(max-width:600px){body{margin:12px auto;font-size:16px} .bar input,.bar select{flex:1 1 100%} .actions button{padding:8px 12px}}
.colle{display:none;margin-top:8px} .colle textarea{width:100%;height:110px;font:inherit;border:1px solid #ccc;border-radius:6px;padding:6px;box-sizing:border-box}
.gem{background:#fff;border:1px solid #e5e5e5;border-radius:10px;padding:8px 12px;margin-bottom:12px;font-size:14px}
.gem input{width:280px} .warn{color:#b00020;font-size:13px;margin-top:6px}
</style>
<h1>Radio France – sorties du jour</h1>
<div class="meta" id="meta">Chargement des flux…</div>
<div class="gem" id="gem"></div>
<div class="bar">
 <input type="date" id="day"> <select id="ch"><option value="">Toutes les chaînes</option></select>
 <input id="q" placeholder="Filtrer (titre, émission…)" size="28"> <button id="reload">Actualiser</button>
 <button id="stop" title="Arrête l'outil (relance-le depuis l'app)">Quitter</button>
</div>
<div id="list"></div>
<script>
const $=s=>document.querySelector(s); let EPS=[];
function esc(s){return (s||"").replace(/[&<>"]/g,c=>({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;"}[c]))}
async function load(){
  $("#meta").textContent="Chargement des flux…"; $("#list").innerHTML="";
  const r=await fetch("/api/episodes?day="+$("#day").value); const j=await r.json(); EPS=j.episodes;
  $("#meta").textContent=`${EPS.length} épisodes le ${j.day} · ${j.nb_flux} flux lus · ${j.erreurs.length} flux en erreur · ${j.secondes}s`;
  const chs=[...new Set(EPS.map(e=>e.chaine))].sort(); const cur=$("#ch").value;
  $("#ch").innerHTML='<option value="">Toutes les chaînes</option>'+chs.map(c=>`<option ${c==cur?"selected":""}>${esc(c)}</option>`).join("");
  render();
}
function render(){
  const q=$("#q").value.toLowerCase(), ch=$("#ch").value;
  $("#list").innerHTML=EPS.map((e,i)=>({e,i})).filter(({e})=>(!ch||e.chaine==ch)&&(!q||(e.titre+e.emission+e.description).toLowerCase().includes(q)))
   .map(({e,i})=>`<div class="ep"><div class="top"><span class="h">${e.heure}</span><span class="c">${esc(e.chaine)}</span><span class="e">${esc(e.emission)}</span>${e.duree?`<span class="small">${esc(e.duree)}</span>`:""}</div>
   <div class="t">${esc(e.titre)}</div><div class="d">${esc(e.description)}</div>
   <div class="actions">${e.lien?`<a href="${esc(e.lien)}" target="_blank">Page</a>`:""}${e.audio?`<a href="${esc(e.audio)}" target="_blank">Audio</a>`:""}
   <button onclick="resume(${i},this)">Résumer</button>
   ${e.lien?`<button onclick="voirTexte(${i},this)">Texte de la page</button>`:""}
   ${e.lien?`<a class="diag" href="/api/page?url=${encodeURIComponent(e.lien)}">Diagnostic</a>`:""}</div>
   <div class="r" id="x${i}"></div>
   <div class="r" id="r${i}"></div></div>`).join("")||"<p>Aucun épisode.</p>";
}
async function voirTexte(i,btn){const b=$("#x"+i); if(b.style.display=="block"){b.style.display="none";return;}
  b.style.display="block"; b.textContent="Chargement du texte…";
  const j=await (await fetch("/api/texte?url="+encodeURIComponent(EPS[i].lien))).json(); b.textContent=j.texte;}
async function resume(i,btn){
  const e=EPS[i], box=$("#r"+i); box.style.display="block"; box.textContent="Résumé en cours…"; btn.disabled=true;
  const r=await fetch("/api/resume",{method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify(e)});
  const j=await r.json(); btn.disabled=false;
  const al=(j.alertes||[]).map(a=>`<div class="warn">⚠ ${esc(a)}</div>`).join("");
  const md=s=>s.replace(/\\*\\*(.+?)\\*\\*/g,"<b>$1</b>").replace(/^\\s*[-*] /gm,"• ").replace(/^#+\\s*/gm,"");
  box.innerHTML=md(esc(j.resume))+`<div class="small">Source : ${esc(j.source)} · Moteur : ${esc(j.moteur)}</div>`+al;
}
$("#day").value=new Date().toLocaleDateString("sv-SE",{timeZone:"Europe/Paris"});
async function gem(){
  const j=await (await fetch("/api/status")).json(), m=j.moteur;
  if(j.public){ $("#gem").style.display="none"; $("#stop").style.display="none"; document.body.classList.add("pub"); return; }
  const sel=`Moteur : <select id="mo"><option value="gemini" ${m=="gemini"?"selected":""}>Gemini (en ligne)</option>
    <option value="ollama" ${m=="ollama"?"selected":""}>Ollama (sur ce Mac)</option><option value="aucun" ${m=="aucun"?"selected":""}>Sans IA</option></select> `;
  let det="";
  if(m=="gemini") det=(j.gemini_ok?`<b style="color:#0a7d32">✔ actif</b> (clé …${esc(j.cle_fin)})`:`<b style="color:#b00020">✘ ${esc(j.gemini_msg||"pas de clé")}</b>`)
     +`<br><input id="k" type="password" placeholder="Colle ta clé Gemini (AIza…)"> <button id="ks">${j.gemini_ok?"Changer la clé":"Enregistrer"}</button>`;
  if(m=="ollama") det=(j.ollama_ok?`<b style="color:#0a7d32">✔ prêt</b>`:`<b style="color:#b00020">✘ ${esc(j.ollama_msg)}</b>`)
     +`<br>Modèle : <input id="om" value="${esc(j.ollama_modele)}" size="14"> <button id="omok">Utiliser</button> <button id="pull">Télécharger le modèle</button>`;
  if(m=="aucun") det=`<span class="small">Résumés basiques sans IA. Choisis Gemini (colle ta clé) ou Ollama pour de vrais résumés.</span>`;
  $("#gem").innerHTML=sel+det;
  $("#mo").onchange=async()=>{await post("/api/reglages",{moteur:$("#mo").value}); gem();};
  if($("#ks")) $("#ks").onclick=async()=>{const k=$("#k").value.trim(); if(!k)return; $("#gem").textContent="Test de la clé…"; await post("/api/cle",{cle:k}); gem();};
  if($("#omok")) $("#omok").onclick=async()=>{await post("/api/reglages",{ollama_modele:$("#om").value.trim()}); gem();};
  if($("#pull")) $("#pull").onclick=async()=>{$("#gem").textContent="Téléchargement du modèle en cours… (plusieurs minutes, quelques Go — laisse la page ouverte)";
     const r=await post("/api/ollama_pull",{}); if(!r.ok) alert("Échec : "+r.msg+" — Ollama est-il bien ouvert ?"); gem();};
}
async function post(u,b){return (await fetch(u,{method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify(b)})).json();}
gem();
$("#stop").onclick=async()=>{await fetch("/api/stop",{method:"POST"});document.body.innerHTML="<h1>Outil arrêté.</h1><p>Relance l'app pour l'utiliser à nouveau.</p>"};
$("#reload").onclick=load; $("#day").onchange=load; $("#q").oninput=render; $("#ch").onchange=render; load();
</script></html>"""


def make_handler(feeds):
    cache, lock = {}, threading.Lock()

    class H(BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def _send(self, code, body, ctype="application/json; charset=utf-8"):
            b = body if isinstance(body, bytes) else body.encode()
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(b)))
            self.end_headers()
            self.wfile.write(b)

        def _ip(self):
            return (self.headers.get("X-Forwarded-For") or self.client_address[0]).split(",")[0].strip()

        def do_GET(self):
            u = urllib.parse.urlparse(self.path)
            if PUBLIC and u.path in ("/api/page",):
                return self._send(403, '{"erreur":"désactivé en ligne"}')
            if u.path == "/":
                return self._send(200, PAGE, "text/html; charset=utf-8")
            if u.path == "/api/episodes":
                q = urllib.parse.parse_qs(u.query)
                try:
                    day = date.fromisoformat(q.get("day", [""])[0])
                except ValueError:
                    day = datetime.now(PARIS).date()
                t0 = time.time()
                with lock:
                    hit = cache.get(day)
                    if not hit or time.time() - hit[0] > 300:      # cache 5 min
                        eps, err = sorties_du_jour(feeds, day)
                        hit = cache[day] = (time.time(), eps, err)
                return self._send(200, json.dumps({
                    "day": day.isoformat(), "episodes": hit[1], "erreurs": hit[2],
                    "nb_flux": len(feeds), "secondes": round(time.time() - t0, 1)}, ensure_ascii=False))
            if u.path == "/api/page":
                url = urllib.parse.parse_qs(u.query).get("url", [""])[0]
                try:
                    b = fetch(url)
                except Exception as err:
                    b = f"Erreur en récupérant {url} : {err}".encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/octet-stream")
                self.send_header("Content-Disposition", 'attachment; filename="page_episode_radiofrance.html"')
                self.send_header("Content-Length", str(len(b)))
                self.end_headers()
                self.wfile.write(b)
                return
            if u.path == "/api/texte":
                url = urllib.parse.parse_qs(u.query).get("url", [""])[0]
                if not url_autorisee(url):
                    return self._send(200, json.dumps({"texte": "Adresse non autorisée."}, ensure_ascii=False))
                try:
                    t = texte_page(page_episode(url)) or "(aucun texte trouvé sur la page)"
                except Exception as err:
                    t = f"Erreur : {err}"
                return self._send(200, json.dumps({"texte": t}, ensure_ascii=False))
            if u.path == "/api/ping":
                return self._send(200, json.dumps({"ok": True, "version": VERSION}))
            if u.path == "/api/status":
                ok, msg = tester_gemini()
                k = cle_gemini()
                o_ok, o_msg = ollama_etat() if llm_choisi() == "ollama" else (False, "")
                return self._send(200, json.dumps({"version": VERSION, "public": PUBLIC, "gemini_ok": ok, "gemini_msg": msg,
                                                   "cle_fin": k[-4:] if k else "", "moteur": llm_choisi() or "aucun",
                                                   "ollama_ok": o_ok, "ollama_msg": o_msg,
                                                   "ollama_modele": modele_ollama(),
                                                   "groq": dict(zip(("ok", "msg"), groq_etat()))}, ensure_ascii=False))
            self._send(404, "{}")

        def do_POST(self):
            if PUBLIC and self.path != "/api/resume":
                return self._send(403, '{"erreur":"désactivé en ligne"}')
            if self.path == "/api/stop":
                self._send(200, '{"ok":true}')
                threading.Thread(target=lambda: (time.sleep(0.5), os._exit(0))).start()
                return
            if self.path == "/api/reglages":
                e = json.loads(self.rfile.read(int(self.headers.get("Content-Length", 0))))
                enregistrer_reglages(**{k: v for k, v in e.items() if k in ("moteur", "ollama_modele", "groq_cle")})
                return self._send(200, '{"ok":true}')
            if self.path == "/api/ollama_pull":
                try:
                    _post_json(OLLAMA + "/api/pull", {"model": modele_ollama(), "stream": False}, {})
                    return self._send(200, '{"ok":true}')
                except Exception as err:
                    return self._send(200, json.dumps({"ok": False, "msg": str(err)}, ensure_ascii=False))
            if self.path == "/api/cle":
                e = json.loads(self.rfile.read(int(self.headers.get("Content-Length", 0))))
                enregistrer_cle(e.get("cle", ""))
                return self._send(200, '{"ok":true}')
            if self.path != "/api/resume":
                return self._send(404, "{}")
            e = json.loads(self.rfile.read(int(self.headers.get("Content-Length", 0))))
            lien = e.get("lien", "")
            if lien and not url_autorisee(lien):
                return self._send(200, json.dumps({"source": "", "moteur": "", "resume": "Adresse non autorisée.", "alertes": []}))
            if lien in _cache_resumes:                    # déjà résumé : on ressert (gratuit, instantané)
                return self._send(200, json.dumps(_cache_resumes[lien], ensure_ascii=False))
            if PUBLIC:
                ok, msg = quota_ok(self._ip())
                if not ok:
                    return self._send(200, json.dumps({"source": "", "moteur": "", "resume": msg, "alertes": []}, ensure_ascii=False))
            r = resumer(e.get("lien", ""), e.get("titre", ""), e.get("description", ""), e.get("audio", ""), e.get("texte", ""))
            if lien and r.get("moteur") not in ("", "extractif (sans IA)") and not r.get("alertes"):
                _cache_resumes[lien] = r
            self._send(200, json.dumps(r, ensure_ascii=False))
    return H


# ----------------------------------------------------------------- main
def main():
    ap = argparse.ArgumentParser(description="Sorties du jour des podcasts Radio France")
    ap.add_argument("--feeds", default=os.path.join(HERE, "radiofrance_podcasts_rss.json"))
    ap.add_argument("--top51", action="store_true", help="seulement les 51 principaux flux")
    ap.add_argument("--port", type=int, default=8765)
    ap.add_argument("--texte", action="store_true", help="affiche la liste dans le terminal, sans page web")
    ap.add_argument("--jour", help="AAAA-MM-JJ (défaut : aujourd'hui)")
    ap.add_argument("--pas-de-navigateur", action="store_true", help="n'ouvre pas le navigateur")
    ap.add_argument("--public", action="store_true", help="mode site en ligne (hébergement)")
    a = ap.parse_args()
    feeds = load_feeds(a.feeds, a.top51)

    if a.texte:
        day = date.fromisoformat(a.jour) if a.jour else datetime.now(PARIS).date()
        eps, err = sorties_du_jour(feeds, day)
        print(f"\n{len(eps)} sorties le {day} ({len(err)} flux en erreur)\n")
        for e in eps:
            print(f"{e['heure']}  [{e['chaine']}] {e['emission']} — {e['titre']}")
            if e["description"]:
                print("        " + e["description"][:220])
        return

    global PUBLIC
    if a.public:
        PUBLIC = True
    host, port = ("0.0.0.0", int(os.environ.get("PORT", a.port))) if PUBLIC else ("127.0.0.1", a.port)
    srv = ThreadingHTTPServer((host, port), make_handler(feeds))
    if PUBLIC:
        print(f"Mode en ligne sur le port {port} – moteur : {llm_choisi() or 'aucun (GEMINI_API_KEY manquante)'}", flush=True)
        threading.Thread(target=lambda: urllib.request.urlopen(f"http://127.0.0.1:{port}/api/episodes", timeout=600),
                         daemon=True).start()        # préchauffe la liste du jour
        srv.serve_forever()
        return
    url = f"http://localhost:{a.port}"
    print(f"{len(feeds)} flux chargés. Ouvre {url}  (Ctrl+C pour arrêter)")
    if not a.pas_de_navigateur:
        threading.Timer(1, lambda: webbrowser.open(url)).start()
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
