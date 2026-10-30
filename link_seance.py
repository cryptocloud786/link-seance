#!/usr/bin/env python3
"""
Link Séance: find the dead links in a list, summon their last snapshot from the
Internet Archive, and let a small local open-weight model (Gemma) write each one
an epitaph. The results are laid on an ofrenda: OFRENDA.md and ofrenda.html.

Offline-first: archive lookups are cached on disk, the model runs locally, and
`--offline` replays a séance from the cache with no network at all.

MIT License. https://github.com/cryptocloud786/link-seance
"""
from __future__ import annotations

import argparse
import datetime as dt
import glob
import gzip
import hashlib
import html
import json
import os
import re
import socket
import ssl
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import zlib
from html.parser import HTMLParser

__version__ = "0.1.0"

UA = "link-seance/0.1 (+https://github.com/cryptocloud786/link-seance)"
BROWSER_UA = "Mozilla/5.0 (X11; Linux x86_64) link-seance/0.1"
CACHE_DIR = ".seance_cache"
URL_RE = re.compile(r"""https?://[^\s<>"'\)\]]+""", re.I)

# ----------------------------------------------------------------- terminal ---
USE_COLOR = (sys.stdout.isatty() or bool(os.environ.get("FORCE_COLOR"))) and os.environ.get("NO_COLOR") is None


def c(text: str, code: str) -> str:
    return f"\033[{code}m{text}\033[0m" if USE_COLOR else text


def say(msg: str = "") -> None:
    print(msg, flush=True)


# -------------------------------------------------------------------- cache ---
class Cache:
    """Tiny JSON-on-disk cache so a séance can be replayed offline."""

    def __init__(self, root: str, offline: bool):
        self.root, self.offline = root, offline
        os.makedirs(root, exist_ok=True)

    def _path(self, key: str) -> str:
        return os.path.join(self.root, hashlib.sha1(key.encode()).hexdigest() + ".json")

    def get(self, key: str):
        p = self._path(key)
        if os.path.exists(p):
            with open(p, encoding="utf-8") as f:
                return json.load(f)
        return None

    def put(self, key: str, value) -> None:
        with open(self._path(key), "w", encoding="utf-8") as f:
            json.dump(value, f)


# ---------------------------------------------------------------- input ---
def extract_urls(text: str) -> list[str]:
    """Pull URLs out of plain text, Markdown, or a browser bookmarks.html export."""
    seen, out = set(), []
    for m in URL_RE.finditer(html.unescape(text)):
        url = m.group(0).rstrip(".,;:!?*_")
        if "web.archive.org" in url or url in seen:
            continue
        seen.add(url)
        out.append(url)
    return out


# ---------------------------------------------------------------- network ---
def http_get(url: str, timeout: float = 15.0, max_bytes: int = 600_000, ua: str = UA):
    req = urllib.request.Request(url, headers={"User-Agent": ua, "Accept": "*/*"})
    ctx = ssl.create_default_context()
    with urllib.request.urlopen(req, timeout=timeout, context=ctx) as r:
        body = r.read(max_bytes)
        if body[:2] == b"\x1f\x8b":  # archived bytes are sometimes still gzipped
            try:
                body = gzip.decompress(body)
            except (OSError, EOFError):
                body = zlib.decompressobj(31).decompress(body)
        charset = r.headers.get_content_charset() or "utf-8"
        return r.status, r.geturl(), body, charset


def _host(u: str) -> str:
    h = (urllib.parse.urlsplit(u).hostname or "").lower()
    return h[4:] if h.startswith("www.") else h


