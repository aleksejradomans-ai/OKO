"""secrets_paths: выбор пути файла секретов (закрытая папка secrets/ или старое место), приоритет переменных
окружения модулей; атомарная запись в закрытой папке не открывает права (Windows, иначе пропуск). Временные папки."""
import codecs
import json
import locale
import os
import subprocess
import sys

import pytest

import secrets_paths as sp


def test_rules_order(tmp_path):
    name = ".x_tokens.json"
    assert sp.secret_path(name, tmp_path) == tmp_path / name                      # ни папки, ни файла -> старое
    (tmp_path / name).write_text("{}", encoding="utf-8")
    (tmp_path / "secrets").mkdir()
    assert sp.secret_path(name, tmp_path) == tmp_path / name                      # файл ещё на старом месте
    (tmp_path / "secrets" / name).write_text("{}", encoding="utf-8")
    assert sp.secret_path(name, tmp_path) == tmp_path / "secrets" / name          # перенесён -> новое
    (tmp_path / name).unlink()
    assert sp.secret_path(".new.json", tmp_path) == tmp_path / "secrets" / ".new.json"   # новый файл -- в папку


def test_default_base_is_config_base_dir():
    import config
    assert sp.base_dir() == config.BASE_DIR and sp.secrets_dir() == config.BASE_DIR / "secrets"


def test_smartthings_env_first_then_secrets(tmp_path, monkeypatch):
    import config
    import smartthings_oauth as o
    monkeypatch.setattr(config, "BASE_DIR", tmp_path)
    monkeypatch.delenv("OKO_ST_TOKENS", raising=False)
    assert o.token_path() == tmp_path / ".smartthings_tokens.json"
    (tmp_path / "secrets").mkdir()
    assert o.token_path() == tmp_path / "secrets" / ".smartthings_tokens.json"
    monkeypatch.setenv("OKO_ST_TOKENS", str(tmp_path / "explicit.json"))
    assert o.token_path() == tmp_path / "explicit.json"


def test_smartthings_refresh_writes_where_read(tmp_path, monkeypatch):
    import config
    import smartthings_oauth as o
    monkeypatch.setattr(config, "BASE_DIR", tmp_path)
    monkeypatch.delenv("OKO_ST_TOKENS", raising=False)
    (tmp_path / "secrets").mkdir()
    p = tmp_path / "secrets" / ".smartthings_tokens.json"
    p.write_text(json.dumps({"client_id": "c", "client_secret": "s", "access_token": "a", "refresh_token": "r",
                             "expires_at": 0}), encoding="utf-8")

    def post(url, form, basic):
        return 200, {"access_token": "a2", "refresh_token": "r2", "expires_in": 86400}
    assert o.get_token(post=post, now=1000.0) == "a2"
    assert json.loads(p.read_text(encoding="utf-8"))["refresh_token"] == "r2"
    assert not (tmp_path / ".smartthings_tokens.json").exists()


def test_garmin_env_first_then_secrets(tmp_path, monkeypatch):
    import garmin_sync as g
    monkeypatch.setattr(g, "_base_dir", lambda: tmp_path)
    monkeypatch.delenv("OKO_GARMIN_TOKENS", raising=False)
    assert g.tokens_path() == tmp_path / ".garmin_tokens.json"
    (tmp_path / "secrets").mkdir()
    assert g.tokens_path() == tmp_path / "secrets" / ".garmin_tokens.json"
    monkeypatch.setenv("OKO_GARMIN_TOKENS", str(tmp_path / "g.json"))
    assert g.tokens_path() == tmp_path / "g.json"


BROAD = {"S-1-5-32-545", "S-1-5-11", "S-1-1-0", "S-1-5-4", "S-1-2-0", "S-1-2-1"}   # Users, Authenticated, Everyone,
#   INTERACTIVE, LOCAL, CONSOLE LOGON; Administrators / OWNER RIGHTS / SYSTEM допустимы
OWNER_PLACEHOLDERS = {"S-1-3-0", "S-1-3-1"}     # CREATOR OWNER / GROUP: на файле заменяются владельцем (оператор / Админы)


def acl_problems(dir_sids, file_sids, me):
    """Пустой список = закрыто: ни у папки, ни у файла нет широких групп, и у файла нет SID сверх ACL папки
    (владелец-создатель папки на файле превращается в текущего пользователя -- допустимо)."""
    allowed = set(dir_sids) | ({me, "S-1-5-32-544"} if set(dir_sids) & OWNER_PLACEHOLDERS else set())
    p = []
    if not file_sids:
        p.append("file ACL empty / unreadable")
    if set(dir_sids) & BROAD:
        p.append(f"dir has broad groups {sorted(set(dir_sids) & BROAD)}")
    if set(file_sids) & BROAD:
        p.append(f"file has broad groups {sorted(set(file_sids) & BROAD)}")
    extra = set(file_sids) - allowed
    if extra:
        p.append(f"file has SIDs beyond the dir ACL {sorted(extra)}")
    return p


