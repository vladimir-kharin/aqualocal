"""
Выгрузка OpenAPI-спеки SingularityApp + разбор непонятных мест.

Что делает:
  1. Ищет спеку по всем известным путям (включая NestJS-специфичный
     swagger-ui-init.js, где спека лежит инлайном в JS).
  2. Кладёт её в singularity/openapi.json.
  3. Печатает то, чего НЕТ в публичной вики:
       - схему тела POST /v2/task (есть ли projectId?)
       - все поля сущности Task (есть ли tags?)
       - все эндпоинты со словом tag
  4. По флагу --probe делает живую проверку: создаёт тестовую задачу
     с projectId и тегом, читает обратно, смотрит что прилипло, удаляет.

Запуск:
    .venv\\Scripts\\python.exe singularity\\fetch_openapi.py
    .venv\\Scripts\\python.exe singularity\\fetch_openapi.py --probe
"""

import json
import re
import sys
import ssl
import urllib.request
import urllib.error
from pathlib import Path

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

ROOT = Path(__file__).resolve().parent.parent
OUT_JSON = ROOT / "singularity" / "openapi.json"
OUT_RAW = ROOT / "singularity" / "openapi_raw.txt"


# ---------------------------------------------------------------- env
def load_env() -> dict:
    env = {}
    p = ROOT / ".env"
    if not p.exists():
        sys.exit(f"Не найден {p}")
    for line in p.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        env[k.strip()] = v.strip().strip('"').strip("'")
    if not env.get("SNG_TOKEN"):
        sys.exit("В .env пуст SNG_TOKEN")
    return env


ENV = load_env()
BASE = ENV.get("SNG_BASE_URL", "https://api.singularity-app.com").rstrip("/")
TOKEN = ENV["SNG_TOKEN"]
CTX = ssl.create_default_context()


# ---------------------------------------------------------------- http
def http(method: str, path: str, body=None, auth=True, timeout=20):
    """Возвращает (status, text). Не бросает на 4xx/5xx."""
    url = path if path.startswith("http") else BASE + path
    data = json.dumps(body).encode("utf-8") if body is not None else None
    req = urllib.request.Request(url, data=data, method=method)
    req.add_header("Accept", "application/json, text/javascript, */*")
    req.add_header("User-Agent", "aqualocal/1.0")
    if data:
        req.add_header("Content-Type", "application/json")
    if auth:
        req.add_header("Authorization", f"Bearer {TOKEN}")
    try:
        with urllib.request.urlopen(req, timeout=timeout, context=CTX) as r:
            return r.status, r.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode("utf-8", "replace")
    except Exception as e:
        return 0, f"{type(e).__name__}: {e}"


# ---------------------------------------------------------------- поиск спеки
CANDIDATES = [
    "/v2/api-json",
    "/v2/api/swagger-ui-init.js",   # NestJS: спека инлайном в JS
    "/v2/api/swagger.json",
    "/v2/api/json",
    "/v2/api-docs",
    "/v2/swagger.json",
    "/v2/openapi.json",
    "/v2/api-yaml",
]


def extract_spec(text: str):
    """Достаёт JSON-спеку из ответа: чистый JSON или swaggerDoc внутри JS."""
    t = text.lstrip()
    if t.startswith("{"):
        try:
            obj = json.loads(t)
            if "paths" in obj:
                return obj
        except Exception:
            pass
    m = re.search(r'"swaggerDoc"\s*:\s*(\{)', text)
    if m:
        start = m.start(1)
        depth, in_str, esc = 0, False, False
        for i in range(start, len(text)):
            c = text[i]
            if in_str:
                if esc:
                    esc = False
                elif c == "\\":
                    esc = True
                elif c == '"':
                    in_str = False
            else:
                if c == '"':
                    in_str = True
                elif c == "{":
                    depth += 1
                elif c == "}":
                    depth -= 1
                    if depth == 0:
                        try:
                            return json.loads(text[start:i + 1])
                        except Exception:
                            return None
    return None


