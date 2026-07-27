"""
Разбор произнесённой фразы в структуру задачи SingularityApp.

Грамматика — ведущие маркеры, съедаются с начала фразы в любом порядке.
Остаток = заголовок задачи.

    проект <имя>          / в проект <имя>   -> projectId (fuzzy по кэшу)
    тег <имя>             / с тегом <имя>    -> tags[] (можно несколько раз)
    на <дата>             / дата <дата>      -> start
    срок <дата>           / до <дата>        -> deadline
    дедлайн <дата>                           -> deadline
    важно | срочно                           -> priority = 0 (высокий)
    неважно | потом                          -> priority = 2 (низкий)
    описание <текст>      / заметка <текст>  -> note (ищется в любом месте фразы,
                                                забирает весь хвост)

Даты: сегодня, завтра, послезавтра, <день недели>, через N дней/недель/месяцев,
      "15 августа", "15 августа 2026", 24.12, 24.12.2026.
      Прошедшая дата без года переносится на следующий год.

Примеры (проверено тестами):
    "купить хлеб"
        -> Входящие: Купить хлеб
    "проект ВЛП интеграция срок пятница подготовить смету по ЛДК"
        -> ВЛП интеграция, deadline=ближайшая пятница, title="Подготовить смету по ЛДК"
    "в проект а семь пиломатериалы тег срочно доделать акт сортировки"
        -> А7 пиломатериалы, #срочно, title="Доделать акт сортировки"
    "неважно потом разобрать почту описание накопилось за неделю"
        -> priority=2, title="Разобрать почту", note="накопилось за неделю"

Самостоятельная проверка разбора (без обращения к API):
    .venv\\Scripts\\python.exe -m singularity.parser "проект дом срок завтра заказать окна"
"""

import re
from dataclasses import dataclass, field
from datetime import date, datetime, time as dtime, timedelta


def _local_tz():
    """Локальная таймзона машины (для Автора — +03:00). API требует явный оффсет."""
    return datetime.now().astimezone().tzinfo


def to_api_datetime(d, end_of_day: bool) -> str:
    """date -> ISO-8601 с временем и таймзоной, как требует SingularityApp.

    API отвечает HTTP 400 на голую дату '2026-07-27' — нужно
    '2026-07-27T23:59:00+03:00'. Дедлайн — конец дня, старт — начало.
    На вход может прийти уже готовая ISO-строка (совместимость) — вернём как есть.
    """
    if d is None:
        return None
    if isinstance(d, str):
        # уже с временем/таймзоной — не трогаем; голая дата — достраиваем
        if "T" in d:
            return d
        try:
            d = date.fromisoformat(d)
        except ValueError:
            return d
    t = dtime(23, 59) if end_of_day else dtime(9, 0)
    return datetime.combine(d, t, tzinfo=_local_tz()).isoformat(timespec="seconds")

WEEKDAYS = {
    "понедельник": 0, "вторник": 1, "среда": 2, "среду": 2, "четверг": 3,
    "пятница": 4, "пятницу": 4, "суббота": 5, "субботу": 5,
    "воскресенье": 6, "понедельника": 0, "вторника": 1, "среды": 2,
    "четверга": 3, "пятницы": 4, "субботы": 5, "воскресенья": 6,
}

MONTHS = {
    "января": 1, "январь": 1, "февраля": 2, "февраль": 2, "марта": 3, "март": 3,
    "апреля": 4, "апрель": 4, "мая": 5, "май": 5, "июня": 6, "июнь": 6,
    "июля": 7, "июль": 7, "августа": 8, "август": 8, "сентября": 9, "сентябрь": 9,
    "октября": 10, "октябрь": 10, "ноября": 11, "ноябрь": 11,
    "декабря": 12, "декабрь": 12,
}

M_PROJECT = {"проект", "проекте", "проекту"}
M_TAG = {"тег", "тэг", "теги", "тэги", "тегом", "тэгом", "тегами"}
M_START = {"на", "дата", "начало", "начать"}
M_DEADLINE = {"срок", "дедлайн", "до", "крайний"}
M_NOTE = {"описание", "заметка", "заметку", "подробности", "детали", "комментарий"}
M_HIGH = {"важно", "срочно", "важное", "срочное", "приоритет"}
M_LOW = {"неважно", "потом", "низкий"}
SKIP = {"в", "с", "со", "во"}  # служебные перед маркером: "в проект", "с тегом"

