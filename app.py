import warnings

warnings.filterwarnings("ignore", message=".*pkg_resources is deprecated.*")

import os
import sys
import json
import logging
from pathlib import Path
from datetime import datetime

# ================= 0. ЛОГИРОВАНИЕ (ВМЕСТО КОНСОЛИ) =================
# Все print() заменены на логи, чтобы вы видели историю работы без окна
log_file = Path(__file__).parent / 'aqualocal.log'
logging.basicConfig(
    filename=str(log_file),
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s',
    encoding='utf-8'
)


# Перехватчик любых критических падений программы (чтобы понять причину закрытия)
def handle_exception(exc_type, exc_value, exc_traceback):
    if issubclass(exc_type, KeyboardInterrupt):
        sys.__excepthook__(exc_type, exc_value, exc_traceback)
        return
    logging.critical("КРИТИЧЕСКАЯ ОШИБКА (Crash):", exc_info=(exc_type, exc_value, exc_traceback))


sys.excepthook = handle_exception
logging.info("=== Запуск AquaLocal STT ===")

import ctypes

# ================= 0.5 ЗАЩИТА ОТ ДВОЙНОГО ЗАПУСКА =================
# Создаем уникальное имя для нашего приложения
MUTEX_NAME = "AquaLocal_STT_Mutex_v1"

# Пытаемся создать системный Mutex
mutex = ctypes.windll.kernel32.CreateMutexW(None, False, MUTEX_NAME)
last_error = ctypes.windll.kernel32.GetLastError()

# Ошибка 183 (ERROR_ALREADY_EXISTS) означает, что мьютекс уже создан другой копией
if last_error == 183:
    logging.warning("Попытка двойного запуска! Программа уже работает в трее. Закрываем эту копию.")
    sys.exit(0)

# Если скрипт дошел сюда, значит это первая и единственная копия.
logging.info("Мьютекс успешно захвачен. Запуск разрешен.")


# ================= 1. ПОДГРУЗКА БИБЛИОТЕК NVIDIA =================
def setup_nvidia_paths():
    try:
        venv_base = Path(sys.executable).parent.parent
        nvidia_base_path = venv_base / 'Lib' / 'site-packages' / 'nvidia'
        if not nvidia_base_path.exists():
            logging.warning("Папка nvidia не найдена в site-packages.")
            return

        paths_to_add = [str(p) for p in [
            nvidia_base_path / 'cuda_runtime' / 'bin',
            nvidia_base_path / 'cublas' / 'bin',
            nvidia_base_path / 'cudnn' / 'bin',
            nvidia_base_path / 'cuda_nvrtc' / 'bin'
        ] if p.exists()]

        if paths_to_add:
            for env_var in ['CUDA_PATH', 'PATH']:
                curr = os.environ.get(env_var, '')
                os.environ[env_var] = os.pathsep.join(paths_to_add + [curr] if curr else paths_to_add)
            logging.info("Пути к библиотекам NVIDIA успешно прописаны.")
    except Exception as e:
        logging.error(f"Ошибка путей NVIDIA: {e}")


setup_nvidia_paths()

import queue

import io
import math
import time
import tkinter as tk
import threading
import numpy as np
import sounddevice as sd
import keyboard
import pyperclip
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from faster_whisper import WhisperModel
from faster_whisper.audio import decode_audio
from faster_whisper.vad import get_speech_timestamps, VadOptions
import pystray
from PIL import Image, ImageDraw


# sys.excepthook НЕ покрывает потоки: падение рабочего потока ушло бы в stderr,
# которого у pythonw.exe нет, и в логе осталась бы необъяснимая тишина.
def handle_thread_exception(args):
    if issubclass(args.exc_type, SystemExit):
        return
    logging.critical(
        f"КРИТИЧЕСКАЯ ОШИБКА в потоке {getattr(args.thread, 'name', '?')}:",
        exc_info=(args.exc_type, args.exc_value, args.exc_traceback))


threading.excepthook = handle_thread_exception

