"""StaticAnalyzer — эвристический фильтр первого уровня для патчей кода.

ЧЕСТНОЕ ОГРАНИЧЕНИЕ, не скрытое за формулировками: это не security boundary.
AST-анализ видит только литералы и структуру вызовов — он не отслеживает
поток данных (dataflow). open(some_variable) обходит любую проверку на
"запрещённые литералы пути", потому что переменная не является литералом
на этапе парсинга. Python — динамический язык: getattr, монкипатчинг,
композиция строк через f-string или конкатенацию — всё это легко обходит
статический анализ без выполнения кода.

Реальные границы защиты в этой системе — SandboxEnvironment (изоляция
выполнения), внешний manifest.sha256 (защита от самоослабления патчем —
см. security_tools/apply_patch.ps1, который сверяет хэши этого файла и
других инфраструктурных модулей ДО запуска PatchApplier, вне зоны,
которую PatchApplier способен патчить) и человек, читающий патч перед
approve (Task A — Human-in-the-Loop по определению, не автономия).
StaticAnalyzer отсекает грубые, случайные или наивные ошибки/попытки
(опечатка в виде eval(), забытый import os.system, ОБХОД ЧЕРЕЗ АЛИАС
ИМПОРТА вида `import subprocess as sp`) — не специально написанные атаки,
использующие полную динамику языка (getattr(builtins, 'ex'+'ec')),
которые тривиально обходят любую AST-проверку структуры вызова.

ИЗМЕНЕНИЕ (см. историю решений — аудит self-patching pipeline): добавлено
отслеживание import-биндингов через ImportTracker. Раньше проверка
`subprocess.run(...)` смотрела только на буквальное имя `subprocess` в
узле ast.Attribute — `import subprocess as sp; sp.run(...)` и
`from subprocess import run; run(...)` проходили анализ чисто, потому что
структурно не содержали строки "subprocess" в месте вызова. Подтверждено
тестом (test_static_analyzer.py::test_aliased_subprocess_bypass и
test_import_from_bypass) — оба сценария реально обходили предыдущую
версию. Тот же принцип применён к os.system/os.popen и аналогичным
опасным функциям os, ранее вообще не проверявшимся.
"""
from __future__ import annotations

import ast
import logger as oko_logger
from pathlib import Path
from typing import NamedTuple

logger = oko_logger.get_logger("kernel.static_analyzer")

# Функции, вызов которых запрещён безусловно — динамическое выполнение и
# динамический импорт. Эти случаи действительно детектируются надёжно:
# вызов eval(...)/exec(...)/__import__(...) как именованной функции —
# это структурный паттерн AST (ast.Call с ast.Name.id == "eval" и т.п.),
# не зависящий от значений переменных.
FORBIDDEN_CALLS = frozenset({
    "eval", "exec", "compile", "__import__",
})

# Модули, импорт функций из которых для динамического импорта запрещён.
FORBIDDEN_IMPORT_FUNCTIONS = frozenset({
    "import_module",  # importlib.import_module
})

# Файлы, где subprocess — ожидаемый, легитимный паттерн (уже одобренный
# код проекта), не нарушение. Список явный, не "везде кроме": расширять
# его — отдельное решение human-in-the-loop, не побочный эффект патча.
SUBPROCESS_ALLOWED_FILES = frozenset({
    "sandbox_environment.py",
    "knowledge_indexer.py",  # io_retry/shutil операции, не сам subprocess, но исторически близко по риску
})

# Файлы, где sys.path.append/insert — ожидаемый, легитимный паттерн
# (см. историю решений, аудит блока №3 — knowledge_indexer.py строка 43
# уже содержит sys.path.append(OKO_RAG_DIR) для доступа к oko_rag/loader.py,
# документированный, не случайный код). Раньше проверка sys.path-мутаций
# не имела whitelist вовсе, в отличие от SUBPROCESS_ALLOWED_FILES —
# асимметрия, из-за которой ЛЮБОЙ будущий легитимный патч на этот файл
# был бы гарантированно отклонён на этапе static analysis, даже не дойдя
# до содержательной проверки диффа. Список явный и отдельный от
# SUBPROCESS_ALLOWED_FILES: у файла может быть легитимная причина для
# одного паттерна и не быть для другого — не предполагаем, что "разрешён
# subprocess" автоматически означает "разрешена мутация sys.path".
SYS_PATH_ALLOWED_FILES = frozenset({
    "knowledge_indexer.py",
})

# Модуль -> опасные атрибуты этого модуля, независимо от способа импорта
# (import X / import X as Y / from X import Z). Расширяемо: добавление
# нового модуля сюда не требует правки логики визитора ниже.
DANGEROUS_MODULE_ATTRS: dict[str, frozenset[str]] = {
    "subprocess": frozenset({
        "run", "call", "check_call", "check_output", "Popen",
    }),
    "os": frozenset({
        "system", "popen", "exec", "execv", "execve", "execvp",
        "execvpe", "spawnv", "spawnve", "spawnl", "spawnle",
    }),
}


class AnalysisResult(NamedTuple):
    ok: bool
    violations: list[str]


