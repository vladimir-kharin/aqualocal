"""
Тонкий REST-клиент SingularityApp + кэш проектов/тегов + fuzzy-резолв имён.

Схема подтверждена по openapi.json (Singularity REST API v2.0):
  POST /v2/task  <- TaskCreateDto:
      title*      string
      projectId   string       (не передан => Входящие)
      tags        string[]     (ID тегов вида A-<uuid>)
      start       string
      deadline    string       (отдельное поле, не путать со start)
      useTime     boolean
      priority    number       0=HIGH, 1=NORMAL, 2=LOW
      note        string       (спека врёт про "delta format", см. encode_note)
      externalId  string       (идемпотентность)
      parent      string       (родительская ЗАДАЧА, не проект)
      group       string       (секция проекта, /v2/task-group)
  Ответы списков обёрнуты: {"tasks":[...]} / {"projects":[...]} / {"tags":[...]}
  ID: задача T-..., проект P-..., тег A-<uuid>.
"""

import json
import ssl
import time
import urllib.request
import urllib.parse
import urllib.error
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
CACHE_PATH = ROOT / "singularity" / "cache.json"
ALIASES_PATH = ROOT / "singularity" / "aliases.json"

PRIORITY_HIGH = 0
PRIORITY_NORMAL = 1
PRIORITY_LOW = 2

_CTX = ssl.create_default_context()


# ---------------------------------------------------------------- env
def load_env() -> dict:
    env = {}
    p = ROOT / ".env"
    if p.exists():
        for line in p.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, v = line.split("=", 1)
            env[k.strip()] = v.strip().strip('"').strip("'")
    return env


ENV = load_env()
BASE = ENV.get("SNG_BASE_URL", "https://api.singularity-app.com").rstrip("/")
TOKEN = ENV.get("SNG_TOKEN", "")
TIMEOUT = float(ENV.get("SNG_TIMEOUT", "10"))
CACHE_TTL = float(ENV.get("SNG_CACHE_TTL", "600"))
THRESHOLD = float(ENV.get("SNG_FUZZY_THRESHOLD", "75"))
DEFAULT_PROJECT = ENV.get("SNG_DEFAULT_PROJECT", "").strip()
NOTE_FORMAT = ENV.get("SNG_NOTE_FORMAT", "plain").strip().lower()


class SngError(RuntimeError):
    pass


# ---------------------------------------------------------------- fuzzy
try:
    from rapidfuzz import fuzz as _fuzz

    def ratio(a: str, b: str) -> float:
        # ratio как основа: штрафует лишние слова в запросе, иначе "тег срочно
        # доделать акт" целиком матчится на тег "срочно" со счётом 100.
        # token_set с понижающим весом — страховка от перестановки слов.
        return max(_fuzz.ratio(a, b), 0.85 * _fuzz.token_set_ratio(a, b))
except ImportError:  # запасной вариант на stdlib, качество ниже
    from difflib import SequenceMatcher

    def ratio(a: str, b: str) -> float:
        return SequenceMatcher(None, a, b).ratio() * 100


def normalize(s: str) -> str:
    """Убираем всё, что whisper любит добавлять: пунктуацию, регистр, ё."""
    s = (s or "").lower().replace("ё", "е")
    return "".join(ch for ch in s if ch.isalnum() or ch.isspace()).strip()


def load_aliases() -> dict:
    """Ручные соответствия «как я это произношу» -> ID.

    Нужны там, где fuzzy бессилен: латиница против кириллицы
    ("митинг ассистент" никогда не сматчится на "Meeting Assistant"),
    а также произнесённые по буквам аббревиатуры ("один эс" -> тег "1С").
    Формат singularity/aliases.json:
        {"митинг ассистент": "P-...", "один эс": "A-..."}
    """
    if not ALIASES_PATH.exists():
        return {}
    try:
        raw = json.loads(ALIASES_PATH.read_text(encoding="utf-8"))
        return {normalize(k): v for k, v in raw.items() if not k.startswith("_")}
    except Exception:
        return {}


def best_match(query: str, items, threshold: float = None, aliases: dict = None):
    """items: [{'id':..,'title':..}]. Возвращает (item|None, score)."""
    threshold = THRESHOLD if threshold is None else threshold
    q = normalize(query)
    if not q or not items:
        return None, 0.0

    if aliases:
        target = aliases.get(q)
        if target:
            for it in items:
                if it.get("id") == target:
                    return it, 100.0

    best, best_score = None, 0.0
    for it in items:
        sc = ratio(q, normalize(it.get("title", "")))
        if sc > best_score:
            best, best_score = it, sc
    return (best, best_score) if best_score >= threshold else (None, best_score)