# ================= 2. НАСТРОЙКИ =================
MODE_INSERT = 'insert'  # вставка распознанного текста в позицию курсора
MODE_TASK = 'task'      # отправка задачи в SingularityApp (+ дубль в Obsidian)
MODE_NOTE = 'note'      # строка в файл дня в Obsidian


# ---------------------------------------------------------------- .env
def load_env():
    """Мини-парсер .env (без зависимостей). Такой же, как в модулях."""
    env = {}
    p = Path(__file__).parent / '.env'
    if p.exists():
        for line in p.read_text(encoding='utf-8').splitlines():
            line = line.strip()
            if not line or line.startswith('#') or '=' not in line:
                continue
            k, v = line.split('=', 1)
            env[k.strip()] = v.strip().strip('"').strip("'")
    return env


ENV = load_env()

# Хоткеи берутся из .env, с падением на F19/F20/F21. Синтаксис — как у пакета
# keyboard: 'f19', 'ctrl+d', 'alt+shift+1', 'ctrl+alt+space'. Регистр неважен.
DEFAULT_HOTKEYS = {MODE_INSERT: 'f19', MODE_TASK: 'f20', MODE_NOTE: 'f21'}


def _hotkey(mode, default):
    return (ENV.get('HOTKEY_' + mode.upper(), '') or default).strip().lower()


# Порядок важен: проверяется сверху вниз, первое совпадение выигрывает.
# Более длинные комбинации ставим раньше, чтобы 'ctrl+d' не перехватывался 'd'.
HOTKEYS = {
    _hotkey(MODE_INSERT, DEFAULT_HOTKEYS[MODE_INSERT]): MODE_INSERT,
    _hotkey(MODE_TASK, DEFAULT_HOTKEYS[MODE_TASK]): MODE_TASK,
    _hotkey(MODE_NOTE, DEFAULT_HOTKEYS[MODE_NOTE]): MODE_NOTE,
}
HOTKEYS = dict(sorted(HOTKEYS.items(), key=lambda kv: kv[0].count('+'), reverse=True))

# Тег, которым помечается дубль задачи в дневнике (F20).
TASK_NOTE_TAG = (ENV.get('OBS_TASK_TAG', '') or 'задача').strip()

SAMPLE_RATE = 16000
MODEL_SIZE = "large-v3"
LANGUAGE = "ru"

audio_queue = queue.Queue()
is_recording = False
app_running = True
current_volume = 0.0        # Для анимации
current_mode = MODE_INSERT  # Режим текущей записи (определяет цвет оверлея)
flash_queue = queue.Queue()  # Сообщения для подтверждающей плашки (worker -> UI)

# Идёт распознавание: оверлей показывает статус вместо волны. Звуковых сигналов
# в программе нет — вся обратная связь визуальная, через плашку оверлея.
is_transcribing = False
transcribe_started = 0.0    # момент старта — для счётчика секунд в статусе

# Модель одна на всех: микрофон (хоткеи) и локальный STT-сервер ходят к ней
# по очереди, иначе два transcribe разом дерутся за видеопамять.
model_lock = threading.Lock()


def flash(text, ok=True, sec=None):
    """Показать плашку в оверлее (из рабочего потока). sec — сколько держать."""
    flash_queue.put((text, ok, sec))

# Отправщик в SingularityApp. Если модуля нет или он не импортируется,
# задачи копятся в singularity/queue.jsonl и не теряются.
try:
    from singularity.sender import send_task

    logging.info("singularity.sender подключён — F20 шлёт задачи напрямую в API.")
except Exception as _e:
    send_task = None
    logging.warning(f"singularity.sender недоступен ({_e}) — режим F20 работает в offline-очередь.")

# Запись в Obsidian. При недоступной папке заметки копятся в obsidian/queue.jsonl.
try:
    from obsidian.writer import append_note, enqueue as enqueue_note, INBOX_DIR

    logging.info(f"obsidian.writer подключён — F21 пишет в {INBOX_DIR}")
except Exception as _e:
    append_note = None
    enqueue_note = None
    logging.warning(f"obsidian.writer недоступен ({_e}) — режим F21 пишет в локальную очередь.")