def _probe(target: str, url: str) -> dict:
    try:
        status, final, body, charset = http_get(target, timeout=12, max_bytes=65536, ua=BROWSER_UA)
        if _host(final) != _host(url):
            g = _TextGrabber()
            try:
                g.feed(body.decode(charset, errors="replace"))
            except Exception:  # noqa: BLE001
                pass
            return {"state": "wandering", "reason": f"redirects away to {final}", "final_url": final,
                    "final_title": re.sub(r"\s+", " ", g.title).strip()}
        return {"state": "alive", "reason": f"HTTP {status}", "final_url": final}
    except urllib.error.HTTPError as e:
        if e.code in (429, 503) and _host(e.geturl() or target) != _host(url):
            return {"state": "wandering", "final_url": e.geturl(), "http": True,
                    "reason": f"redirects away to {_host(e.geturl())} (which is rate-limiting us)"}
        if e.code in (429, 503):  # busy or rate-limited is not dead; don't mourn it
            return {"state": "unknown", "reason": f"HTTP {e.code} (busy or rate-limited, try later)",
                    "final_url": target, "http": True}
        final = e.geturl() or target
        where = f" (at {final})" if final not in (url, target) else ""
        return {"state": "dead", "reason": f"HTTP {e.code} {e.reason}{where}", "final_url": final,
                "http": True}
    except urllib.error.URLError as e:
        r = e.reason
        if isinstance(r, socket.gaierror):
            return {"state": "dead", "reason": "domain no longer resolves (DNS)", "final_url": url,
                    "dns": True}
        if isinstance(r, (socket.timeout, TimeoutError)):
            why = "no answer (timeout)"
        elif isinstance(r, ssl.SSLError):
            why = "broken TLS certificate" if "CERTIFICATE" in str(r).upper() else "broken TLS"
        else:
            why = f"unreachable ({r})"
        return {"state": "dead", "reason": why, "final_url": url}
    except (socket.timeout, TimeoutError):
        return {"state": "dead", "reason": "no answer (timeout)", "final_url": url}
    except Exception as e:  # noqa: BLE001 - any failure to connect is a kind of death
        return {"state": "dead", "reason": f"unreachable ({e.__class__.__name__})", "final_url": url}


def check_pulse(url: str, cache: Cache) -> dict:
    """Is the page still alive? Returns {state, reason, final_url}.

    state is one of:
      alive     - answers with 2xx on its own host
      dead      - DNS gone, connection refused, timeout, or HTTP 4xx/5xx
      wandering - answers, but only by redirecting to some other host
                  (a shutdown notice, a parent company, a new owner)
    """
    key = "pulse:" + url
    hit = cache.get(key)
    if hit and hit.get("state") == "unknown" and not cache.offline:
        hit = None  # ask again next time
    if hit or cache.offline:
        return hit or {"state": "unknown", "reason": "not in cache (offline)", "final_url": url}

    res = _probe(url, url)
    res.pop("http", None)
    res.pop("dns", None)
    if res["state"] == "unknown":
        return res
    cache.put(key, res)
    return res


# ---------------------------------------------------------------- wayback ---
def _cdx(url: str, extra: str, cache: Cache):
    q = ("https://web.archive.org/cdx/search/cdx?url=" + urllib.parse.quote(url, safe="")
         + "&output=json&fl=timestamp,original,statuscode" + extra)
    key = "cdx:" + q
    hit = cache.get(key)
    if hit or cache.offline:
        return hit
    rows = None
    for attempt in range(3):
        try:
            _, _, body, _ = http_get(q, timeout=40)
            data = json.loads(body.decode("utf-8") or "[]")
            rows = data[1:] if data else []
            if rows:
                break
        except Exception:  # noqa: BLE001 - the archive is busy; try again gently
            pass
        time.sleep(2 + 3 * attempt)
    if rows:  # an empty answer may just mean "busy", so it is never cached
        cache.put(key, rows)
    return rows


def _availability(url: str, cache: Cache):
    """Fallback: the public Wayback availability API (closest-to-now snapshot)."""
    q = "https://archive.org/wayback/available?timestamp=20991231&url=" + urllib.parse.quote(url, safe="")
    key = "avail:" + q
    hit = cache.get(key)
    if hit is not None or cache.offline:
        return hit
    try:
        _, _, body, _ = http_get(q, timeout=30)
        snap = json.loads(body).get("archived_snapshots", {}).get("closest")
        hit = snap if snap and snap.get("available") and snap.get("status") == "200" else {}
    except Exception:  # noqa: BLE001
        return None
    cache.put(key, hit)
    return hit


