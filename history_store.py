"""История распознаваний: аудио (WAV 16 кГц моно) + распознанный текст.

Аудио сохраняется сразу после записи, **до** распознавания: если Whisper упал,
VAD не нашёл речи или текст ушёл не в то окно — фрагмент можно переслушать
и распознать заново из окна истории.

Хранилище плоское: папка `HISTORY_DIR` с WAV-файлами и `index.json` — список
записей от новых к старым. Записей мало (`HISTORY_KEEP`), поэтому индекс
переписывается целиком: так проще дописать текст к уже существующей записи,
чем в append-only jsonl очередей.

Проверить содержимое истории без микрофона:
    .venv\\Scripts\\python.exe -m history_store
"""

import json
import logging
import threading
import wave
from datetime import datetime
from pathlib import Path

import numpy as np

BASE = Path(__file__).parent


def load_env():
    """Мини-парсер .env (без зависимостей). Такой же, как в остальных модулях."""
    env = {}
    p = BASE / '.env'
    if p.exists():
        for line in p.read_text(encoding='utf-8').splitlines():
            line = line.strip()
            if not line or line.startswith('#') or '=' not in line:
                continue
            k, v = line.split('=', 1)
            env[k.strip()] = v.strip().strip('"').strip("'")
    return env


ENV = load_env()

ENABLED = (ENV.get('HISTORY_ENABLED', '') or '1').strip().lower() not in ('0', 'false', 'no')
DIR = Path((ENV.get('HISTORY_DIR', '') or str(BASE / 'history')).strip())
try:
    KEEP = max(1, int((ENV.get('HISTORY_KEEP', '') or '10').strip()))
except ValueError:
    KEEP = 10

INDEX = DIR / 'index.json'

# Индекс трогают три стороны: рабочий поток (диктовка), поток STT-сервера и
# окно истории (повторное распознавание) — все операции с файлом под замком.
_lock = threading.Lock()

STATUS_RECORDED = 'записано'      # аудио есть, распознавание ещё не закончилось
STATUS_DONE = 'распознано'
STATUS_NO_SPEECH = 'нет речи'     # VAD не нашёл речи, Whisper не запускался
STATUS_ERROR = 'ошибка'


def _read():
    if not INDEX.exists():
        return []
    try:
        data = json.loads(INDEX.read_text(encoding='utf-8'))
        return data if isinstance(data, list) else []
    except Exception as e:
        logging.error(f"История: индекс не прочитан ({e}) — считаем пустым.")
        return []


def _write(records):
    INDEX.write_text(json.dumps(records, ensure_ascii=False, indent=1), encoding='utf-8')


def _prune(records):
    """Оставляем KEEP последних записей, WAV-файлы остальных удаляем."""
    for rec in records[KEEP:]:
        try:
            (DIR / rec.get('audio', '')).unlink(missing_ok=True)
        except Exception as e:
            logging.error(f"История: не удалён старый файл {rec.get('audio')}: {e}")
    return records[:KEEP]


def save(audio_np, sample_rate, mode):
    """Кладёт фрагмент в историю до распознавания. Возвращает id записи или None.

    Ошибка здесь не должна ломать диктовку — она только логируется.
    """
    if not ENABLED:
        return None
    try:
        DIR.mkdir(parents=True, exist_ok=True)
        rec_id = datetime.now().strftime('%Y-%m-%d_%H%M%S_%f')[:-3]
        name = rec_id + '.wav'

        pcm = np.clip(np.asarray(audio_np, dtype=np.float32), -1.0, 1.0)
        pcm = (pcm * 32767).astype('<i2')
        with wave.open(str(DIR / name), 'wb') as w:
            w.setnchannels(1)
            w.setsampwidth(2)
            w.setframerate(sample_rate)
            w.writeframes(pcm.tobytes())

        rec = {
            'id': rec_id,
            'ts': datetime.now().isoformat(timespec='seconds'),
            'mode': mode,
            'seconds': round(len(pcm) / float(sample_rate), 1),
            'status': STATUS_RECORDED,
            'text': '',
            'audio': name,
        }
        with _lock:
            _write(_prune([rec] + _read()))
        return rec_id
    except Exception as e:
        logging.error(f"История: фрагмент не сохранён: {e}", exc_info=True)
        return None


def update(rec_id, text=None, status=None):
    """Дописывает результат распознавания к уже сохранённой записи."""
    if not ENABLED or not rec_id:
        return
    try:
        with _lock:
            records = _read()
            for rec in records:
                if rec.get('id') == rec_id:
                    if text is not None:
                        rec['text'] = text
                    if status is not None:
                        rec['status'] = status
                    _write(records)
                    return
    except Exception as e:
        logging.error(f"История: запись {rec_id} не обновлена: {e}")


def recent():
    """Записи от новых к старым."""
    if not ENABLED:
        return []
    with _lock:
        return _read()


def audio_file(rec):
    return DIR / rec.get('audio', '')


def read_audio(rec):
    """WAV из истории → массив float32 для повторного распознавания."""
    with wave.open(str(audio_file(rec)), 'rb') as w:
        rate = w.getframerate()
        frames = w.readframes(w.getnframes())
    return np.frombuffer(frames, dtype='<i2').astype(np.float32) / 32768.0, rate


if __name__ == '__main__':
    if not ENABLED:
        print('История выключена (HISTORY_ENABLED=0)')
    records = recent()
    print(f'{DIR} — записей: {len(records)} (храним {KEEP})')
    for rec in records:
        print(f"{rec['ts']}  {rec['mode']:6}  {rec['seconds']:5.1f} с  "
              f"{rec['status']:10}  {(rec.get('text') or '')[:70]}")