def audio_callback(indata, frames, time_info, status):
    global current_volume
    if status:
        logging.warning(f"Аудио статус: {status}")
    if is_recording and app_running:
        audio_queue.put(indata.copy())
        # Вычисляем RMS (громкость) для анимации
        rms = np.sqrt(np.mean(indata ** 2))
        current_volume = float(rms)
    else:
        current_volume = 0.0


# ================= 2.5 ОБРАБОТЧИКИ РЕЖИМОВ =================
def insert_at_cursor(text):
    """F19: подменяем буфер, шлём Ctrl+V, возвращаем буфер обратно."""
    orig_clip = pyperclip.paste()
    pyperclip.copy(text + " ")
    time.sleep(0.05)
    keyboard.send('ctrl+v')
    time.sleep(0.1)
    pyperclip.copy(orig_clip)


def enqueue_task(text, reason=""):
    """Кладём задачу в offline-очередь на диск, чтобы ничего не потерялось."""
    path = Path(__file__).parent / 'singularity' / 'queue.jsonl'
    path.parent.mkdir(exist_ok=True)
    rec = {
        "ts": datetime.now().isoformat(timespec='seconds'),
        "text": text,
        "sent": False,
        "reason": reason,
    }
    with open(path, 'a', encoding='utf-8') as f:
        f.write(json.dumps(rec, ensure_ascii=False) + "\n")
    logging.info(f"Задача записана в offline-очередь: {text}")


def mirror_task_to_obsidian(text):
    """Дубль задачи в дневник с тегом «задача» — до отправки в API.

    Смысл: если Singularity лежит (или ляжет позже), задача уже зафиксирована
    в текстовом виде и не пропадёт. Сбой здесь не должен мешать отправке в API,
    поэтому всё в try и ошибка только логируется.
    """
    if append_note is not None:
        try:
            append_note(text, extra_tags=[TASK_NOTE_TAG])
            return
        except Exception as e:
            logging.error(f"Дубль задачи в Obsidian не записан ({e}) — кладу в очередь Obsidian.")
            if enqueue_note is not None:
                try:
                    enqueue_note(text, reason=str(e), extra_tags=[TASK_NOTE_TAG])
                    return
                except Exception:
                    pass
    # модуль Obsidian не подключён — сырая очередь с тегом
    enqueue_note_fallback(text, reason="дубль задачи, writer не подключён", extra_tags=[TASK_NOTE_TAG])


def handle_task(text):
    """F20: сначала дубль в дневник (тег «задача»), потом отправка в API.

    Курсор и буфер обмена НЕ трогаем.
    """
    mirror_task_to_obsidian(text)

    if send_task is not None:
        try:
            summary = send_task(text)
            logging.info(f"Задача отправлена в SingularityApp: {summary}")
            flash(summary, True)
            return
        except Exception as e:
            logging.error(f"Ошибка отправки в SingularityApp: {e}")
            enqueue_task(text, reason=str(e))
            flash(f"в очередь: {text}", False)
            return

    enqueue_task(text, reason="sender не подключён")
    flash(f"в очередь: {text}", True)


def enqueue_note_fallback(text, reason="", extra_tags=None):
    """Модуль Obsidian вообще не импортировался — пишем сырую очередь сами."""
    path = Path(__file__).parent / 'obsidian' / 'queue.jsonl'
    path.parent.mkdir(parents=True, exist_ok=True)
    rec = {
        "ts": datetime.now().isoformat(timespec='seconds'),
        "text": text,
        "sent": False,
        "reason": reason,
    }
    if extra_tags:
        rec["extra_tags"] = list(extra_tags)
    with open(path, 'a', encoding='utf-8') as f:
        f.write(json.dumps(rec, ensure_ascii=False) + "\n")
    logging.info(f"Заметка записана в offline-очередь: {text}")


def handle_note(text):
    """F21: строка в файл дня Obsidian. Буфер обмена и курсор НЕ трогаем."""
    if append_note is not None:
        try:
            summary = append_note(text)
            logging.info(f"Заметка записана в Obsidian: {summary}")
            flash(summary, True)
            return
        except Exception as e:
            logging.error(f"Ошибка записи в Obsidian: {e}")
            (enqueue_note or enqueue_note_fallback)(text, reason=str(e))
            flash(f"в очередь: {text}", False)
            return

    enqueue_note_fallback(text, reason="writer не подключён")
    flash(f"в очередь: {text}", True)