class _TextGrabber(HTMLParser):
    SKIP = {"script", "style", "noscript", "svg", "head", "template", "iframe"}

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.depth, self.parts, self.title, self.desc, self._in_title = 0, [], "", "", False

    def handle_starttag(self, tag, attrs):
        a = dict(attrs)
        if tag == "title":
            self._in_title = True
        if tag == "meta" and (a.get("name") or a.get("property") or "").lower() in (
                "description", "og:description") and not self.desc:
            self.desc = (a.get("content") or "").strip()
        if tag in self.SKIP:
            self.depth += 1

    def handle_endtag(self, tag):
        if tag == "title":
            self._in_title = False
        if tag in self.SKIP and self.depth:
            self.depth -= 1

    def handle_data(self, data):
        if self._in_title:
            self.title += data
        elif not self.depth:
            t = data.strip()
            if t:
                self.parts.append(t)


def read_snapshot(snap: dict, cache: Cache) -> dict:
    """Fetch the raw archived HTML (id_ = no Wayback toolbar) and keep its words."""
    raw = f"https://web.archive.org/web/{snap['timestamp']}id_/{snap['original']}"
    key = "snap:" + raw
    hit = cache.get(key)
    if hit is not None or cache.offline:
        return hit or {"title": "", "description": "", "text": ""}
    out = {"title": "", "description": "", "text": ""}
    for attempt in range(3):
        try:
            _, _, body, charset = http_get(raw, timeout=40)
            try:
                doc = body.decode(charset, errors="replace")
            except LookupError:
                doc = body.decode("utf-8", errors="replace")
            g = _TextGrabber()
            g.feed(doc)
            text = re.sub(r"\s+", " ", " ".join(g.parts)).strip()
            out = {"title": re.sub(r"\s+", " ", g.title).strip(),
                   "description": g.desc, "text": text[:4000]}
            break
        except Exception:  # noqa: BLE001
            time.sleep(2 + 3 * attempt)
    cache.put(key, out)
    return out


JUNK = re.compile(r"petabox|wayback machine|internet archive|\b404\b|not found|no such file|"
                  r"\berror\b|for sale|parked|suspended|forbidden|access denied|index of /|"
                  r"moved permanently|object moved|redirecting", re.I)


def looks_alive(mem: dict, pulse: dict) -> bool:
    """Was the page still itself in this snapshot, or already a tombstone / error / impostor?"""
    blob = " ".join([mem["title"], mem["description"], mem["text"][:300]]).strip()
    if len(blob) < 25:
        return False
    if sum(ch.isprintable() for ch in blob) / len(blob) < 0.95:
        return False
    if JUNK.search(mem["title"]) or JUNK.search(blob[:200]):
        return False
    now = (pulse.get("final_title") or "").strip().lower()
    return not (now and mem["title"].strip().lower() == now)


def summon(url: str, pulse: dict, cache: Cache):
    """Find the last snapshot in which the page was still itself.

    The archive keeps recording a URL after it dies (error pages, redirects to a
    parent company), so the newest HTTP 200 capture is often a tombstone. We take
    one capture per month and binary-search for the moment the page stopped
    being itself. Returns (snapshot, memory) or (None, None).
    """
    rows = _cdx(url, "&filter=statuscode:200&collapse=timestamp:6", cache) or []
    if not rows:
        avail = _availability(url, cache)
        if not avail:
            return None, None
        rows = [[avail["timestamp"], url, "200"]]
    born = rows[0][0]

    seen: dict[int, dict] = {}

    def mem_at(i: int) -> dict:
        if i not in seen:
            seen[i] = read_snapshot({"timestamp": rows[i][0], "original": rows[i][1]}, cache)
        return seen[i]

    def ok(i: int) -> bool:
        return looks_alive(mem_at(i), pulse)

    pick = None
    if ok(len(rows) - 1):
        pick = len(rows) - 1
    else:
        lo, hi = 0, len(rows) - 1  # invariant: rows[hi] is a tombstone
        if ok(lo):
            while hi - lo > 1:
                mid = (lo + hi) // 2
                if ok(mid):
                    lo = mid
                else:
                    hi = mid
            pick = lo
        else:  # not monotonic; walk forward a little from the start
            for i in range(1, min(len(rows) - 1, 8)):
                if ok(i):
                    pick = i
                    break
    if pick is None:  # only shells survive; keep the oldest memory we read
        pick = min(seen)
    snap = {"timestamp": rows[pick][0], "original": rows[pick][1], "first_seen": born,
            "alive": looks_alive(mem_at(pick), pulse)}
    return snap, mem_at(pick)