class StaticAnalyzer:
    def analyze(self, source_code: str, filename: str = "<patch>") -> AnalysisResult:
        """Парсит source_code, возвращает AnalysisResult. ok=False, если
        найдена хотя бы одна запрещённая конструкция — violations содержит
        человеко-читаемое описание каждой находки, не только первой
        (вызывающий код видит полную картину, не итеративно перепатчивает
        по одному нарушению за раз).

        Если source_code не парсится вообще (SyntaxError) — это тоже
        violation, не отдельная категория исключения: патч с синтаксической
        ошибкой не должен молча проходить статический анализ как "пустой
        список нарушений = всё ок".
        """
        try:
            tree = ast.parse(source_code, filename=filename)
        except SyntaxError as e:
            return AnalysisResult(ok=False, violations=[f"SyntaxError: {e}"])

        # Первый проход: строим таблицу import-биндингов (локальное имя ->
        # реальный модуль/функция), ДО обхода вызовов — нужна полная карта
        # импортов файла независимо от порядка объявления относительно
        # использования (Python допускает импорт после первого использования
        # в некоторых конструкциях, например внутри функций).
        bindings = _ImportBindingCollector()
        bindings.visit(tree)

        visitor = _ForbiddenPatternVisitor(filename=filename, bindings=bindings.bindings)
        visitor.visit(tree)

        return AnalysisResult(ok=len(visitor.violations) == 0, violations=visitor.violations)

    def analyze_file(self, file_path: Path) -> AnalysisResult:
        """Удобство для случая, когда патч уже применён к файлу на диске
        (например, SnapshotManager уже сделал snapshot, патч записан, и
        перед прогоном тестов в SandboxEnvironment мы хотим проверить
        итоговый файл целиком, не только diff)."""
        path = Path(file_path)
        return self.analyze(path.read_text(encoding="utf-8"), filename=path.name)


class _ImportBindingCollector(ast.NodeVisitor):
    """Собирает отображение локальное_имя -> "модуль" или "модуль.функция"
    для всех import/from-import в файле. Не различает уровень вложенности
    (импорт внутри функции vs на верхнем уровне модуля) — намеренно:
    патч, оборачивающий опасный импорт в def, чтобы он не исполнился при
    статическом анализе, всё равно исполнится при реальном вызове функции,
    и мы хотим ловить сам факт импорта, не место его исполнения.
    """
    def __init__(self) -> None:
        self.bindings: dict[str, str] = {}

    def visit_Import(self, node: ast.Import) -> None:
        for alias in node.names:
            local_name = alias.asname or alias.name
            self.bindings[local_name] = alias.name
        self.generic_visit(node)

    def visit_ImportFrom(self, node: ast.ImportFrom) -> None:
        mod = node.module or ""
        for alias in node.names:
            local_name = alias.asname or alias.name
            self.bindings[local_name] = f"{mod}.{alias.name}"
        self.generic_visit(node)


