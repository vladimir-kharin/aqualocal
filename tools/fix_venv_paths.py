"""
Починка venv после переноса каталога проекта.

Что на самом деле ломается. При переносе venv портятся НЕ все файлы, а только
те, куда абсолютный путь запечён на этапе установки:

  pyvenv.cfg          -> home/base-* указывают на БАЗОВЫЙ Python, а не на проект.
                         Если базовый Python не переезжал — тут всё цело.
  Scripts\\python.exe   -> находит себя сам, по собственному расположению.
                         Поэтому start.bat и приложение работают как ни в чём не бывало.
  Scripts\\*.exe        -> ЛОМАЮТСЯ. Это обёртки distlib: PE-заглушка, следом строка
                         "#!<путь к python.exe>", следом zip со скриптом. Путь внутри
                         старый — отсюда "Fatal error in launcher".
  activate*           -> в них зашит VIRTUAL_ENV. Ломается промпт и PATH,
                         но не сам интерпретатор.
  site-packages\\*.pth  -> редко, но бывает (editable-установки).

Скрипт правит .exe и activate*, проверяет pyvenv.cfg и сообщает о .pth.

Смена длины пути безопасна: приписанный к .exe zip находится по сканированию
с конца, поэтому сдвиг префикса он переживает (проверено).

Запуск (сначала всегда без --apply — посмотреть, что будет):
    .venv\\Scripts\\python.exe tools\\fix_venv_paths.py
    .venv\\Scripts\\python.exe tools\\fix_venv_paths.py --apply
    .venv\\Scripts\\python.exe tools\\fix_venv_paths.py --apply --backup
    .venv\\Scripts\\python.exe tools\\fix_venv_paths.py --venv D:\\other\\.venv --apply
"""

import argparse
import os
import sys
from pathlib import Path

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass


# ---------------------------------------------------------------- .exe обёртки
def read_shebang(data: bytes):
    """-> (start, end, shebang_bytes) или None. Шебанг лежит прямо перед zip."""
    zip_at = data.rfind(b"PK\x03\x04")
    if zip_at < 0:
        return None
    head = data[:zip_at]
    start = head.rfind(b"#!")
    if start < 0:
        return None
    end = head.find(b"\r\n", start)
    if end < 0:
        end = head.find(b"\n", start)
    if end < 0 or end > zip_at:
        return None
    return start, end, head[start:end]


def shebang_path(shebang: bytes) -> str:
    return shebang[2:].decode("utf-8", "replace").strip().strip('"')


def patch_exe(path: Path, new_python: str, apply: bool, backup: bool):
    data = path.read_bytes()
    found = read_shebang(data)
    if not found:
        return "не обёртка", None
    start, end, shebang = found
    old = shebang_path(shebang)
    if os.path.normcase(old) == os.path.normcase(new_python):
        return "уже верный", old

    quoted = shebang.startswith(b'#!"')
    nb = new_python.encode("utf-8")
    new_shebang = (b'#!"' + nb + b'"') if quoted else (b"#!" + nb)

    if apply:
        if backup:
            path.with_suffix(path.suffix + ".bak").write_bytes(data)
        path.write_bytes(data[:start] + new_shebang + data[end:])
    return "ИСПРАВЛЕНО" if apply else "будет исправлен", old


# ---------------------------------------------------------------- activate
ACTIVATE_FILES = ["activate", "activate.bat", "activate.ps1", "activate.fish",
                  "activate.nu", "activate.csh", "activate_this.py"]


def patch_text(path: Path, old_root: str, new_root: str, apply: bool):
    """Регистронезависимая замена old_root -> new_root. -> число вхождений."""
    try:
        text = path.read_text(encoding="utf-8")
    except Exception:
        return None
    low, low_old = text.lower(), old_root.lower()
    if low_old not in low:
        return None
    out, i = [], 0
    while True:
        j = low.find(low_old, i)
        if j < 0:
            out.append(text[i:])
            break
        out.append(text[i:j])
        out.append(new_root)
        i = j + len(old_root)
    if apply:
        path.write_text("".join(out), encoding="utf-8")
    return low.count(low_old)


# ---------------------------------------------------------------- main
def main():
    ap = argparse.ArgumentParser(description="Починка путей внутри venv после переноса")
    ap.add_argument("--venv", default=".venv", help="путь к venv (по умолчанию .venv)")
    ap.add_argument("--apply", action="store_true", help="записать изменения")
    ap.add_argument("--backup", action="store_true", help="сохранять .exe.bak")
    args = ap.parse_args()

    venv = Path(args.venv).resolve()
    scripts = venv / ("Scripts" if os.name == "nt" else "bin")
    if not scripts.is_dir():
        sys.exit(f"Не найден каталог {scripts}")

    new_python = str(scripts / ("python.exe" if os.name == "nt" else "python"))
    print(f"venv           : {venv}")
    print(f"целевой python : {new_python}")
    print(f"режим          : {'ЗАПИСЬ' if args.apply else 'просмотр (добавь --apply)'}\n")

    # --- pyvenv.cfg: жив ли базовый Python
    cfg = venv / "pyvenv.cfg"
    if cfg.exists():
        print("pyvenv.cfg:")
        for line in cfg.read_text(encoding="utf-8").splitlines():
            if line.split("=")[0].strip() in ("home", "base-prefix", "base-exec-prefix",
                                              "base-executable"):
                val = line.split("=", 1)[1].strip()
                ok = Path(val).exists()
                print(f"  {'OK ' if ok else '!!! НЕТ'}  {line.strip()}")
                if not ok:
                    print("       -> базовый Python переехал или удалён, venv придётся пересобрать")
        print()

    # --- .exe обёртки
    exes = sorted(scripts.glob("*.exe"))
    print(f".exe обёрток: {len(exes)}")
    old_root, fixed = None, 0
    for exe in exes:
        status, old = patch_exe(exe, new_python, args.apply, args.backup)
        if status in ("уже верный", "не обёртка"):
            continue
        fixed += 1
        if old_root is None and old:
            old_root = str(Path(old).parent.parent)  # ...\Scripts\python.exe -> корень venv
        print(f"  {status:<18} {exe.name:<28} было: {old}")
    if not fixed:
        print("  всё уже указывает куда надо")
    print()

    if old_root:
        print(f"старый корень venv: {old_root}\n")
        print("activate-скрипты:")
        touched = False
        for name in ACTIVATE_FILES:
            p = scripts / name
            if not p.exists():
                continue
            n = patch_text(p, old_root, str(venv), args.apply)
            if n:
                touched = True
                print(f"  {'ИСПРАВЛЕНО' if args.apply else 'будет исправлен'}  {name} ({n} вхожд.)")
        if not touched:
            print("  чисто")
        print()

        sp = venv / ("Lib/site-packages" if os.name == "nt" else "lib")
        stale = []
        if sp.exists():
            for p in list(sp.rglob("*.pth")) + list(sp.rglob("__editable__*")):
                try:
                    if old_root.lower() in p.read_text(encoding="utf-8", errors="ignore").lower():
                        stale.append(p)
                except Exception:
                    pass
        print("site-packages (.pth / editable):")
        if stale:
            for p in stale:
                print(f"  !!! ссылка на старый путь: {p}")
            print("      правится только переустановкой соответствующего пакета")
        else:
            print("  чисто")
        print()

    print("Готово. Проверь:  .venv\\Scripts\\pip.exe --version" if args.apply
          else "Ничего не записано. Повтори с --apply.")


if __name__ == "__main__":
    main()