def find_spec():
    # подсказка из HTML самой Swagger UI
    st, html = http("GET", "/v2/api")
    hints = []
    if st == 200:
        hints = [h for h in re.findall(r'["\'](/[^"\']*?(?:json|yaml|init\.js)[^"\']*)["\']', html)]

    for path in list(dict.fromkeys(hints + CANDIDATES)):
        for auth in (True, False):
            st, txt = http("GET", path, auth=auth)
            tag = f"{path} auth={'yes' if auth else 'no'}"
            if st != 200:
                print(f"  [{st:>3}] {tag}")
                continue
            spec = extract_spec(txt)
            if spec:
                print(f"  [200] {tag}  <-- СПЕКА НАЙДЕНА")
                return spec, path
            print(f"  [200] {tag}  (не спека, {len(txt)} байт)")
            OUT_RAW.write_text(txt[:200000], encoding="utf-8")
    return None, None


# ---------------------------------------------------------------- разбор
def deref(spec, node, depth=0):
    if depth > 6 or not isinstance(node, dict):
        return node
    if "$ref" in node:
        parts = node["$ref"].lstrip("#/").split("/")
        cur = spec
        for p in parts:
            cur = cur.get(p, {}) if isinstance(cur, dict) else {}
        return deref(spec, cur, depth + 1)
    return node


def props_of(spec, schema):
    schema = deref(spec, schema)
    out = {}
    for key in ("allOf", "oneOf", "anyOf"):
        for sub in schema.get(key, []) or []:
            out.update(props_of(spec, sub))
    for name, val in (schema.get("properties") or {}).items():
        v = deref(spec, val)
        t = v.get("type", "?")
        if t == "array":
            t = f"array<{deref(spec, v.get('items', {})).get('type', '?')}>"
        note = ""
        if v.get("enum"):
            note = f" enum={v['enum']}"
        if v.get("description"):
            note += f"  // {v['description'][:80]}"
        out[name] = f"{t}{note}"
    return out


def report(spec):
    info = spec.get("info", {})
    print(f"\n{'=' * 70}")
    print(f"{info.get('title', '?')}  v{info.get('version', '?')}")
    print(f"{'=' * 70}")

    paths = spec.get("paths", {})

    print("\n--- ВСЕ ЭНДПОИНТЫ " + "-" * 51)
    for p in sorted(paths):
        methods = ",".join(m.upper() for m in paths[p] if m.lower() in
                           ("get", "post", "patch", "put", "delete"))
        print(f"  {methods:<22} {p}")

    # 1. тело POST /v2/task
    print("\n--- ВОПРОС 1: тело POST /v2/task (есть ли projectId?) " + "-" * 16)
    op = (paths.get("/v2/task") or paths.get("/task") or {}).get("post")
    if not op:
        print("  POST /v2/task не найден в спеке (?)")
    else:
        body = op.get("requestBody", {}).get("content", {})
        sch = (body.get("application/json") or next(iter(body.values()), {})).get("schema", {})
        pr = props_of(spec, sch)
        req = set(deref(spec, sch).get("required") or [])
        if not pr:
            print("  Схема тела пустая — смотри openapi.json вручную")
        for k in sorted(pr):
            mark = "*" if k in req else " "
            print(f"  {mark} {k:<24} {pr[k]}")
        hit = [k for k in pr if "project" in k.lower() or k.lower() == "parent"]
        print(f"\n  >>> ПОЛЯ ПРОЕКТА: {hit or 'НЕТ — проект через POST не назначить'}")
        hit = [k for k in pr if "tag" in k.lower()]
        print(f"  >>> ПОЛЯ ТЕГОВ:   {hit or 'НЕТ — тег через POST не назначить'}")

    # 2. схема сущности Task в ответе
    print("\n--- ВОПРОС 2: поля Task в ответе GET /v2/task " + "-" * 24)
    op = (paths.get("/v2/task") or paths.get("/task") or {}).get("get")
    if op:
        resp = op.get("responses", {}).get("200", {}).get("content", {})
        sch = (resp.get("application/json") or next(iter(resp.values()), {})).get("schema", {})
        sch = deref(spec, sch)
        if sch.get("type") == "array":
            sch = deref(spec, sch.get("items", {}))
        pr = props_of(spec, sch)
        for k in sorted(pr):
            print(f"    {k:<24} {pr[k]}")

    # 3. всё, что связано с тегами
    print("\n--- ВОПРОС 3: эндпоинты и схемы со словом tag " + "-" * 24)
    for p in sorted(paths):
        if "tag" in p.lower():
            print(f"  {p}")
    for name in sorted(spec.get("components", {}).get("schemas", {})):
        if "tag" in name.lower():
            print(f"  schema: {name}")

    print(f"\nПолная спека: {OUT_JSON}")