# ---------------------------------------------------------------- note
def encode_note(text: str) -> str:
    """Поле note.

    В openapi.json оно описано как "Task note in delta format" (Quill Delta),
    но проверено на живом API 23.07.2026: сервер хранит строку как есть, а
    приложение рисует её буквально. Отправленный Quill Delta вылезает в
    описание задачи как {"ops":[{"insert":"..."}]}. Поэтому правильное
    значение — plain; delta оставлен только на случай, если поведение
    когда-нибудь приведут в соответствие со спекой.
    """
    if not text:
        return ""
    if NOTE_FORMAT == "delta":
        return json.dumps({"ops": [{"insert": text.rstrip() + "\n"}]}, ensure_ascii=False)
    return text


# ---------------------------------------------------------------- client
class SngClient:
    def __init__(self, token: str = None, base: str = None):
        self.token = token or TOKEN
        self.base = (base or BASE).rstrip("/")
        if not self.token:
            raise SngError("SNG_TOKEN не задан в .env")
        self._cache = {"ts": 0.0, "projects": [], "tags": []}
        self._aliases = load_aliases()
        self._load_cache()

    # ---------- транспорт
    def _request(self, method: str, path: str, body=None, query: dict = None):
        url = self.base + path
        if query:
            clean = {k: v for k, v in query.items() if v is not None}
            if clean:
                url += "?" + urllib.parse.urlencode(clean)
        data = json.dumps(body, ensure_ascii=False).encode("utf-8") if body is not None else None
        req = urllib.request.Request(url, data=data, method=method)
        req.add_header("Authorization", f"Bearer {self.token}")
        req.add_header("Accept", "application/json")
        if data:
            req.add_header("Content-Type", "application/json")
        try:
            with urllib.request.urlopen(req, timeout=TIMEOUT, context=_CTX) as r:
                raw = r.read().decode("utf-8", "replace")
                return json.loads(raw) if raw.strip() else {}
        except urllib.error.HTTPError as e:
            detail = e.read().decode("utf-8", "replace")[:400]
            raise SngError(f"{method} {path} -> HTTP {e.code}: {detail}") from None
        except urllib.error.URLError as e:
            raise SngError(f"{method} {path} -> сеть недоступна: {e.reason}") from None

    # ---------- кэш НСИ
    def _load_cache(self):
        try:
            if CACHE_PATH.exists():
                self._cache = json.loads(CACHE_PATH.read_text(encoding="utf-8"))
        except Exception:
            pass

    def _save_cache(self):
        try:
            CACHE_PATH.write_text(json.dumps(self._cache, ensure_ascii=False, indent=1),
                                  encoding="utf-8")
        except Exception:
            pass

    def refresh(self, force: bool = False):
        """Тянет проекты и теги. Не чаще раза в SNG_CACHE_TTL секунд."""
        if not force and (time.time() - self._cache.get("ts", 0)) < CACHE_TTL:
            return self._cache
        projects = self._request("GET", "/v2/project", query={"maxCount": 500}).get("projects", [])
        tags = self._request("GET", "/v2/tag", query={"maxCount": 500}).get("tags", [])
        self._cache = {
            "ts": time.time(),
            "projects": [{"id": p.get("id"), "title": p.get("title", "")}
                         for p in projects if not p.get("removed")],
            "tags": [{"id": t.get("id"), "title": t.get("title", "")}
                     for t in tags if not t.get("removed")],
        }
        self._save_cache()
        self._aliases = load_aliases()
        return self._cache

    @property
    def projects(self):
        return self._cache.get("projects", [])

    @property
    def tags(self):
        return self._cache.get("tags", [])

    def resolve_project(self, name: str):
        return best_match(name, self.projects, aliases=self._aliases)

    def resolve_tag(self, name: str):
        return best_match(name, self.tags, aliases=self._aliases)

    # ---------- задачи
    def create_task(self, title, project_id=None, tag_ids=None, start=None,
                    deadline=None, use_time=False, priority=PRIORITY_NORMAL,
                    note=None, external_id=None):
        payload = {"title": title.strip()}
        if project_id:
            payload["projectId"] = project_id
        if tag_ids:
            payload["tags"] = list(tag_ids)
        if start:
            payload["start"] = start
        if deadline:
            payload["deadline"] = deadline
        if use_time:
            payload["useTime"] = True
        if priority is not None and priority != PRIORITY_NORMAL:
            payload["priority"] = priority
        if note:
            payload["note"] = encode_note(note)
        if external_id:
            payload["externalId"] = external_id
        return self._request("POST", "/v2/task", payload)

    def get_task(self, task_id: str):
        return self._request("GET", f"/v2/task/{task_id}")

    def delete_task(self, task_id: str):
        return self._request("DELETE", f"/v2/task/{task_id}")
