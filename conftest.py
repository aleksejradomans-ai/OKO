import sys
import os

# --- Защита боевых данных ---
# Тесты НИКОГДА не должны писать в рабочий BASE_DIR (и тем более удалять файлы
# в нём). config.py вызывает load_dotenv(), который не перезаписывает уже
# заданные переменные, а conftest загружается раньше любого тестового модуля.
import os as _guard_os, tempfile as _guard_tempfile
_guard_os.environ['AI_BASE_DIR'] = _guard_tempfile.mkdtemp(prefix='oko_pytest_')


# Принудительно добавляем текущую директорию (C:\oko\) в список мест, 
# где Python ищет модули. Теперь import src.xxx будет работать всегда.
sys.path.insert(0, os.path.abspath(os.getcwd()))