# ================= 2.6 ЛОКАЛЬНЫЙ STT-СЕРВЕР =================
# Отдаёт распознавание другим программам на этой машине: POST /stt с телом-аудио
# → {"text": "..."}. Формат любой, какой понимает PyAV (ogg/opus из Telegram,
# mp3, wav) — перекодировать снаружи не надо. Модель уже в видеопамяти, второй
# копии не появляется. Выключен, пока в .env не задан STT_PORT.
def start_stt_server(recognize):
    try:
        port = int((ENV.get('STT_PORT', '') or '0').strip())
    except ValueError:
        port = 0
    if not port:
        logging.info("STT_PORT не задан — локальный сервер распознавания выключен.")
        return

    class Handler(BaseHTTPRequestHandler):
        protocol_version = 'HTTP/1.1'

        def do_POST(self):
            if self.path.rstrip('/') != '/stt':
                self.send_error(404)
                return
            size = int(self.headers.get('Content-Length') or 0)
            data = self.rfile.read(size) if size else b''
            if not data:
                self.send_error(400, 'empty body')
                return

            started = time.time()
            try:
                audio = decode_audio(io.BytesIO(data), sampling_rate=SAMPLE_RATE)
                # Речи нет — модель не трогаем, отвечаем пустым текстом.
                if get_speech_timestamps(audio, VadOptions(min_silence_duration_ms=500)):
                    text = recognize(audio)
                else:
                    text = ''
                logging.info(f"[stt] {len(audio) / SAMPLE_RATE:.1f} сек аудио за "
                             f"{time.time() - started:.1f} с: {text or '(речи нет)'}")
                self._reply(200, {"text": text})
            except Exception as e:
                logging.error(f"STT-сервер: {e}", exc_info=True)
                self._reply(500, {"error": str(e)})

        def _reply(self, code, payload):
            body = json.dumps(payload, ensure_ascii=False).encode('utf-8')
            self.send_response(code)
            self.send_header('Content-Type', 'application/json; charset=utf-8')
            self.send_header('Content-Length', str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *args):
            pass        # у pythonw нет stderr, лог ведём сами

    try:
        srv = ThreadingHTTPServer(('127.0.0.1', port), Handler)
    except Exception as e:
        logging.error(f"STT-сервер не поднялся на порту {port}: {e}")
        return

    threading.Thread(target=srv.serve_forever, name="stt_server", daemon=True).start()
    logging.info(f"STT-сервер слушает 127.0.0.1:{port} (POST /stt).")


