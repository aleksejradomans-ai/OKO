# Око — локальный ассистент на Ollama с Telegram-интерфейсом

Личный проект: ассистент, который целиком работает на домашнем ПК под Windows.
Генерация и эмбеддинги — через локальную Ollama, интерфейс — Telegram-бот,
память и база знаний — SQLite и ChromaDB. Облачных API нет.

Проект однопользовательский: бот отвечает только ID из `ADMIN_USER_IDS`.

## Что умеет

- **Диалог с памятью.** Журнал переписки в `data/memory/memory.db`
  (ключ — локальный `subject_id`, а не Telegram ID). `/memory` — что хранится,
  `/forget` → `/forget confirm` — полное удаление.
- **База знаний с ручной модерацией.** Файл в `data/knowledge/` → карантин
  (`staging/`) → кнопки Approve/Reject в Telegram с проверкой хэша → индексация
  в SQLite и ChromaDB. `/discuss <файл>` — разговор по одному документу целиком.
- **Судья ответов.** Каждый ответ проверяется вторым вызовом той же модели
  (`temperature=0`) по двум правилам из `src/manifesto.py`. FAIL → одна
  попытка коррекции, HALT → блокировка. Нераспознанный вердикт и
  неустранённое нарушение не отдаются пользователю.
- **Отказоустойчивость.** Транспортные сбои Ollama не запускают повторных
  вызовов модели (F1); предохранитель `CircuitBreaker` отклоняет запросы при
  массовых нарушениях; 12 служебных кодов `[ERR: …]` с фиксированным уровнем
  лога («светофор»), см. `docs/kernel_codes.txt`.
- **Игровой режим.** `game_watcher.ps1`: при запуске игры из `games.txt`
  останавливает бота и выгружает модель из VRAM, после закрытия — поднимает.

## Архитектура

```
Telegram ──► src/bot.py ──► DataHygieneFilter ──► AgentGovernor ──► Ollama
                │   (allowlist,        (маскирует        │  (судья, коррекция,
                │    /memory, /forget)  ключи/токены)    │   CircuitBreaker)
                ├── MemoryCore (data/memory/memory.db)   └── RAG: oko_rag/ (ChromaDB)
                └── staging: кнопки Approve/Reject
data/knowledge/ ──► src/run_system.py (watcher) ──► staging ──► knowledge_indexer
game_watcher.ps1 ──► stop/start OkoBot, unload модели из VRAM
NSSM: службы OkoBot и OkoWatcher
```

Решения и их причины — в `docs/DECISIONS.md` (ADR-001 и далее).

## Требования

- Windows 10/11 (Linux-юниты в `deploy/` есть, но основной деплой — Windows + NSSM)
- Python 3.11+ (проверено на 3.12 и 3.13)
- [Ollama](https://ollama.com) с моделью генерации и моделью эмбеддингов
  (имена задаются в `.env`, умолчания — в `src/config.py` и `oko_rag/rag_config.py`)
- Для OCR в PDF: [Tesseract](https://github.com/tesseract-ocr/tesseract)
- Для служб Windows: [NSSM](https://nssm.cc)

## Установка

```powershell
git clone <repo> oko; cd oko
python -m venv .venv; .\.venv\Scripts\Activate.ps1
pip install -r requirements.txt -r oko_rag\requirements.txt
ollama pull gemma2
Copy-Item .env.example .env      # заполнить TELEGRAM_TOKEN и ADMIN_USER_IDS
```

Запуск вручную (из корня репозитория):

```powershell
$env:PYTHONPATH = (Get-Location).Path
python src\bot.py          # бот
python src\run_system.py   # наблюдатель за data\knowledge
```

Как службы (PowerShell от администратора, `nssm.exe` в корне репозитория):

```powershell
.\setup_service.ps1 -PythonExe C:\path\to\python.exe
```

## Тесты

```powershell
python -m pytest src -q
```

Тесты не обращаются к Ollama и Telegram и не читают `.env`: `src/conftest.py`
подставляет временный `AI_BASE_DIR`, фиктивный токен и пустой список админов.
`test_enforce_compliance_live.py` и `scripts/` — ручные проверки на живой Ollama.

## Известные ограничения

- Судья — эвристика на той же малой модели; возможны ложные HALT/FAIL.
  Если судья сам упал не по транспорту (битый ответ Ollama), ответ уходит
  непроверенным (`UNCHECKED`).
- Индекс памяти пока `_NullIndex`: в контекст попадают только последние 10 сообщений.
- Весь ввод проходит через серверы Telegram до любых локальных фильтров.
- `Registry` — JSON-файл без межпроцессной блокировки (staging уже в SQLite).
- Модель склонна к непрошенным советам; судья их ловит не всегда.

## Лицензия

MIT, см. файл `LICENSE`.