def human_date(ts: str | None) -> str:
    if not ts:
        return "unknown"
    return dt.datetime.strptime(ts[:8], "%Y%m%d").strftime("%-d %B %Y")


# ------------------------------------------------------------------ model ---
STOP = set("""a an the this that these those it its here lies lay rest peace was were is are
be been being and or but of to in on at for from by with as into onto than then so such
your you we our us i my me they them their he she his her who whom which what when where
why how all any each every some no not only once now still ever never forever farewell
goodbye rip may sleep gentle gently quiet quietly soft softly remembered remember memory
web page site home homepage internet online link archive archived last seen alive dead
lost gone light candle candles ofrenda spirit spirits ghost ghosts beloved dear old""".split())


FIRST_PERSON = re.compile(r"\b(we|we're|we've|our|us|i|i'm|my)\b", re.I)


def _words(s: str) -> set[str]:
    s = re.sub(r"([a-z])([A-Z])", r"\1 \2", s)  # "SiliconValley" also counts as "Silicon Valley"
    return set(re.findall(r"[a-z]+|[0-9]+", s.lower()))


def unsupported_terms(epitaph: str, source: str) -> list[str]:
    """Names and numbers in the epitaph that never appear in what the archive gave us.

    A 1B model can be wonderfully poetic and confidently wrong. Any capitalised
    word (mid-sentence) or number it writes must be traceable to the retrieved
    text, otherwise the epitaph is rejected.
    """
    allowed = _words(source) | STOP
    bad = []
    for sent in re.split(r"(?<=[.!?])\s+", epitaph):
        toks = re.findall(r"[A-Za-z][A-Za-z0-9'’\-]*|\d[\d,.]*", sent)
        for i, t in enumerate(toks):
            is_name = t[0].isupper() and i > 0 and len(t) > 1
            is_num = t[0].isdigit()
            if not (is_name or is_num):
                continue
            parts = _words(t)
            if parts and not parts <= allowed:
                bad.append(t)
    return sorted(set(bad))


def _stem(w: str) -> str:
    return w[:5]


def grounding(sentence: str, source: str, title: str = "") -> tuple[float, list[str]]:
    """Share of the content words in a sentence (beyond the page's own title)
    that the archive itself used, plus the words it did not use."""
    src = {_stem(w) for w in _words(source)}
    skip = STOP | _words(title)
    words = [w for w in _words(sentence) if len(w) > 3 and w not in skip]
    if not words:
        return 1.0, []
    foreign = [w for w in words if _stem(w) not in src]
    return 1 - len(foreign) / len(words), foreign


def verify(epitaph: str, source: str, title: str = "") -> str | None:
    """Returns None if the epitaph may be laid on the ofrenda, else the reason it may not."""
    if not epitaph.lower().startswith("here lies"):
        return "does not begin 'Here lies'"
    if FIRST_PERSON.search(epitaph):
        return "speaks in the first person"
    bad = unsupported_terms(epitaph, source)
    if bad:
        return "invented names or numbers: " + ", ".join(bad)
    parts = sentences(epitaph, title)
    if len(parts) < 2:
        return "needs two sentences"
    first = parts[0]
    score, foreign = grounding(first, source, title)
    if score < 0.6:
        return "first sentence uses words the archive never said: " + ", ".join(foreign)
    return None


