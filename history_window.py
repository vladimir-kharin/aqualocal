"""Окно истории распознаваний (tkinter).

Живёт в главном потоке, как и оверлей: из рабочих потоков виджеты трогать
нельзя. Открывается командой из трея через очередь UI-команд `app.py`.

Повторное распознавание уходит в отдельный поток (иначе mainloop встал бы
на секунды, а вместе с ним и оверлей); результат окно забирает из своей
очереди по таймеру. Доступ к модели сериализован замком в `app.py`, так что
с обычной диктовкой распознавание из истории не подерётся.
"""

import logging
import os
import queue
import threading
import tkinter as tk
from tkinter import ttk

import pyperclip

import history_store

MODE_TITLES = {'insert': 'курсор', 'task': 'задача', 'note': 'заметка'}


class HistoryWindow:
    """Одно окно на приложение: повторный вызов из трея поднимает существующее."""

    POLL_MS = 700          # перечитывание индекса и результатов распознавания
    _instance = None

    @classmethod
    def open(cls, root, recognize):
        if cls._instance is not None and cls._instance.alive():
            cls._instance.lift()
            return cls._instance
        cls._instance = cls(root, recognize)
        return cls._instance

    def __init__(self, root, recognize):
        self.recognize = recognize
        self.results = queue.Queue()    # (rec_id, text, error) из фонового потока
        self.rows = []                  # записи в порядке строк дерева
        self.busy = False               # идёт повторное распознавание

        self.win = tk.Toplevel(root)
        self.win.title('AquaLocal — история распознаваний')
        self.win.geometry('860x480')
        self.win.minsize(680, 380)
        self.win.protocol('WM_DELETE_WINDOW', self.close)

        cols = ('time', 'mode', 'sec', 'status', 'text')
        self.tree = ttk.Treeview(self.win, columns=cols, show='headings', height=10)
        for key, title, width, anchor in (
                ('time', 'Время', 130, 'w'),
                ('mode', 'Режим', 80, 'w'),
                ('sec', 'Длит.', 60, 'e'),
                ('status', 'Статус', 100, 'w'),
                ('text', 'Текст', 460, 'w')):
            self.tree.heading(key, text=title)
            self.tree.column(key, width=width, anchor=anchor,
                             stretch=(key == 'text'))
        self.tree.pack(fill='both', expand=True, padx=8, pady=(8, 4))
        self.tree.bind('<<TreeviewSelect>>', lambda _e: self._show_selected())

        self.text = tk.Text(self.win, height=6, wrap='word', font=('Segoe UI', 10))
        self.text.pack(fill='x', padx=8)

        bar = tk.Frame(self.win)
        bar.pack(fill='x', padx=8, pady=8)
        self.btn_copy = tk.Button(bar, text='Копировать текст', command=self._copy)
        self.btn_again = tk.Button(bar, text='Распознать заново', command=self._recognize_again)
        self.btn_play = tk.Button(bar, text='Прослушать', command=self._play)
        for b in (self.btn_copy, self.btn_again, self.btn_play):
            b.pack(side='left', padx=(0, 6))
        self.status = tk.Label(bar, text='', anchor='w', fg='#555555')
        self.status.pack(side='left', padx=8)

        self._snapshot = None
        self._after_id = None
        self._reload(force=True)
        self._poll()

    # ---------------------------------------------------------------- служебное
    def alive(self):
        try:
            return bool(self.win.winfo_exists())
        except Exception:
            return False

    def lift(self):
        self.win.deiconify()
        self.win.lift()
        self.win.focus_force()

    def close(self):
        if self._after_id is not None:
            try:
                self.win.after_cancel(self._after_id)
            except Exception:
                pass
            self._after_id = None
        self.win.destroy()

    def _selected(self):
        sel = self.tree.selection()
        if not sel:
            return None
        idx = self.tree.index(sel[0])
        return self.rows[idx] if 0 <= idx < len(self.rows) else None

    # ---------------------------------------------------------------- отрисовка
    def _reload(self, force=False):
        """Перечитывает индекс. Дерево перестраиваем только при изменениях,
        иначе выделение сбрасывалось бы каждые POLL_MS."""
        records = history_store.recent()
        snapshot = [(r.get('id'), r.get('status'), r.get('text')) for r in records]
        if not force and snapshot == self._snapshot:
            return
        self._snapshot = snapshot

        keep_id = (self._selected() or {}).get('id')
        self.tree.delete(*self.tree.get_children())
        self.rows = records
        for rec in records:
            text = (rec.get('text') or '').replace('\n', ' ')
            self.tree.insert('', 'end', values=(
                rec.get('ts', '').replace('T', ' ')[5:],
                MODE_TITLES.get(rec.get('mode'), rec.get('mode') or ''),
                f"{rec.get('seconds', 0):.1f} с",
                rec.get('status', ''),
                text[:120]))

        items = self.tree.get_children()
        if items:
            target = items[0]
            if keep_id:
                for item, rec in zip(items, records):
                    if rec.get('id') == keep_id:
                        target = item
                        break
            self.tree.selection_set(target)
            self._show_selected()

    def _show_selected(self):
        rec = self._selected()
        self.text.delete('1.0', 'end')
        if rec:
            self.text.insert('1.0', rec.get('text') or '')

    def _say(self, message, error=False):
        self.status.config(text=message, fg='#b71c1c' if error else '#555555')

    # ---------------------------------------------------------------- действия
    def _copy(self):
        rec = self._selected()
        if not rec:
            return
        text = self.text.get('1.0', 'end').strip() or (rec.get('text') or '')
        if not text:
            self._say('у записи нет текста', error=True)
            return
        pyperclip.copy(text)
        self._say('текст скопирован в буфер обмена')

    def _play(self):
        rec = self._selected()
        if not rec:
            return
        path = history_store.audio_file(rec)
        if not path.exists():
            self._say('файл записи не найден', error=True)
            return
        try:
            os.startfile(str(path))     # плеер по умолчанию
            self._say(f'открыт {path.name}')
        except Exception as e:
            logging.error(f"История: не удалось открыть {path}: {e}")
            self._say(f'не удалось открыть: {e}', error=True)

    def _recognize_again(self):
        rec = self._selected()
        if not rec or self.busy:
            return
        if self.recognize is None:
            self._say('модель ещё не загружена', error=True)
            return
        path = history_store.audio_file(rec)
        if not path.exists():
            self._say('файл записи не найден', error=True)
            return

        self.busy = True
        self.btn_again.config(state='disabled')
        self._say('распознаю заново…')

        rec_id = rec.get('id')

        def work():
            try:
                audio, _rate = history_store.read_audio(rec)
                text = self.recognize(audio)
                history_store.update(rec_id, text=text,
                                     status=history_store.STATUS_DONE if text
                                     else history_store.STATUS_NO_SPEECH)
                self.results.put((rec_id, text, None))
            except Exception as e:
                logging.error(f"История: повторное распознавание не удалось: {e}", exc_info=True)
                self.results.put((rec_id, None, str(e)))

        threading.Thread(target=work, name='history_recognize', daemon=True).start()

    # ---------------------------------------------------------------- таймер
    def _poll(self):
        if not self.alive():
            return
        try:
            while True:
                rec_id, text, error = self.results.get_nowait()
                self.busy = False
                self.btn_again.config(state='normal')
                if error:
                    self._say(f'ошибка распознавания: {error}', error=True)
                else:
                    self._say('распознано заново' if text else 'речи не найдено')
        except queue.Empty:
            pass

        self._reload()
        self._after_id = self.win.after(self.POLL_MS, self._poll)