STOP = M_PROJECT | M_TAG | M_START | M_DEADLINE | M_NOTE | M_HIGH | M_LOW | SKIP


def _norm(w: str) -> str:
    w = w.lower().replace("ё", "е")
    return re.sub(r"[^\w]", "", w, flags=re.UNICODE)


# ---------------------------------------------------------------- даты
def parse_date(words, i, today=None):
    """Пробует разобрать дату начиная со слова i. -> (date|None, consumed).

    Возвращает объект date, а не строку: превращение в ISO-datetime с
    таймзоной делает parse() через to_api_datetime (start vs deadline
    требуют разного времени суток).
    """
    today = today or date.today()
    if i >= len(words):
        return None, 0
    w = _norm(words[i])
    nxt = _norm(words[i + 1]) if i + 1 < len(words) else ""

    if w == "сегодня":
        return today, 1
    if w == "завтра":
        return today + timedelta(days=1), 1
    if w == "послезавтра":
        return today + timedelta(days=2), 1

    if w in WEEKDAYS:
        delta = (WEEKDAYS[w] - today.weekday()) % 7
        return today + timedelta(days=delta or 7), 1

    if w == "через" and nxt:
        m = re.match(r"^(\d+)$", nxt)
        n = int(m.group(1)) if m else {"день": 1, "неделю": 7, "месяц": 30}.get(nxt)
        if m:
            unit = _norm(words[i + 2]) if i + 2 < len(words) else "день"
            mult = 7 if unit.startswith("недел") else (30 if unit.startswith("месяц") else 1)
            return today + timedelta(days=n * mult), 3
        if n:
            return today + timedelta(days=n), 2

    # "24 июля" / "24 июля 2026"
    if re.match(r"^\d{1,2}$", w) and nxt in MONTHS:
        day, month = int(w), MONTHS[nxt]
        year = today.year
        third = _norm(words[i + 2]) if i + 2 < len(words) else ""
        consumed = 2
        if re.match(r"^\d{4}$", third):
            year, consumed = int(third), 3
        try:
            d = date(year, month, day)
        except ValueError:
            return None, 0
        if consumed == 2 and d < today:
            d = date(year + 1, month, day)
        return d, consumed

    # 24.07 / 24.07.2026 / 24-07-2026
    m = re.match(r"^(\d{1,2})[.\-/](\d{1,2})(?:[.\-/](\d{2,4}))?$", words[i].strip(" ,."))
    if m:
        day, month = int(m.group(1)), int(m.group(2))
        year = int(m.group(3)) if m.group(3) else today.year
        if year < 100:
            year += 2000
        try:
            d = date(year, month, day)
        except ValueError:
            return None, 0
        if not m.group(3) and d < today:
            d = date(year + 1, month, day)
        return d, 1

    return None, 0


# ---------------------------------------------------------------- результат
@dataclass
class ParsedTask:
    title: str = ""
    note: str = ""
    project_id: str = None
    project_title: str = None
    tag_ids: list = field(default_factory=list)
    tag_titles: list = field(default_factory=list)
    start: str = None
    deadline: str = None
    priority: int = 1
    warnings: list = field(default_factory=list)

    def summary(self) -> str:
        """Короткая строка для подтверждающей плашки."""
        head = self.project_title or "Входящие"
        tags = ("#" + " #".join(self.tag_titles)) if self.tag_titles else ""
        d = self.deadline or self.start
        when = d[:10] if isinstance(d, str) else (d.isoformat() if d else "")
        parts = [p for p in (head, tags, when) if p]
        return f"{' · '.join(parts)}: {self.title}"


def _take_entity(words, i, resolver, max_span=4):
    """Жадно подбирает 1..max_span слов под fuzzy-резолв.

    Span никогда не пересекает другой маркер: в "проект дом срок 15 августа"
    имя проекта закончится на слове "срок".
    -> (item|None, consumed, score, phrase)
    """
    limit = min(max_span, len(words) - i)
    stop_hit = False
    for n in range(1, limit + 1):
        if _norm(words[i + n - 1]) in STOP:
            limit, stop_hit = max(n - 1, 1), n > 1
            break

    best = (None, 1, 0.0, words[i] if i < len(words) else "")
    for n in range(1, limit + 1):
        phrase = " ".join(words[i:i + n])
        if len(_norm(phrase)) < 2:
            continue
        item, score = resolver(phrase)
        if score > best[2]:
            best = (item, n, score, phrase)

    if best[0] is None:
        # Не распознали. Если дальше идёт другой маркер — считаем, что всё до него
        # было (кривым) именем, и не рвём остальную грамматику. Иначе съедаем одно
        # слово, чтобы не утащить в никуда весь заголовок.
        used = limit if stop_hit else 1
        return None, used, best[2], " ".join(words[i:i + used])
    return best