class Medium:
    """The local open-weight model that speaks for the dead."""

    def __init__(self, backend: str, model: str | None, threads: int | None):
        self.backend, self.name = backend, model or ""
        if backend == "llama.cpp":
            from llama_cpp import Llama  # pip install llama-cpp-python
            path = model or next(iter(sorted(glob.glob("models/*.gguf"))), None)
            if not path or not os.path.exists(path):
                sys.exit("No GGUF model found. Put one in ./models/ or pass --model PATH (see README).")
            self.name = os.path.basename(path)
            self.llm = Llama(model_path=path, n_ctx=4096, n_threads=threads or os.cpu_count(),
                             verbose=False, seed=31)
        elif backend == "ollama":
            self.name = model or "gemma3:1b"
            self.host = os.environ.get("OLLAMA_HOST", "http://localhost:11434")
        else:
            sys.exit(f"unknown backend {backend}")

    def ask(self, prompt: str, temperature: float) -> str:
        if self.backend == "llama.cpp":
            r = self.llm.create_chat_completion(
                messages=[{"role": "user", "content": prompt}],
                max_tokens=110, temperature=temperature, top_p=0.9, repeat_penalty=1.1)
            return r["choices"][0]["message"]["content"].strip()
        body = json.dumps({"model": self.name, "stream": False,
                           "messages": [{"role": "user", "content": prompt}],
                           "options": {"temperature": temperature, "num_predict": 110}}).encode()
        req = urllib.request.Request(self.host + "/api/chat", data=body,
                                     headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=300) as r:
            return json.loads(r.read())["message"]["content"].strip()


PROMPT = """You write short, gentle epitaphs for web pages that no longer exist.
Use ONLY facts found in the ARCHIVED TEXT. Never add names, dates, numbers, people
or features that are not written there. If the text is unclear, stay vague rather
than guess. Do not mention any dates; they are recorded separately.
Speak as a mourner, in the third person and past tense. Never write "we", "us" or "I".

Write exactly two sentences, under 40 words in total.
Sentence 1 starts with "Here lies", names the page by its title, and says plainly
what it was, using words from the archived text.
Sentence 2 is a tender farewell.

Example of the style (for a different page):
Here lies Soup Swap, the page where neighbours traded winter recipes. Rest now, little kitchen; the pot is still warm.

URL: {url}
PAGE TITLE: {title}
ARCHIVED TEXT:
\"\"\"{text}\"\"\"

Epitaph:"""


def sentences(text: str, title: str = "") -> list[str]:
    """Split into sentences without breaking inside the page title ("Yahoo! GeoCities")."""
    mask = "\u0000TITLE\u0000"
    t = text.replace(title, mask) if title else text
    return [p.replace(mask, title) for p in re.split(r"(?<=[.!?])\s+", t) if p]


def clean(s: str, title: str = "") -> str:
    s = s.strip().strip('"“”').replace("**", "")
    s = re.sub(r"^(epitaph:)\s*", "", re.sub(r"\s+", " ", s), flags=re.I)
    return " ".join(sentences(s, title)[:2]).strip()



def quoted_epitaph(mem: dict) -> str:
    title = mem["title"] or "an untitled page"
    line = ""
    for chunk in (mem["description"], mem["text"]):
        for sent in re.split(r"(?<=[.!?])\s", (chunk or "").strip()):
            sent = re.sub(r"^[^A-Za-z]+", "", sent)
            if len(sent.split()) >= 6:  # skip navigation crumbs like "Home | Login"
                line = sent if len(sent) <= 200 else sent[:197].rsplit(" ", 1)[0] + "…"
                break
        if line:
            break
    if line:
        return f"Here lies “{title}”. Its last recorded words: “{line}”"
    return f"Here lies “{title}”. The archive kept its name, and little else."


