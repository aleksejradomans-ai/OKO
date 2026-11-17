"""Сверка .env-переключателей: каждое getattr(config, "X", ...) в коде должно
иметь атрибут X в config.py. Иначе переключатель мёртв (молча работает
дефолт getattr), а остальные тесты зелёные. Только ast/regex, без импорта
config (тот требует TELEGRAM_TOKEN)."""
import ast
import re
from pathlib import Path

SRC = Path(__file__).resolve().parent
_GETATTR = re.compile(r"""getattr\(\s*config\s*,\s*["'](\w+)["']""")
CONSUMERS = ("governor.py", "bot.py")


def _config_names() -> set:
    tree = ast.parse((SRC / "config.py").read_text(encoding="utf-8-sig"))
    names = set()
    for node in tree.body:
        if isinstance(node, ast.Assign):
            names |= {t.id for t in node.targets if isinstance(t, ast.Name)}
        elif isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
            names.add(node.target.id)
    return names


def _getattr_names(fname: str) -> set:
    return set(_GETATTR.findall((SRC / fname).read_text(encoding="utf-8-sig")))


def test_every_getattr_config_name_defined():
    defined = _config_names()
    missing = {f: sorted(_getattr_names(f) - defined) for f in CONSUMERS}
    assert not any(missing.values()), f"нет в config.py: {missing}"


def test_depth_ctx_switches_present():
    defined = _config_names()
    for name in ("OKO_CTX_LOG", "OKO_RUNTIME_NUM_CTX", "OKO_DEPTH_PAUSE_S"):
        assert name in defined