class _ForbiddenPatternVisitor(ast.NodeVisitor):
    def __init__(self, filename: str, bindings: dict[str, str]) -> None:
        self.filename = filename
        self.bindings = bindings
        self.violations: list[str] = []

    def _is_subprocess_call(self, module_name: str) -> bool:
        return module_name == "subprocess" and self.filename not in SUBPROCESS_ALLOWED_FILES

    def visit_Call(self, node: ast.Call) -> None:
        # eval(...)/exec(...)/compile(...)/__import__(...) как простой вызов
        # по имени — ast.Name, не ast.Attribute (то есть НЕ перехватывает
        # something.eval(), только голый eval() в текущем скоупе — это
        # сознательное ограничение, расширение на атрибуты дало бы много
        # ложных срабатываний на легитимные методы с похожими именами).
        if isinstance(node.func, ast.Name) and node.func.id in FORBIDDEN_CALLS:
            self.violations.append(
                f"{self.filename}:{node.lineno}: forbidden call to {node.func.id}()"
            )

        # importlib.import_module(...) — атрибутный вызов, проверяем имя
        # атрибута, не require конкретный модуль (someobj.import_module()
        # технически не importlib, но это маловероятный ложноположительный
        # случай, и cost запрета его сильно ниже cost пропуска реального
        # importlib.import_module через переименованный импорт).
        if isinstance(node.func, ast.Attribute) and node.func.attr in FORBIDDEN_IMPORT_FUNCTIONS:
            self.violations.append(
                f"{self.filename}:{node.lineno}: forbidden dynamic import via .{node.func.attr}()"
            )

        # --- Случай А (было раньше): subprocess.* через ЛИТЕРАЛЬНОЕ имя
        # "subprocess" в узле вызова. Оставлено для читаемого сообщения об
        # ошибке в самом частом случае (обычный `import subprocess`), хотя
        # технически этот кейс уже покрывается веткой ниже через bindings
        # (import subprocess -> bindings["subprocess"] == "subprocess") —
        # проверка тут не даёт дублирующее сообщение благодаря "elif" ниже.
        is_literal_subprocess = (
            isinstance(node.func, ast.Attribute)
            and isinstance(node.func.value, ast.Name)
            and node.func.value.id == "subprocess"
        )
        if is_literal_subprocess and self._is_subprocess_call("subprocess"):
            self.violations.append(
                f"{self.filename}:{node.lineno}: subprocess.{node.func.attr}() not allowed "
                f"outside {sorted(SUBPROCESS_ALLOWED_FILES)}"
            )

        # --- Случай Б (НОВОЕ): вызов через ИМЯ, привязанное к опасному
        # модулю через import-таблицу — покрывает:
        #   import subprocess as sp; sp.run(...)          (Attribute call)
        #   from subprocess import run; run(...)           (Name call)
        #   import os as o; o.system(...)                  (Attribute call)
        #   from os import system; system(...)              (Name call)
        elif isinstance(node.func, ast.Attribute) and isinstance(node.func.value, ast.Name):
            base_name = node.func.value.id
            bound = self.bindings.get(base_name)
            if bound == "subprocess" and base_name != "subprocess" and self._is_subprocess_call(bound):
                self.violations.append(
                    f"{self.filename}:{node.lineno}: subprocess.{node.func.attr}() via aliased "
                    f"import ('{base_name}' -> subprocess) not allowed outside "
                    f"{sorted(SUBPROCESS_ALLOWED_FILES)}"
                )
            elif bound == "os" and node.func.attr in DANGEROUS_MODULE_ATTRS.get("os", frozenset()):
                self.violations.append(
                    f"{self.filename}:{node.lineno}: os.{node.func.attr}() via aliased import "
                    f"('{base_name}' -> os) is a dangerous system call"
                )

        if isinstance(node.func, ast.Name):
            bound = self.bindings.get(node.func.id, "")
            mod, _, attr = bound.rpartition(".")
            if mod == "subprocess" and attr in DANGEROUS_MODULE_ATTRS["subprocess"] \
                    and self._is_subprocess_call("subprocess"):
                self.violations.append(
                    f"{self.filename}:{node.lineno}: {node.func.id}() is subprocess.{attr} "
                    f"imported via 'from subprocess import {attr}' — not allowed outside "
                    f"{sorted(SUBPROCESS_ALLOWED_FILES)}"
                )
            elif mod == "os" and attr in DANGEROUS_MODULE_ATTRS["os"]:
                self.violations.append(
                    f"{self.filename}:{node.lineno}: {node.func.id}() is os.{attr} imported via "
                    f"'from os import {attr}' — dangerous system call"
                )

        # sys.path.append(...)/.insert(...)/.remove(...) — МУТАЦИЯ sys.path
        # через вызов метода. Это и есть реальная угроза (манипуляция
        # разрешением импортов), не просто чтение sys.path (см. visit_Attribute
        # ниже — там раньше было ложное срабатывание на ЛЮБОЕ упоминание
        # sys.path, включая чтение типа print(sys.path); найдено тестом,
        # исправлено: теперь проверка специфична к вызову мутирующих методов).
        #
        # Whitelist SYS_PATH_ALLOWED_FILES (см. историю решений, аудит
        # блока №3) — та же модель, что уже применена для subprocess:
        # блокировка безусловна везде, кроме явно перечисленных файлов,
        # где паттерн уже документирован как легитимный.
        if (
            isinstance(node.func, ast.Attribute)
            and isinstance(node.func.value, ast.Attribute)
            and isinstance(node.func.value.value, ast.Name)
            and node.func.value.value.id == "sys"
            and node.func.value.attr == "path"
            and node.func.attr in {"append", "insert", "remove", "extend", "pop", "clear"}
            and self.filename not in SYS_PATH_ALLOWED_FILES
        ):
            self.violations.append(
                f"{self.filename}:{node.lineno}: sys.path.{node.func.attr}() — "
                f"mutating import resolution is forbidden in patches outside "
                f"{sorted(SYS_PATH_ALLOWED_FILES)}"
            )

        self.generic_visit(node)

    def visit_Assign(self, node: ast.Assign) -> None:
        # sys.path = [...] — прямое переприсваивание всего списка путей.
        # Отдельная ветка от visit_Call, потому что это не вызов метода,
        # это присвоение значения атрибуту. Тот же whitelist применяется
        # для консистентности, хотя знаемый легитимный паттерн в проекте
        # использует только .append(), не полное переприсваивание.
        for target in node.targets:
            if (
                isinstance(target, ast.Attribute)
                and isinstance(target.value, ast.Name)
                and target.value.id == "sys"
                and target.attr == "path"
                and self.filename not in SYS_PATH_ALLOWED_FILES
            ):
                self.violations.append(
                    f"{self.filename}:{node.lineno}: sys.path = ... — "
                    f"reassigning import resolution is forbidden in patches outside "
                    f"{sorted(SYS_PATH_ALLOWED_FILES)}"
                )
        self.generic_visit(node)

    def visit_Import(self, node: ast.Import) -> None:
        for alias in node.names:
            if alias.name == "importlib":
                self.violations.append(
                    f"{self.filename}:{node.lineno}: import of importlib "
                    f"(dynamic import capability) is forbidden"
                )
        self.generic_visit(node)