# ================= 3. РАБОЧИЙ ПОТОК (STT) =================
def stt_worker():
    global is_recording, current_mode

    logging.info(f"Загрузка модели {MODEL_SIZE} в VRAM (FP16)...")

    # Сторож: WhisperModel — нативный вызов, он может не упасть, а зависнуть
    # (например, если VRAM занята другим процессом). Без этого в логе была бы
    # просто тишина после строки "Загрузка модели".
    loaded = threading.Event()

    def watchdog(started_at):
        while not loaded.wait(30):
            waited = int(time.time() - started_at)
            logging.warning(
                f"Модель грузится уже {waited} сек. Обычно это занимает ~30. "
                f"Проверь, не занята ли VRAM другим процессом (nvidia-smi).")

    threading.Thread(target=watchdog, args=(time.time(),), daemon=True).start()

    try:
        model = WhisperModel(MODEL_SIZE, device="cuda", compute_type="float16")
        loaded.set()
        hk = ", ".join(f"{k.upper()}={v}" for k, v in HOTKEYS.items())
        logging.info(f"Модель успешно загружена! Хоткеи: {hk}")
        flash("AquaLocal готов", True, 1.5)  # видимый признак готовности вместо сигнала
    except Exception as e:
        loaded.set()
        logging.critical(f"Ошибка загрузки модели (скорее всего проблема с CUDA/cuDNN): {e}",
                         exc_info=True)
        return

    def recognize(audio):
        """Массив float32 16 кГц → текст. Обращение к модели сериализовано замком:
        микрофон и локальный STT-сервер не должны запускать transcribe разом."""
        with model_lock:
            segments, _ = model.transcribe(
                audio,
                beam_size=1,
                language=LANGUAGE,
                vad_filter=True,
                vad_parameters=dict(min_silence_duration_ms=500)
            )
            return " ".join(s.text.strip() for s in segments).strip()

    start_stt_server(recognize)

    def process_audio(mode):
        global is_transcribing, transcribe_started

        audio_data = []
        while not audio_queue.empty():
            audio_data.append(audio_queue.get())
        if not audio_data:
            flash("не расслышал", False, 1.0)
            return

        audio_np = np.concatenate(audio_data, axis=0).flatten().astype(np.float32)

        # Речи нет — модель на видеокарте не будим. Детектор Silero (он же
        # используется внутри Whisper) отрабатывает на процессоре за 10–20 мс
        # и, в отличие от порога громкости, не принимает шум за речь.
        if not get_speech_timestamps(audio_np, VadOptions(min_silence_duration_ms=500)):
            logging.info(f"[{mode}] Речи не найдено ({len(audio_np) / SAMPLE_RATE:.1f} сек) — Whisper не запускался.")
            flash("не расслышал", False, 1.0)
            return

        is_transcribing = True          # оверлей покажет статус вместо волны
        transcribe_started = time.time()
        try:
            text = recognize(audio_np)
            is_transcribing = False     # дальше говорят плашки подтверждения

            if text:
                logging.info(f"[{mode}] Распознано ({len(audio_np) / SAMPLE_RATE:.1f} сек аудио): {text}")
                if mode == MODE_TASK:
                    handle_task(text)
                elif mode == MODE_NOTE:
                    handle_note(text)
                else:
                    insert_at_cursor(text)
            else:
                logging.info("Распознана тишина (отброшено VAD).")
                flash("не расслышал", False, 1.0)
        except Exception as e:
            logging.error(f"Ошибка транскрибации: {e}", exc_info=True)
            flash("ошибка распознавания", False)
        finally:
            is_transcribing = False

    # Бесконечный цикл перезапуска микрофона (Защита от сбоев)
    while app_running:
        try:
            with sd.InputStream(samplerate=SAMPLE_RATE, channels=1, dtype='float32', callback=audio_callback):
                logging.info("Аудио-поток (InputStream) успешно открыт. Хоткеи слушаются.")

                while app_running:
                    pressed = None
                    for key, mode in HOTKEYS.items():
                        if keyboard.is_pressed(key):
                            pressed = (key, mode)
                            break

                    if pressed:
                        key, mode = pressed
                        current_mode = mode

                        # Очищаем старые данные перед новой записью
                        while not audio_queue.empty():
                            audio_queue.get()

                        is_recording = True

                        # Цикл удержания клавиши
                        while keyboard.is_pressed(key) and app_running:
                            time.sleep(0.01)

                        # Отпускание клавиши
                        is_recording = False
                        process_audio(mode)

                    time.sleep(0.05)

        except Exception as e:
            logging.error(f"Сбой аудио-устройства (InputStream упал): {e}. Перезапуск через 2 секунды...")
            time.sleep(2)


