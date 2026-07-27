"""
Запись распознанной фразы в Obsidian (третий приёмник, хоткей F21).

Точка входа для app.py:
    from obsidian.writer import append_note
    summary = append_note("тег дом купить мастику")

Логика:
  * папка берётся из .env -> OBS_INBOX_DIR;
  * файл дня <ГГГГ-ММ-ДД>.md — если нет, создаётся с заголовком, если есть,
    строка дописывается в конец;
  * строка: "- ЧЧ:ММ<TAB>Текст #тег1 #тег2".

Теги: маркер «тег/тэг/с тегом» в любом месте фразы. Имя тега сверяется с
кэшем тегов SingularityApp (singularity/cache.json, без обращения к сети) —
чтобы «курс» и «Курс» не расползались в два разных тега. Не нашли в кэше —
пишем как произнесено.

CLI:
    .venv\\Scripts\\python.exe -m obsidian.writer "тег дом купить мастику"
    .venv\\Scripts\\python.exe -m obsidian.writer --dry "тег 1с посмотреть вебинар"
    .venv\\Scripts\\python.exe -m obsidian.writer --flush
"""

import json
import logging
import re
import sys
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
QUEUE_PATH = ROOT / "obsidian" / "queue.jsonl"

# Защита от рекурсии: append_note в конце разбирает очередь, а очередь
# разбирается тем же append_note. Без флага первая же запись в очереди
# уводит в бесконечный цикл.
_flushing = False


# ---------------------------------------------------------------- env
def load_env() -> dict:
    """Свой загрузчик, а не импорт из singularity.client: пакеты независимы,
    Obsidian-режим обязан работать даже при развалившейся интеграции с API."""
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

# Fallback на случай, если строку в .env забыли добавить — приложение не должно
# молча терять заметки. В лог при этом уйдёт предупреждение (см. _inbox_dir).
DEFAULT_INBOX = r"E:\vaults\personnel\personnel\Входящие"

def _unescape(s: str) -> str:
    """В .env табуляцию руками не поставить — пришли два символа '\\t'."""
    return s.replace("\\t", "\t").replace("\\n", "\n")


INBOX_DIR = Path(ENV.get("OBS_INBOX_DIR", "").strip() or DEFAULT_INBOX)
DATE_FMT = ENV.get("OBS_DATE_FMT", "%Y-%m-%d")
TIME_FMT = ENV.get("OBS_TIME_FMT", "%H:%M")
LINE_FMT = _unescape(ENV.get("OBS_LINE_FMT", "- {time}\t{text}"))
HEADING = ENV.get("OBS_HEADING", "# {date}")
RESOLVE_TAGS = ENV.get("OBS_RESOLVE_TAGS", "1").strip() not in ("0", "false", "no", "")

if not ENV.get("OBS_INBOX_DIR", "").strip():
    logging.warning(f"OBS_INBOX_DIR не задан в .env — использую {DEFAULT_INBOX}")


class ObsidianError(RuntimeError):
    pass


# ---------------------------------------------------------------- разбор тегов
# Маркеры берём из singularity.parser, чтобы «тег» означал одно и то же в обоих
# режимах. Если пакет недоступен — минимальный локальный набор.
try:
    from singularity.parser import M_TAG, SKIP, _norm, _take_entity
except Exception:  # pragma: no cover
    M_TAG = {"тег", "тэг", "теги", "тэги", "тегом", "тэгом", "тегами"}
    SKIP = {"в", "с", "со", "во"}
    _take_entity = None

    def _norm(w: str) -> str:
        w = w.lower().replace("ё", "е")
        return re.sub(r"[^\w]", "", w, flags=re.UNICODE)


