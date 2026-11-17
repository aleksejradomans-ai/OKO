"""secrets_paths.py -- где лежат файлы секретов (токены облаков, пара ТВ): закрытая папка <BASE_DIR>/secrets/.

secret_path(name, base_dir=None):
  1. файл уже в <base>/secrets/<name> -> он;
  2. иначе файл по старому месту <base>/<name> -> он (переходный период: служба со старым расположением не ломается);
  3. иначе, если папка <base>/secrets/ существует -> <base>/secrets/<name> (новый файл создаётся в закрытой папке);
  4. иначе -> <base>/<name>.
Явные переменные окружения конкретного модуля (OKO_ST_TOKENS, OKO_GARMIN_TOKENS) проверяются в самих модулях
раньше этой функции. Запись (обновление токенов) идёт туда же, откуда прочитано: атомарная запись через временный
файл в той же папке наследует права папки, поэтому файл в закрытой папке остаётся закрытым.
Только стандартная библиотека; base_dir по умолчанию -- config.BASE_DIR."""
from __future__ import annotations

from pathlib import Path
from typing import Optional

SECRETS_DIRNAME = "secrets"


def base_dir() -> Path:
    try:
        import config
        return Path(config.BASE_DIR)
    except Exception:  # noqa: BLE001 -- вне службы (ручной запуск без .env)
        return Path(__file__).resolve().parent.parent / "data"


def secrets_dir(base: Optional[Path] = None) -> Path:
    return Path(base if base is not None else base_dir()) / SECRETS_DIRNAME


def secret_path(name: str, base: Optional[Path] = None) -> Path:
    b = Path(base if base is not None else base_dir())
    new = b / SECRETS_DIRNAME / name
    old = b / name
    if new.is_file():
        return new
    if old.is_file():
        return old
    if new.parent.is_dir():
        return new
    return old