def parse(text: str, client=None, today=None) -> ParsedTask:
    """client — SngClient с прогретым кэшем; без него резолв имён пропускается."""
    res = ParsedTask()
    words = text.strip().split()

    # "описание/заметка ..." ищем во всей фразе, а не только в голове
    for j in range(1, len(words) - 1):
        if _norm(words[j]) in M_NOTE:
            res.note = " ".join(words[j + 1:]).strip()
            words = words[:j]
            break

    i = 0
    while i < len(words):
        w = _norm(words[i])

        # служебное слово перед маркером: "в проект", "с тегом"
        if w in SKIP and i + 1 < len(words):
            nxt = _norm(words[i + 1])
            if nxt in M_PROJECT or nxt in M_TAG or nxt in M_DEADLINE:
                i += 1
                continue

        if w in M_PROJECT and i + 1 < len(words):
            if client is None:
                res.warnings.append("проект указан, но клиент недоступен")
                i += 2
                continue
            item, used, score, phrase = _take_entity(words, i + 1, client.resolve_project)
            if item:
                res.project_id, res.project_title = item["id"], item["title"]
            else:
                res.warnings.append(f"проект не распознан: «{phrase}» (score {score:.0f})")
            i += 1 + used
            continue

        if w in M_TAG and i + 1 < len(words):
            if client is None:
                res.warnings.append("тег указан, но клиент недоступен")
                i += 2
                continue
            item, used, score, phrase = _take_entity(words, i + 1, client.resolve_tag, max_span=3)
            if item:
                res.tag_ids.append(item["id"])
                res.tag_titles.append(item["title"])
            else:
                res.warnings.append(f"тег не распознан: «{phrase}» (score {score:.0f})")
            i += 1 + used
            continue

        if w in M_DEADLINE and i + 1 < len(words):
            iso, used = parse_date(words, i + 1, today)
            if iso:
                res.deadline = iso
                i += 1 + used
                continue

        if w in M_START and i + 1 < len(words):
            iso, used = parse_date(words, i + 1, today)
            if iso:
                res.start = iso
                i += 1 + used
                continue

        if w in M_HIGH:
            res.priority = 0
            i += 1
            continue

        if w in M_LOW:
            res.priority = 2
            i += 1
            continue

        # голая дата в начале: "завтра забрать кота..."
        if not res.title:
            iso, used = parse_date(words, i, today)
            if iso:
                res.start = iso
                i += used
                continue

        break  # маркеры кончились — дальше заголовок

    res.title = " ".join(words[i:]).strip(" ,.;:-—")

    if not res.title:
        res.title = text.strip() or "Без названия"
        res.warnings.append("заголовок пуст — взята вся фраза целиком")

    res.title = res.title[0].upper() + res.title[1:] if res.title else res.title

    # даты — в ISO-datetime с таймзоной (API требует явный оффсет).
    # Старт — начало дня, дедлайн — конец дня.
    res.start = to_api_datetime(res.start, end_of_day=False)
    res.deadline = to_api_datetime(res.deadline, end_of_day=True)

    # то, что не распозналось, не теряем — дописываем в заметку задачи
    if res.warnings:
        tail = "\n".join(res.warnings)
        res.note = (res.note + "\n\n" if res.note else "") + "[aqualocal] " + tail

    return res


if __name__ == "__main__":
    import sys
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass
    phrase = " ".join(sys.argv[1:]) or "проект ВЛП тег интеграция срок пятница подготовить смету"
    p = parse(phrase)
    print(f"title    : {p.title}")
    print(f"note     : {p.note!r}")
    print(f"start    : {p.start}")
    print(f"deadline : {p.deadline}")
    print(f"priority : {p.priority}  (0=высокий, 1=обычный, 2=низкий)")
    print(f"warnings : {p.warnings}")
    print("(имена проекта/тега здесь не резолвятся — нужен клиент, см. sender.py --dry)")