def _tag_resolver():
    """Резолвер имени тега по локальному кэшу. Сети не касается.

    Кэш пишет singularity.client при обновлении НСИ. Если его нет, клиент не
    импортируется или токен не задан — возвращаем None, теги пойдут как есть.
    """
    if not RESOLVE_TAGS:
        return None
    try:
        from singularity.client import SngClient
        client = SngClient()
        return client.resolve_tag if client.tags else None
    except Exception as e:
        logging.debug(f"Резолв тегов недоступен ({e}) — пишу как произнесено.")
        return None


def slug(tag: str) -> str:
    """Имя тега -> вид, пригодный для Obsidian: без пробелов и пунктуации."""
    t = (tag or "").strip().strip("#")
    t = re.sub(r"\s+", "-", t)
    t = re.sub(r"[^\w\-/]", "", t, flags=re.UNICODE)
    # Чисто цифровой тег Obsidian не подсветит — обесцениваем его в текст.
    return "" if not t or t.isdigit() else t


def parse_note(text: str, resolver=None):
    """Фраза -> (текст без маркеров, [теги]). Маркер ищется в любом месте."""
    words = text.strip().split()
    body, tags = [], []
    i = 0
    while i < len(words):
        w = _norm(words[i])

        # служебное слово перед маркером: «с тегом», «в теге»
        if w in SKIP and i + 1 < len(words) and _norm(words[i + 1]) in M_TAG:
            i += 1
            continue

        if w in M_TAG and i + 1 < len(words):
            if resolver is not None and _take_entity is not None:
                item, used, _score, _phrase = _take_entity(words, i + 1, resolver, max_span=3)
                if item:
                    tags.append(item["title"])
                    i += 1 + used
                    continue
            # без резолвера (или не совпало) берём ровно одно слово — иначе
            # рискуем утащить в тег половину заметки
            tags.append(words[i + 1].strip(" ,.;:«»\"'"))
            i += 2
            continue

        body.append(words[i])
        i += 1

    out = " ".join(body).strip(" ,.;:-—")
    if out:
        out = out[0].upper() + out[1:]
    tags = [s for s in (slug(t) for t in tags) if s]
    return out, tags


# ---------------------------------------------------------------- запись
def _inbox_dir() -> Path:
    if not INBOX_DIR.exists():
        try:
            INBOX_DIR.mkdir(parents=True, exist_ok=True)
        except Exception as e:
            raise ObsidianError(f"папка {INBOX_DIR} недоступна: {e}") from None
    return INBOX_DIR


def day_file(when: datetime = None) -> Path:
    when = when or datetime.now()
    return _inbox_dir() / (when.strftime(DATE_FMT) + ".md")


def build_line(text, tags, when: datetime = None) -> str:
    when = when or datetime.now()
    tail = (" " + " ".join("#" + t for t in tags)) if tags else ""
    return LINE_FMT.format(time=when.strftime(TIME_FMT), text=text) + tail


def append_note(text: str, when: datetime = None, extra_tags=None) -> str:
    """Разбирает фразу и дописывает строку в файл дня. -> строка для плашки.

    extra_tags — теги, которые навешиваются помимо распознанных во фразе
    (F20 добавляет сюда «задача», чтобы отличать дубль задачи от обычной заметки).

    Бросает ObsidianError — app.py ловит и кладёт фразу в offline-очередь.
    """
    when = when or datetime.now()
    body, tags = parse_note(text, _tag_resolver())
    if not body:
        body = text.strip() or "(пусто)"

    # теги-маркеры спереди, без дубликатов, с сохранением порядка
    if extra_tags:
        seen = set()
        merged = []
        for t in [slug(x) for x in extra_tags] + tags:
            if t and t not in seen:
                seen.add(t)
                merged.append(t)
        tags = merged

    path = day_file(when)
    line = build_line(body, tags, when)

    try:
        created = not path.exists()
        if created:
            head = HEADING.format(date=when.strftime(DATE_FMT)) if HEADING else ""
            with open(path, "w", encoding="utf-8", newline="\n") as f:
                if head:
                    f.write(head + "\n\n")
        else:
            # файл мог остаться без перевода строки в конце — тогда наша запись
            # приклеится к чужой
            with open(path, "rb") as f:
                if path.stat().st_size:
                    f.seek(-1, 2)
                    if f.read(1) not in (b"\n", b"\r"):
                        with open(path, "a", encoding="utf-8", newline="\n") as a:
                            a.write("\n")

        with open(path, "a", encoding="utf-8", newline="\n") as f:
            f.write(line + "\n")
    except OSError as e:
        raise ObsidianError(f"не удалось записать {path.name}: {e}") from None

    if not _flushing:
        try:
            flush_queue()
        except Exception:
            pass

    tagstr = (" " + " ".join("#" + t for t in tags)) if tags else ""
    return f"{path.name}{tagstr}: {body}"


