from faster_whisper.utils import get_assets_path
import ctranslate2

print("=== Проверка окружения ===")
print(f"Версия CTranslate2: {ctranslate2.__version__}")

# Проверяем, скомпилирован ли движок с поддержкой CUDA
supported_compute_types = ctranslate2.get_supported_compute_types("cuda")

if supported_compute_types:
    print("\n✅ УРА! CUDA доступна для faster-whisper.")
    print(f"Поддерживаемые типы вычислений на вашей видеокарте: {supported_compute_types}")
    
    if "float16" in supported_compute_types:
        print("✅ float16 поддерживается (идеально для RTX 3090).")
    if "int8" in supported_compute_types:
        print("✅ int8 поддерживается.")
else:
    print("\n❌ ОШИБКА: CUDA не обнаружена или CTranslate2 установлен без поддержки GPU.")
    print("Модель будет работать на процессоре (CPU), что очень медленно.")