def test_acl_compare_logic():
    me = "S-1-5-21-1-2-3-1001"
    d = {"S-1-5-18", "S-1-5-32-544", "S-1-3-4", me}
    assert acl_problems(d, {"S-1-5-18", "S-1-5-32-544", me}, me) == []          # Админы / OWNER RIGHTS допустимы
    assert acl_problems(d | {"S-1-3-0"}, {"S-1-5-18", me}, me) == []
    assert "file has SIDs beyond the dir ACL" in acl_problems(d, {"S-1-5-18", "S-1-5-21-9-9-9-500"}, me)[0]
    assert acl_problems(d, {"S-1-5-18", "S-1-5-32-545"}, me)[0].startswith("file has broad")
    assert acl_problems(d | {"S-1-5-4"}, {"S-1-5-18"}, me)[0].startswith("dir has broad")
    assert acl_problems(d, set(), me) == ["file ACL empty / unreadable"]


def _console_text(raw: bytes) -> str:
    """Вывод консольных утилит Windows (powershell / icacls) -- в OEM-кодировке консоли, а не в кодировке процесса
    (на ru-ПК совпадало, на раннере cp1252 -- нет: UnicodeDecodeError в потоке чтения, пустой ACL). Байты -> текст,
    недекодируемое -> U+FFFD. SID -- ASCII; строка с мусором остаётся в наборе, поэтому проверка ACL не слабеет."""
    for enc in ("oem", locale.getpreferredencoding(False), "utf-8"):
        try:
            codecs.lookup(enc)
        except LookupError:          # "oem" есть только в Python для Windows
            continue
        return raw.decode(enc, errors="replace")
    return raw.decode("ascii", errors="replace")


def _ps_env():
    """Окружение дочернего Windows PowerShell 5.1 без PSModulePath родителя: pwsh 7 подставляет туда свои пути, и 5.1
    не грузит Microsoft.PowerShell.Security / Utility (Get-Acl, Get-FileHash). Без переменной 5.1 берёт свои пути."""
    return {k: v for k, v in os.environ.items() if k.upper() != "PSMODULEPATH"}


def _powershell(cmd):
    r = subprocess.run(["powershell", "-NoProfile", "-Command", cmd], capture_output=True, timeout=60, env=_ps_env())
    return _console_text(r.stdout)


def _sids_from_text(text):
    return {x.strip() for x in text.splitlines() if x.strip()}


def _sids(path):
    cmd = ("(Get-Acl -LiteralPath '%s').Access | ForEach-Object { "
           "$_.IdentityReference.Translate([Security.Principal.SecurityIdentifier]).Value }" % str(path))
    return _sids_from_text(_powershell(cmd))


def _me():
    return _powershell("[Security.Principal.WindowsIdentity]::GetCurrent().User.Value").strip()


def test_console_text_non_ascii_byte_does_not_break_sids():
    """Регрессия CI (раннер cp1252): байт 0x81 в выводе не роняет чтение; SID-строки целы, мусор не теряется."""
    raw = b"S-1-5-18\r\n\x81\xfe \xc4\xee\xf1\xf2\xf3\xef\r\nS-1-5-32-544\r\n\r\n"
    sids = _sids_from_text(_console_text(raw))
    assert {"S-1-5-18", "S-1-5-32-544"} <= sids and len(sids) == 3
    me = "S-1-5-21-1-2-3-1001"
    assert any(x.startswith("file has SIDs beyond") for x in acl_problems({"S-1-5-18", "S-1-5-32-544"}, sids, me))
    assert acl_problems({"S-1-5-18"}, _sids_from_text(_console_text(b"\x81\r\n")), me) != []
    assert _sids_from_text(_console_text(b"")) == set() and acl_problems({"S-1-5-18"}, set(), me) == [
        "file ACL empty / unreadable"]


def test_ps_env_drops_parent_psmodulepath(monkeypatch):
    monkeypatch.setenv("PSModulePath", "C:\\pwsh7\\Modules")
    monkeypatch.setenv("OKO_PS_ENV_PROBE", "1")
    e = _ps_env()
    assert not any(k.upper() == "PSMODULEPATH" for k in e) and e.get("OKO_PS_ENV_PROBE") == "1"


@pytest.mark.skipif(sys.platform != "win32", reason="ACL Windows: проверка наследования прав папки только на Windows")
def test_atomic_write_in_closed_dir_keeps_closed_acl(tmp_path):
    import smartthings_oauth as o
    d = tmp_path / "secrets"
    d.mkdir()
    me = _me()
    r = subprocess.run(["icacls", str(d), "/inheritance:r", "/grant:r", "*S-1-5-18:(OI)(CI)F",
                        f"*{me}:(OI)(CI)F"], capture_output=True, timeout=60)
    assert r.returncode == 0
    subprocess.run(["icacls", str(d), "/remove:g"] + [f"*{x}" for x in sorted(BROAD)], capture_output=True, timeout=60)
    p = d / ".smartthings_tokens.json"
    o._save(p, {"access_token": "x"})                          # temp + os.replace, как при обновлении токена
    o._save(p, {"access_token": "y"})
    assert acl_problems(_sids(d), _sids(p), me) == []          # SID файла -- из ACL папки, без широких групп
