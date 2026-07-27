"""
Отправка распознанной фразы в SingularityApp.

Точка входа для app.py:
    from singularity.sender import send_task
    summary = send_task("проект ВЛП срок пятница подготовить смету")

Бросает исключение при сбое — app.py ловит и кладёт фразу в offline-очередь.

CLI (проверка без микрофона):
    .venv\\Scripts\\python.exe -m singularity.sender "проект ВЛП купить хлеб"
    .venv\\Scripts\\python.exe -m singularity.sender --list
    .venv\\Scripts\\python.exe -m singularity.sender --flush
    .venv\\Scripts\\python.exe -m singularity.sender --dry "срок завтра позвонить"
"""

import json
import logging
import sys
import uuid
from pathlib import Path

from .client import SngClient, SngError, DEFAULT_PROJECT
from .parser import parse

ROOT = Path(__file__).resolve().parent.parent
QUEUE_PATH = ROOT / "singularity" / "queue.jsonl"

_client = None


def get_client() -> SngClient:
    """Ленивый синглтон. Кэш НСИ обновляется по TTL из .env."""
    global _client
    if _client is None:
        _client = SngClient()
    try:
        _client.refresh()
    except SngError as e:
        # Кэш с прошлого раза лучше, чем ничего: резолв проектов продолжит работать
        logging.warning(f"Не удалось обновить кэш проектов/тегов: {e}")
    return _client


def build_task(text: str, client=None):
    """Фраза -> ParsedTask. Отдельно от отправки, чтобы можно было прогнать всухую."""
    parsed = parse(text, client)
    if not parsed.project_id and DEFAULT_PROJECT:
        parsed.project_id = DEFAULT_PROJECT
    return parsed


def send_task(text: str) -> str:
    """Разбирает фразу и создаёт задачу. Возвращает строку для плашки."""
    client = get_client()
    p = build_task(text, client)

    client.create_task(
        title=p.title,
        project_id=p.project_id,
        tag_ids=p.tag_ids,
        start=p.start,
        deadline=p.deadline,
        priority=p.priority,
        note=p.note,
        external_id=f"aqualocal-{uuid.uuid4()}",
    )

    try:
        flush_queue(client)
    except Exception:
        pass

    return p.summary()


# ---------------------------------------------------------------- offline-очередь
def flush_queue(client=None) -> int:
    """Дошлёт то, что накопилось, пока не было сети. -> сколько отправлено."""
    if not QUEUE_PATH.exists():
        return 0
    client = client or get_client()

    records = []
    for line in QUEUE_PATH.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line:
            try:
                records.append(json.loads(line))
            except Exception:
                pass

    pending = [r for r in records if not r.get("sent")]
    if not pending:
        return 0

    sent = 0
    for rec in pending:
        try:
            p = build_task(rec.get("text", ""), client)
            client.create_task(
                title=p.title, project_id=p.project_id, tag_ids=p.tag_ids,
                start=p.start, deadline=p.deadline, priority=p.priority, note=p.note,
                external_id=rec.get("external_id") or f"aqualocal-{uuid.uuid4()}",
            )
            rec["sent"] = True
            sent += 1
        except SngError as e:
            rec["reason"] = str(e)
            break  # сеть всё ещё лежит — остальные не трогаем

    remaining = [r for r in records if not r.get("sent")]
    if remaining:
        QUEUE_PATH.write_text(
            "\n".join(json.dumps(r, ensure_ascii=False) for r in remaining) + "\n",
            encoding="utf-8")
    else:
        QUEUE_PATH.unlink(missing_ok=True)

    if sent:
        logging.info(f"Из offline-очереди отправлено задач: {sent}")
    return sent


# ---------------------------------------------------------------- CLI
def _cli():
    args = sys.argv[1:]
    logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")

    if not args or args[0] in ("-h", "--help"):
        print(__doc__)
        return

    if args[0] == "--list":
        c = get_client()
        c.refresh(force=True)
        print(f"\nПРОЕКТЫ ({len(c.projects)}):")
        for p in c.projects:
            print(f"  {p['id']:<42} {p['title']}")
        print(f"\nТЕГИ ({len(c.tags)}):")
        for t in c.tags:
            print(f"  {t['id']:<42} {t['title']}")
        return

    if args[0] == "--flush":
        print(f"Отправлено из очереди: {flush_queue()}")
        return

    dry = args[0] == "--dry"
    text = " ".join(args[1:] if dry else args)

    client = None
    try:
        client = get_client()
    except SngError as e:
        print(f"Клиент недоступен ({e}) — разбор без резолва имён.")

    p = build_task(text, client)
    print(f"\nфраза     : {text}")
    print(f"title     : {p.title}")
    print(f"project   : {p.project_title or 'Входящие'}  ({p.project_id})")
    print(f"tags      : {p.tag_titles}  ({p.tag_ids})")
    print(f"start     : {p.start}")
    print(f"deadline  : {p.deadline}")
    print(f"priority  : {p.priority}  (0=высокий, 1=обычный, 2=низкий)")
    print(f"note      : {p.note!r}")
    print(f"warnings  : {p.warnings}")
    print(f"summary   : {p.summary()}")

    if dry:
        print("\n--dry: задача НЕ создана")
        return

    print(f"\nОтправлено: {send_task(text)}")


if __name__ == "__main__":
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass
    _cli()