# ================= 3.5 ВИЗУАЛИЗАЦИЯ (ОВЕРЛЕЙ ЗАПИСИ) =================
class RecordingOverlay:
    """Белая плашка внизу экрана: мигающая точка + волны, реагирующие на звук.

    Цвет зависит от режима: красный — вставка в курсор (F19),
    синий — задача в SingularityApp (F20),
    зелёный — заметка в Obsidian (F21).
    """
    W, H = 300, 56
    BARS = 34          # количество столбиков волны
    FPS_MS = 33        # ~30 кадров/сек
    RED = '#e53935'    # MODE_INSERT
    BLUE = '#2979ff'   # MODE_TASK
    GREEN = '#2e7d32'  # MODE_NOTE
    GREY = '#9e9e9e'
    TRANS = '#0f0f0f'  # "прозрачный" цвет (не должен встречаться в рисунке)
    FLASH_SEC = 1.8    # сколько держим подтверждающую плашку

    def __init__(self, root):
        self.root = root
        root.overrideredirect(True)                # без рамки
        root.attributes('-topmost', True)          # поверх всех окон
        root.attributes('-transparentcolor', self.TRANS)
        root.attributes('-alpha', 0.0)             # скрыта (прячем через alpha, а не withdraw,
        root.config(bg=self.TRANS)                 # чтобы окно никогда не забирало фокус)

        sw, sh = root.winfo_screenwidth(), root.winfo_screenheight()
        root.geometry(f'{self.W}x{self.H}+{(sw - self.W) // 2}+{sh - self.H - 90}')

        self.canvas = tk.Canvas(root, width=self.W, height=self.H,
                                bg=self.TRANS, highlightthickness=0)
        self.canvas.pack()

        self.history = [0.0] * self.BARS
        self.shown = False
        self.flash_text = None
        self.flash_until = 0.0
        self.flash_ok = True

        root.update_idletasks()
        self._make_clickthrough()
        self._tick()

    def accent(self):
        """Цвет по текущему режиму."""
        return {MODE_TASK: self.BLUE, MODE_NOTE: self.GREEN}.get(current_mode, self.RED)

    def _make_clickthrough(self):
        """WinAPI: окно не активируется и пропускает клики сквозь себя."""
        try:
            GWL_EXSTYLE = -20
            WS_EX_NOACTIVATE = 0x08000000
            WS_EX_TOOLWINDOW = 0x00000080
            WS_EX_TRANSPARENT = 0x00000020
            hwnd = ctypes.windll.user32.GetParent(self.root.winfo_id())
            style = ctypes.windll.user32.GetWindowLongW(hwnd, GWL_EXSTYLE)
            ctypes.windll.user32.SetWindowLongW(
                hwnd, GWL_EXSTYLE,
                style | WS_EX_NOACTIVATE | WS_EX_TOOLWINDOW | WS_EX_TRANSPARENT)
        except Exception as e:
            logging.error(f"Не удалось сделать оверлей click-through: {e}")

    def _draw_pill(self):
        c, w, h = self.canvas, self.W, self.H
        r = h // 2
        c.create_oval(0, 0, 2 * r, h, fill='white', outline='')
        c.create_oval(w - 2 * r, 0, w, h, fill='white', outline='')
        c.create_rectangle(r, 0, w - r, h, fill='white', outline='')

    def _redraw(self):
        c = self.canvas
        c.delete('all')
        self._draw_pill()
        color = self.accent()

        # Мигающая точка (2 раза в секунду)
        if int(time.time() * 2) % 2 == 0:
            c.create_oval(20, self.H / 2 - 6, 32, self.H / 2 + 6, fill=color, outline='')

        # Волна: бегущие столбики, высота = громкость
        x0, pad_r = 46, 22
        step = (self.W - x0 - pad_r) / self.BARS
        mid = self.H / 2
        max_h = (self.H - 18) / 2
        for i, v in enumerate(self.history):
            h = max(2.0, v * max_h)
            x = x0 + i * step + step / 2
            c.create_line(x, mid - h, x, mid + h, fill=color,
                          width=max(2, int(step * 0.55)), capstyle='round')

    def _redraw_flash(self):
        """Подтверждение отправки задачи: точка + текст, без волны."""
        c = self.canvas
        c.delete('all')
        self._draw_pill()
        color = self.accent() if self.flash_ok else self.RED
        c.create_oval(20, self.H / 2 - 6, 32, self.H / 2 + 6, fill=color, outline='')

        txt = self.flash_text or ''
        if len(txt) > 34:
            txt = txt[:33] + '…'
        c.create_text(42, self.H / 2, text=txt, anchor='w',
                      font=('Segoe UI', 10), fill='#212121')

    def _redraw_status(self):
        """Запись кончилась, работает Whisper: точка + «идёт распознавание».

        Точки бегут, чтобы было видно, что процесс жив; если распознавание
        затянулось (VRAM занята, длинная фраза) — добавляется счётчик секунд.
        """
        c = self.canvas
        c.delete('all')
        self._draw_pill()
        c.create_oval(20, self.H / 2 - 6, 32, self.H / 2 + 6, fill=self.accent(), outline='')

        waited = time.time() - transcribe_started
        txt = 'идёт распознавание'
        if waited > 3:
            txt += f' {int(waited)} с'
        txt += '.' * (int(time.time() * 2) % 4)
        c.create_text(42, self.H / 2, text=txt, anchor='w',
                      font=('Segoe UI', 10), fill='#212121')

    def _tick(self):
        if not app_running:
            return

        # забираем подтверждения из рабочего потока
        try:
            while True:
                text, ok, sec = flash_queue.get_nowait()
                self.flash_text, self.flash_ok = text, ok
                self.flash_until = time.time() + (sec or self.FLASH_SEC)
        except queue.Empty:
            pass

        if is_recording:
            self.flash_until = 0.0
            if not self.shown:
                self.root.attributes('-alpha', 0.97)
                self.shown = True
            # сдвигаем волну влево, добавляем текущую громкость
            self.history.pop(0)
            self.history.append(min(1.0, current_volume * 14))
            self._redraw()
        elif is_transcribing:
            self.flash_until = 0.0
            if not self.shown:
                self.root.attributes('-alpha', 0.97)
                self.shown = True
            self._redraw_status()
        elif time.time() < self.flash_until:
            if not self.shown:
                self.root.attributes('-alpha', 0.97)
                self.shown = True
            self._redraw_flash()
        elif self.shown:
            self.root.attributes('-alpha', 0.0)
            self.history = [0.0] * self.BARS
            self.shown = False

        self.root.after(self.FPS_MS, self._tick)


