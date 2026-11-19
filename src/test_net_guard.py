"""Сторож прямых сетевых выходов: новый код ходит наружу только через src/net_gateway.py. Существующие прямые
выходы -- замороженный реестр config/net_direct_registry.json (файл -> сетевые библиотеки -> куда). Новый файл с
сетью или новая сетевая библиотека в файле -> красный тест; запись реестра без сети в файле -> тоже красный
(реестр точный)."""
import ast
import json
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
NET = {"requests", "httpx", "urllib.request", "urllib3", "http.client", "http.server", "socket", "socketserver", "ssl",
       "aiohttp", "telegram", "telethon", "websocket", "websockets", "ftplib", "smtplib", "imaplib", "poplib",
       "xmlrpc.client", "paho", "aiomqtt", "asyncio_mqtt", "tinytuya", "roborock", "garminconnect", "garth", "chromadb",
       "ollama", "openai", "anthropic", "curl_cffi", "playwright", "paramiko", "pywinrm", "bleak", "webbrowser",
       "zeroconf", "grpc", "gradio", "fastapi", "uvicorn", "wakeonlan"}


def scope_files() -> list[str]:
    try:
        out = subprocess.run(["git", "ls-files", "*.py"], cwd=ROOT, capture_output=True, text=True, timeout=60)
        files = out.stdout.split() if out.returncode == 0 and out.stdout.strip() else None
    except (OSError, subprocess.SubprocessError):
        files = None
    if files is None:                                     # снимок без git: файлы на диске
        files = [p.relative_to(ROOT).as_posix() for p in ROOT.rglob("*.py")]
    res = []
    for f in files:
        name = f.rsplit("/", 1)[-1]
        if name.startswith("test_") or name == "conftest.py" or "__pycache__" in f:
            continue
        top = f.split("/")[0] if "/" in f else ""
        if top == "" or top in ("src", "oko_rag") or (top == "tools" and not f.startswith("tools/archive/")):
            if (ROOT / f).is_file():
                res.append(f)
    return sorted(res)


def net_libs(path: Path) -> set:
    try:
        t = ast.parse(path.read_text(encoding="utf-8-sig"))
    except SyntaxError:
        return {"<syntax error>"}
    found = set()
    for n in ast.walk(t):
        mods = []
        if isinstance(n, ast.Import):
            mods = [a.name for a in n.names]
        elif isinstance(n, ast.ImportFrom) and n.module and n.level == 0:
            mods = [n.module] + [f"{n.module}.{a.name}" for a in n.names]
        elif isinstance(n, ast.Call) and getattr(n.func, "attr", getattr(n.func, "id", "")) in ("import_module", "__import__") \
                and n.args and isinstance(n.args[0], ast.Constant) and isinstance(n.args[0].value, str):
            mods = [n.args[0].value]
        for m in mods:
            for k in NET:
                if m == k or m.startswith(k + "."):
                    found.add(k)
    return found


def registry() -> dict:
    return json.loads((ROOT / "config" / "net_direct_registry.json").read_text(encoding="utf-8"))["entries"]


def test_no_new_direct_network_exits():
    reg = registry()
    actual = {f: sorted(net_libs(ROOT / f)) for f in scope_files()}
    actual = {f: v for f, v in actual.items() if v}
    new = {f: v for f, v in actual.items() if f not in reg}
    grown = {f: sorted(set(v) - set(reg[f]["libs"])) for f, v in actual.items() if f in reg and set(v) - set(reg[f]["libs"])}
    assert new == {}, f"новый прямой выход в сеть мимо шлюза: {new}"
    assert grown == {}, f"новая сетевая библиотека в файле реестра: {grown}"


def test_registry_is_exact_and_described():
    reg = registry()
    present = set(scope_files())
    stale = {f for f, e in reg.items() if f in present and not set(e["libs"]) <= net_libs(ROOT / f)}
    assert stale == set(), f"реестр не совпадает с кодом: {stale}"
    assert all(e.get("target") for e in reg.values())
    assert reg["src/net_gateway.py"]["target"].startswith("ШЛЮЗ")
    assert {"src/bot.py", "src/device_watch.py", "src/garmin_sync.py", "src/tv_control.py", "src/governor.py"} <= set(reg)


def test_guard_detects_new_exit(tmp_path):
    p = tmp_path / "x.py"
    p.write_text("import json\nfrom urllib import request\nimport importlib\nimportlib.import_module('httpx')\n",
                 encoding="utf-8")
    assert net_libs(p) == {"urllib.request", "httpx"}
    q = tmp_path / "y.py"
    q.write_text("import net_gateway\nimport urllib.parse\n", encoding="utf-8")
    assert net_libs(q) == set()


def test_fetch_tool_uses_only_gateway():
    assert net_libs(ROOT / "tools" / "net_fetch.py") == set()
    assert net_libs(ROOT / "src" / "net_sanitize.py") == set()


def test_fetched_documents_use_an_extension_the_watcher_sees():
    t = ast.parse((ROOT / "src" / "run_system.py").read_text(encoding="utf-8"))
    exts = next(ast.literal_eval(n.value) for n in t.body if isinstance(n, ast.Assign)
                and getattr(n.targets[0], "id", "") == "WATCHED_EXTENSIONS")
    src = (ROOT / "tools" / "net_fetch.py").read_text(encoding="utf-8")
    assert 'f"{name}.txt"' in src and ".txt" in exts