def write_epitaph(medium: Medium | None, url: str, snap: dict, mem: dict):
    """Returns (epitaph, how, rejected) where how is 'model' or 'quoted' and
    rejected lists the model's drafts that failed verification, with reasons."""
    if len(mem["text"].strip()) < 200 or medium is None:  # too little to summarise honestly
        return quoted_epitaph(mem), "quoted", []
    source = " ".join([mem["title"], mem["description"], mem["text"]])
    facts = " ".join([url, source])
    prompt = PROMPT.format(url=url, title=mem["title"] or "(none)", text=source[:1800])
    rejected = []
    for temp in (0.6, 0.4, 0.2):
        ask = prompt
        if rejected:  # tell the medium exactly why its last draft was turned away
            last, why = rejected[-1]
            ask += (f"\n\nA previous draft was rejected.\nDraft: {last}\nReason: {why}.\n"
                    "Write a new epitaph. In sentence 1, reuse words and phrases that appear "
                    "in the ARCHIVED TEXT.\n\nEpitaph:")
        ep = clean(medium.ask(ask, temp), mem["title"])
        why = verify(ep, facts, mem["title"]) if ep else "empty"
        if why is None:
            return ep, "model", rejected
        rejected.append((ep, why))
    return quoted_epitaph(mem), "quoted", rejected


# ---------------------------------------------------------------- ofrenda ---
def render_markdown(spirits: list[dict], living: list[dict], meta: dict) -> str:
    L = ["# 🕯️ OFRENDA", "",
         f"_A séance held {meta['when']} over `{meta['source']}`. "
         f"{len(spirits)} of {meta['total']} links have crossed over; "
         f"{len(living)} still answer._", "",
         f"_Epitaphs spoken by **{meta['model']}**, running locally. "
         "It was only allowed to speak from what the Internet Archive remembered._", "",
         "---", ""]
    for s in spirits:
        L.append(f"## 🕯️ {s['title'] or s['url']}")
        L.append("")
        L.append(f"- **Address:** <{s['url']}>")
        L.append(f"- **Cause of death:** {s['reason']}")
        if s["snapshot"]:
            L.append(f"- **First seen:** {s['born']}  ·  **Last seen alive:** {s['last_seen']}")
            L.append(f"- **Last snapshot:** [{s['snapshot']}]({s['snapshot']})")
        L.append("")
        L.append(f"> {s['epitaph']}")
        tag = {"model": f"spoken by {meta['model']}",
               "quoted": "quoted from the archive (no model words could be verified)",
               "silent": "no snapshot exists, so nothing was invented"}[s["how"]]
        L.append(">")
        L.append(f"> <sub>{tag}</sub>")
        L.append("")
        if s.get("rejected"):
            L.append("<details><summary>Drafts the guardrail turned away</summary>")
            L.append("")
            L += [f"- ✗ _{r['reason']}_: “{r['draft']}”" for r in s["rejected"]]
            L.append("")
            L.append("</details>")
            L.append("")
    if living:
        L += ["---", "", "### 🌼 Still among the living", ""]
        L += [f"- <{p['url']}>" for p in living]
        L.append("")
    L += ["---", "", "_Made with [Link Séance](https://github.com/cryptocloud786/link-seance). "
          "Snapshots courtesy of the Internet Archive’s Wayback Machine. Please consider "
          "[donating](https://archive.org/donate) to the people who keep the candles lit._", ""]
    return "\n".join(L)