# ---------------------------------------------------------------- offline-очередь
def enqueue(text: str, reason: str = "", extra_tags=None):
    """Папка недоступна (внешний диск, сеть) — фраза не теряется."""
    QUEUE_PATH.parent.mkdir(parents=True, exist_ok=True)
    rec = {
        "ts": datetime.now().isoformat(timespec="seconds"),
        "text": text,
        "sent": False,
        "reason": reason,
    }
    if extra_tags:
        rec["extra_tags"] = list(extra_tags)
    with open(QUEUE_PATH, "a", encoding="utf-8") as f:
        f.write(json.dumps(rec, ensure_ascii=False) + "\n")
    logging.info(f"Заметка записана в offline-очередь: {text}")


def flush_queue() -> int:
    """Дописывает накопленное. Время берётся из записи, а не текущее, поэтому
    заметка попадёт в файл того дня, когда была продиктована. -> сколько ушло."""
    global _flushing
    if _flushing or not QUEUE_PATH.exists():
        return 0

    records = []
    for line in QUEUE_PATH.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line:
            try:
                records.append(json.loads(line))
            except Exception:
                pass

    if not [r for r in records if not r.get("sent")]:
        return 0

    sent = 0
    _flushing = True
    try:
        for rec in records:
            if rec.get("sent"):
                continue
            try:
                when = datetime.fromisoformat(rec.get("ts")) if rec.get("ts") else None
            except Exception:
                when = None
            try:
                append_note(rec.get("text", ""), when=when,
                            extra_tags=rec.get("extra_tags"))
                rec["sent"] = True
                sent += 1
            except ObsidianError as e:
                rec["reason"] = str(e)
                break  # папка всё ещё недоступна
    finally:
        _flushing = False

    remaining = [r for r in records if not r.get("sent")]
    if remaining:
        QUEUE_PATH.write_text(
            "\n".join(json.dumps(r, ensure_ascii=False) for r in remaining) + "\n",
            encoding="utf-8")
    else:
        QUEUE_PATH.unlink(missing_ok=True)

    if sent:
        logging.info(f"Из offline-очереди записано заметок: {sent}")
    return sent


# ---------------------------------------------------------------- CLI
def _cli():
    args = sys.argv[1:]
    logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")

    if not args or args[0] in ("-h", "--help"):
        print(__doc__)
        print(f"OBS_INBOX_DIR = {INBOX_DIR}  (существует: {INBOX_DIR.exists()})")
        return

    if args[0] == "--flush":
        print(f"Записано из очереди: {flush_queue()}")
        return

    dry = args[0] == "--dry"
    text = " ".join(args[1:] if dry else args)

    body, tags = parse_note(text, _tag_resolver())
    when = datetime.now()
    print(f"\nфраза : {text}")
    print(f"файл  : {INBOX_DIR / (when.strftime(DATE_FMT) + '.md')}")
    print(f"текст : {body}")
    print(f"теги  : {tags}")
    print(f"строка: {build_line(body, tags, when)!r}")

    if dry:
        print("\n--dry: ничего не записано")
        return

    print(f"\nЗаписано: {append_note(text)}")


if __name__ == "__main__":
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass
    _cli()