# ---------------------------------------------------------------- живая проба
def probe():
    print("\n" + "=" * 70)
    print("ЖИВАЯ ПРОБА (создаст и удалит тестовую задачу в твоём аккаунте)")
    print("=" * 70)

    st, txt = http("GET", "/v2/project?maxCount=5")
    print(f"\nGET /v2/project -> {st}")
    projects = []
    if st == 200:
        try:
            data = json.loads(txt)
            projects = data if isinstance(data, list) else data.get("items", data.get("data", []))
        except Exception:
            print(txt[:500])
    for p in projects[:5]:
        print(f"   {p.get('id')}  {p.get('title')}")

    st, txt = http("GET", "/v2/tag?maxCount=5")
    print(f"\nGET /v2/tag -> {st}")
    tags = []
    if st == 200:
        try:
            data = json.loads(txt)
            tags = data if isinstance(data, list) else data.get("items", data.get("data", []))
        except Exception:
            print(txt[:500])
    for t in tags[:5]:
        print(f"   {t.get('id')}  {t.get('title')}")

    pid = projects[0].get("id") if projects else None
    tid = tags[0].get("id") if tags else None

    payload = {"title": "aqualocal probe (удали меня)", "note": "тест API", "priority": 1}
    if pid:
        payload["projectId"] = pid
    if tid:
        payload["tags"] = [tid]

    print(f"\nPOST /v2/task  {json.dumps(payload, ensure_ascii=False)}")
    st, txt = http("POST", "/v2/task", payload)
    print(f"  -> {st}")
    print(f"  {txt[:1200]}")

    if st not in (200, 201):
        print("\n  Создание не прошло. Пробую без projectId/tags...")
        st, txt = http("POST", "/v2/task", {"title": "aqualocal probe (удали меня)"})
        print(f"  -> {st}  {txt[:600]}")

    try:
        created = json.loads(txt)
        tid_new = created.get("id") or created.get("data", {}).get("id")
    except Exception:
        tid_new = None

    if tid_new:
        st, txt = http("GET", f"/v2/task/{tid_new}")
        print(f"\nGET /v2/task/{tid_new} -> {st}")
        print(f"  {txt[:1200]}")
        print("\n  ^^^ смотри: projectId и tags доехали или молча отвалились")

        st, _ = http("DELETE", f"/v2/task/{tid_new}")
        print(f"\nDELETE /v2/task/{tid_new} -> {st}")


# ---------------------------------------------------------------- main
if __name__ == "__main__":
    print(f"База: {BASE}")
    print("Ищу спеку...")
    spec, path = find_spec()
    if spec:
        OUT_JSON.write_text(json.dumps(spec, ensure_ascii=False, indent=2), encoding="utf-8")
        report(spec)
    else:
        print("\nСпеку по известным путям достать не удалось.")
        print("Тогда: открой https://api.singularity-app.com/v2/api в браузере,")
        print("F12 -> Network -> обнови страницу -> найди запрос со спекой")
        print("-> ПКМ -> Copy response -> сохрани в singularity/openapi.json")
        if OUT_RAW.exists():
            print(f"(частичный сырой ответ сохранён в {OUT_RAW})")

    if "--probe" in sys.argv:
        probe()
    else:
        print("\nДля живой проверки полей: добавь флаг --probe")