HTML_TMPL = """<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Ofrenda · Link Séance</title>
<style>
:root{--bg:#120d14;--card:#1e1520;--ink:#f3e7d3;--dim:#b9a58f;--marigold:#f2a33a;--rose:#d9577a}
*{box-sizing:border-box}body{margin:0;background:radial-gradient(ellipse at top,#2a1a2c,var(--bg) 60%);
color:var(--ink);font:17px/1.6 Georgia,"Iowan Old Style",serif;min-height:100vh}
header{text-align:center;padding:56px 20px 24px}h1{font-size:2.6rem;margin:0;letter-spacing:.04em}
header p{color:var(--dim);max-width:640px;margin:10px auto 0}
.papel{height:28px;background:repeating-linear-gradient(90deg,var(--marigold) 0 22px,var(--rose) 22px 44px,#7a4fd1 44px 66px);
-webkit-mask:radial-gradient(circle 7px at 11px 20px,transparent 98%,#000) 0 0/22px 28px;mask:radial-gradient(circle 7px at 11px 20px,transparent 98%,#000) 0 0/22px 28px;opacity:.85}
main{max-width:860px;margin:0 auto;padding:24px 20px 60px;display:grid;gap:22px}
.card{background:var(--card);border:1px solid #3a2a3a;border-radius:14px;padding:22px 24px;position:relative;overflow:hidden}
.card:before{content:"";position:absolute;inset:auto -40px -60px auto;width:180px;height:180px;
background:radial-gradient(circle,rgba(242,163,58,.18),transparent 70%)}
.flame{display:inline-block;width:12px;height:18px;background:radial-gradient(ellipse at 50% 70%,#fff3c4,var(--marigold) 45%,transparent 70%);
border-radius:50% 50% 45% 45%;margin-right:10px;animation:f 1.6s infinite alternate ease-in-out;vertical-align:-2px}
@keyframes f{from{transform:scale(1) rotate(-2deg);opacity:.85}to{transform:scale(1.12) rotate(3deg);opacity:1}}
h2{margin:0 0 6px;font-size:1.3rem}.meta{color:var(--dim);font-size:.9rem}.meta a{color:var(--marigold)}
blockquote{margin:14px 0 4px;font-style:italic;font-size:1.08rem;border-left:3px solid var(--marigold);padding-left:14px}
.how{font-size:.78rem;color:var(--dim)}footer{text-align:center;color:var(--dim);font-size:.85rem;padding-bottom:40px}
footer a{color:var(--marigold)}@media (prefers-reduced-motion:reduce){.flame{animation:none}}
</style></head><body>
<div class="papel"></div>
<header><h1>Ofrenda</h1><p>__SUMMARY__</p></header>
<main>__CARDS__</main>
<footer>Made with <a href="https://github.com/cryptocloud786/link-seance">Link Séance</a>.
Snapshots courtesy of the Internet Archive’s Wayback Machine.</footer>
</body></html>
"""


def render_html(spirits: list[dict], meta: dict) -> str:
    e = html.escape
    cards = []
    for s in spirits:
        snap = (f' · last seen alive <b>{e(s["last_seen"])}</b> · '
                f'<a href="{e(s["snapshot"])}">visit the last snapshot</a>') if s["snapshot"] else ""
        how = {"model": f"spoken by {meta['model']}", "quoted": "quoted from the archive",
               "silent": "no snapshot, nothing invented"}[s["how"]]
        cards.append(
            f'<article class="card"><h2><span class="flame"></span>{e(s["title"] or s["url"])}</h2>'
            f'<div class="meta">{e(s["url"])} · {e(s["reason"])}{snap}</div>'
            f'<blockquote>{e(s["epitaph"])}</blockquote><div class="how">{e(how)}</div></article>')
    summary = (f"{len(spirits)} of {meta['total']} links have crossed over. "
               f"Held {meta['when']}; epitaphs by {meta['model']}, running locally.")
    return HTML_TMPL.replace("__SUMMARY__", e(summary)).replace("__CARDS__", "\n".join(cards))


