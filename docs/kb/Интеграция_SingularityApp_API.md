---
title: Интеграция с SingularityApp REST API
вопрос: как создаётся задача в SingularityApp и какие грабли в их API уже пройдены
ключевые_слова: SingularityApp, REST, /v2/task, токен, projectId, tags, deadline,
  note, delta, externalId, кэш, openapi
модуль: singularity/
теги:
- интеграция
- singularity
статус: действует
обновлено: 2026-08-13
источник: разбор singularity/client.py, sender.py, fetch_openapi.py; комментарии о
  проверках на живом API от 23.07.2026, сессия Claude 2026-08-13
permalink: aqualocal/integratsiia-singularity-app-api
---

# Интеграция с SingularityApp REST API

Клиент написан на голом `urllib` без внешних HTTP-зависимостей (`singularity/client.py`).
Точка входа для приложения — `send_task(text)` из `singularity/sender.py:53`. Требуется подписка
с доступом к REST API и токен `SNG_TOKEN` (создаётся в ЛК `me.singularity-app.com/rest-tokens`).

## Контракт, подтверждённый по openapi.json

```
POST /v2/task
  title*      string
  projectId   string     не передан => Входящие
  tags        string[]   ID тегов вида A-<uuid>
  start       string
  deadline    string     отдельное поле, не путать со start
  priority    number     0=HIGH, 1=NORMAL, 2=LOW
  note        string
  externalId  string     идемпотентность
Ответы списков обёрнуты: {"tasks":[...]} / {"projects":[...]} / {"tags":[...]}
ID: задача T-…, проект P-…, тег A-<uuid>
```

Спека выкачивается скриптом `singularity/fetch_openapi.py` (умеет доставать её из
NestJS-специфичного `swagger-ui-init.js`), с флагом `--probe` делает живую проверку: создаёт задачу,
читает обратно, смотрит, что прилипло, и удаляет.

## Три грабли, за которые уже заплачено

**Даты нужны с временем и таймзоной.** На голую дату `2026-07-27` API отвечает HTTP 400. Поэтому
`to_api_datetime` (`parser.py:45`) достраивает `2026-07-27T23:59:00+03:00`: дедлайн — конец дня,
старт — начало (9:00), таймзона берётся локальная с машины.

**`note` — это plain text, а не Quill Delta.** В openapi поле описано как «Task note in delta format»,
но проверка на живом API 23.07.2026 показала: сервер хранит строку как есть, а приложение рисует её
буквально — отправленный Delta вылезает в описание задачи как `{"ops":[{"insert":"…"}]}`. Отсюда
`SNG_NOTE_FORMAT=plain` по умолчанию; значение `delta` оставлено на случай, если поведение когда-нибудь
приведут в соответствие со спекой (`client.py:131-145`). **Не менять без проверки на живом API.**

**Priority отправляется, только если не «обычный»** (`client.py:243`) — лишние поля в теле API не любит.

## Кэш НСИ и его роль

Проекты и теги тянутся списками по 500 и кладутся в `singularity/cache.json`, TTL —
`SNG_CACHE_TTL` (600 с). Удалённые (`removed`) отбрасываются (`client.py:197-212`).

Кэш нужен не только для скорости: по нему идёт fuzzy-резолв имён (см. [[Грамматика_голосовых_команд]]),
и **его же читает модуль Obsidian**, чтобы теги в заметках писались в том же написании, что и в
трекере — при этом в сеть не ходит. Обновить кэш не удалось (нет сети) — берётся вчерашний, резолв
продолжает работать (`sender.py:37-42`).

Файл `cache.json` содержит названия личных проектов и их ID — он в `.gitignore`, как и `aliases.json`.

`externalId` вида `aqualocal-<uuid4>` ставится на каждую задачу — защита от дублей при повторной
отправке из очереди.

## CLI для проверки без микрофона

```powershell
.venv\Scripts\python.exe -m singularity.sender --list          # проекты и теги с ID
.venv\Scripts\python.exe -m singularity.sender --dry "…"       # разбор без создания задачи
.venv\Scripts\python.exe -m singularity.sender "…"             # создать задачу
.venv\Scripts\python.exe -m singularity.sender --flush         # дослать offline-очередь
```

Смежное: [[Оффлайн_очереди_и_сохранность_фраз]], [[Конфигурация_через_env]].