# ================= 4. СИСТЕМНЫЙ ТРЕЙ (GUI) =================
def create_image():
    """Рисуем иконку (синий квадрат, белый круг)"""
    image = Image.new('RGB', (64, 64), color=(100, 120, 215))
    dc = ImageDraw.Draw(image)
    dc.ellipse((16, 16, 48, 48), fill=(255, 255, 255))
    return image


def exit_action(icon, item):
    global app_running
    logging.info("Пользователь запросил выход через трей. Завершение работы...")
    app_running = False
    icon.stop()
    os._exit(0)


# Запускаем STT в фоне
worker_thread = threading.Thread(target=stt_worker, name="stt_worker", daemon=True)
worker_thread.start()

# Подписи в трее — из реальных хоткеев, чтобы отражали .env
def _key_for(mode):
    for k, m in HOTKEYS.items():
        if m == mode:
            return k.upper()
    return "?"


# Трей — в фоновом режиме, а главный поток отдаем Tkinter (визуализация)
try:
    menu = pystray.Menu(
        pystray.MenuItem("AquaLocal", lambda: None, enabled=False),
        pystray.MenuItem(f"{_key_for(MODE_INSERT)} — вставить в курсор", lambda: None, enabled=False),
        pystray.MenuItem(f"{_key_for(MODE_TASK)} — задача в SingularityApp", lambda: None, enabled=False),
        pystray.MenuItem(f"{_key_for(MODE_NOTE)} — заметка в Obsidian", lambda: None, enabled=False),
        pystray.MenuItem("Выход", exit_action)
    )
    icon = pystray.Icon("AquaLocal", create_image(), "AquaLocal STT", menu)
    icon.run_detached()
    logging.info("Иконка в трее создана.")
except Exception as e:
    logging.critical(f"Ошибка запуска системного трея (pystray): {e}", exc_info=True)

# Оверлей записи (блокирующий mainloop в главном потоке)
try:
    root = tk.Tk()
    overlay = RecordingOverlay(root)
    logging.info("Оверлей создан, отдаём главный поток Tkinter.")
    root.mainloop()
except Exception as e:
    logging.critical(f"Ошибка запуска оверлея (tkinter): {e}", exc_info=True)