# ------------------------------------------------------------------- main ---
def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="link-seance", description=__doc__.split("\n\n")[0])
    ap.add_argument("input", help="text / markdown / bookmarks.html file with URLs ('-' for stdin)")
    ap.add_argument("-o", "--out", default=".", help="directory for OFRENDA.md and ofrenda.html")
    ap.add_argument("--backend", default="llama.cpp", choices=["llama.cpp", "ollama"])
    ap.add_argument("--model", help="GGUF path (llama.cpp) or model tag (ollama, default gemma3:1b)")
    ap.add_argument("--threads", type=int, help="CPU threads for llama.cpp")
    ap.add_argument("--no-model", action="store_true", help="skip the model; quote the archive instead")
    ap.add_argument("--offline", action="store_true", help="use only the local cache, no network")
    ap.add_argument("--cache", default=CACHE_DIR)
    ap.add_argument("--version", action="version", version=__version__)
    a = ap.parse_args(argv)

    text = sys.stdin.read() if a.input == "-" else open(a.input, encoding="utf-8", errors="replace").read()
    urls = extract_urls(text)
    if not urls:
        say("No URLs found. The room is quiet.")
        return 1
    cache = Cache(a.cache, a.offline)

    say(c("🕯️  Link Séance", "1;33") + c(f"  ·  {len(urls)} links  ·  "
        + ("offline, from cache" if a.offline else "checking pulses"), "2"))
    say()

    spirits, living, pulses = [], [], {}
    for i, url in enumerate(urls, 1):
        p = check_pulse(url, cache)
        pulses[url] = p
        mark = {"alive": c("alive    ", "32"), "dead": c("DEAD     ", "1;31"),
                "wandering": c("WANDERING", "1;35"), "unknown": c("unknown  ", "2")}[p["state"]]
        say(f"  [{i:>2}/{len(urls)}] {mark} {url}  " + c(p["reason"], "2"))
        if p["state"] == "alive":
            living.append({"url": url})
        elif p["state"] in ("dead", "wandering"):
            spirits.append({"url": url, "reason": p["reason"]})

    if not spirits:
        say("\nEveryone is still here. No candles needed tonight. 🌼")
        return 0

    medium = None
    if not a.no_model:
        say(c(f"\n  Waking the medium ({a.backend})…", "2"))
        medium = Medium(a.backend, a.model, a.threads)
        say(c(f"  {medium.name} is listening. Nothing leaves this machine but archive lookups.", "2"))

    say(c(f"\n  Summoning {len(spirits)} spirits from the Wayback Machine…\n", "1"))
    for s in spirits:
        snap, mem = summon(s["url"], pulses[s["url"]], cache)
        if not snap:
            s.update(title="", snapshot="", last_seen="", born="", how="silent", rejected=[],
                     epitaph="The archive never saw this page alive (it may have lived behind "
                             "a login), so I will not invent an epitaph for it.")
            say(f"  {c('∅', '2')} {s['url']}\n      " + c(s["epitaph"], "2") + "\n")
            continue
        ep, how, rejected = write_epitaph(medium, s["url"], snap, mem)
        s["rejected"] = [{"draft": d, "reason": r} for d, r in rejected]
        s.update(title=mem["title"][:120], how=how, epitaph=ep,
                 snapshot=f"https://web.archive.org/web/{snap['timestamp']}/{snap['original']}",
                 last_seen=human_date(snap["timestamp"]), born=human_date(snap["first_seen"]))
        say(f"  {c('🕯️', '33')} {c(s['title'] or s['url'], '1')}  "
            + c(f"(born {s['born']}, last seen {s['last_seen']})", "2"))
        for d, r in rejected:
            say(c(f"      ✗ rejected draft ({r}): “{d}”", "2;31"))
        if how == "model":
            say(f"      “{ep}”\n")
        else:
            say(f"      {ep}" + c("  [quoted from the archive]", "2") + "\n")

    meta = {"when": dt.date.today().strftime("%-d %B %Y"), "source": os.path.basename(a.input),
            "total": len(urls), "model": (medium.name if medium else "no model")}
    os.makedirs(a.out, exist_ok=True)
    with open(os.path.join(a.out, "OFRENDA.md"), "w", encoding="utf-8") as f:
        f.write(render_markdown(spirits, living, meta))
    with open(os.path.join(a.out, "ofrenda.html"), "w", encoding="utf-8") as f:
        f.write(render_html(spirits, meta))
    with open(os.path.join(a.out, "ofrenda.json"), "w", encoding="utf-8") as f:
        json.dump({"meta": meta, "spirits": spirits, "living": living}, f, indent=2, ensure_ascii=False)
    say(c(f"  The ofrenda is set: {os.path.join(a.out, 'OFRENDA.md')} and ofrenda.html", "1;33"))
    return 0


if __name__ == "__main__":
    sys.exit(main())
