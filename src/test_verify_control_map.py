"""Тесты верификатора карты доказуемости (tools/verify_control_map.py, B32).

На каждый вид FAIL -- негативный тест на поддельных csv / JUnit во временной папке: недостающий и лишний ID, нет файла,
нет символа, SKIP, XFAIL, XPASS (метка xfail при «прошёл» в отчёте и сообщение strict), нет в отчёте, PASS только на
одной платформе, плохой evidence, пустой scope, устаревшая карта, расхождение кодов ядра, source-scan под видом behavior.
Тесты-лжецы: мутанты верификатора «XFAIL = PASS», «одного отчёта достаточно», «SKIP без known_gap = GAP» и «чтение
исходника не проверяется» обязаны быть пойманы теми же сценариями.
Последний блок -- настоящая карта репозитория: структура, символы, коды ядра и docs/CONTROL_MAP.md (по синтетическому
отчёту «все тесты карты прошли на обеих платформах»)."""
from __future__ import annotations

import csv
import importlib.util
import io
import sys
from pathlib import Path
from xml.sax.saxutils import quoteattr

import pytest

ROOT = Path(__file__).resolve().parent.parent
TOOL = ROOT / "tools" / "verify_control_map.py"


def _load(name: str, text: str | None = None):
    spec = importlib.util.spec_from_loader(name, loader=None)
    mod = importlib.util.module_from_spec(spec)
    code = TOOL.read_text(encoding="utf-8") if text is None else text
    exec(compile(code, str(TOOL), "exec"), mod.__dict__)
    return mod


V = _load("verify_control_map_b32")

INVARIANTS = """# Инварианты
- **INV-01 Первое (01.01.2026).** Текст.
- **INV-02 Второе (01.01.2026).** Текст.

| ID | зона | Зачем это владельцу / Не менять без [1] |
|---|---|---|
| INV-01 | красная | — |
| INV-02 | рабочая | — |
"""
GOVERNOR = """YELLOW_CODES = frozenset({"AAA_ONE"})
RED_CODES = frozenset({"BBB_TWO"})
SPLIT_CODES = frozenset({"CCC_MIX"})
"""
KERNEL = """Всего 3 кодов.
Жёлтые коды (WARNING): AAA_ONE.
Красные коды (ERROR): BBB_TWO.
Смешанный код: CCC_MIX — жёлтый при транспорте.
[ERR: AAA_ONE] a. [ERR: BBB_TWO] b. [ERR: CCC_MIX] c.
"""
MOD = """class Gate:
    def before(self):
        return 1


def decide():
    return 0
"""
TESTS = """import pytest


def test_a():
    assert True


def test_b():
    assert True


@pytest.mark.xfail(reason="known")
def test_x():
    assert True


def test_scan():
    from pathlib import Path
    assert "def" in (Path(__file__).parent / "mod.py").read_text()


class TestK:
    def test_m(self):
        assert True
"""
PLAIN = "id,breaks\nINV-01,Сломается первое.\nINV-02,Сломается второе.\n"
HEADER = "id,implementation,tests,evidence,scope,known_gap"
ROW1 = "INV-01,src/mod.py::Gate.before,src/test_mod.py::test_a;src/test_mod.py::TestK::test_m,behavior,путь A,"
ROW2 = "INV-02,src/mod.py::decide,src/test_mod.py::test_b,integration,путь B,"


def _junit(path: Path, cases: dict) -> Path:
    """cases: {(classname, name): PASS|FAIL|ERROR|SKIP|XFAIL|XPASS_STRICT}."""
    body = []
    for (cls, name), st in cases.items():
        inner = {"PASS": "", "FAIL": '<failure message="boom">x</failure>', "ERROR": '<error message="e">x</error>',
                 "SKIP": '<skipped type="pytest.skip" message="nope">x</skipped>',
                 "XFAIL": '<skipped type="pytest.xfail" message="known" />',
                 "XPASS_STRICT": '<skipped message="xfail-marked test passes unexpectedly" />'}[st]
        body.append(f'<testcase classname={quoteattr(cls)} name={quoteattr(name)} time="0.01">{inner}</testcase>')
    path.write_text('<?xml version="1.0" encoding="utf-8"?><testsuites><testsuite name="pytest">' + "".join(body) +
                    "</testsuite></testsuites>", encoding="utf-8")
    return path


ALL_PASS = {("src.test_mod", "test_a"): "PASS", ("src.test_mod", "test_b"): "PASS",
            ("src.test_mod.TestK", "test_m"): "PASS", ("src.test_mod", "test_x"): "XFAIL",
            ("src.test_mod", "test_scan"): "PASS"}


@pytest.fixture
def repo(tmp_path):
    (tmp_path / "docs").mkdir()
    (tmp_path / "src").mkdir()
    (tmp_path / ".github").mkdir()
    (tmp_path / "docs" / "INVARIANTS.md").write_text(INVARIANTS, encoding="utf-8")
    (tmp_path / "docs" / "control_map_plain.csv").write_text(PLAIN, encoding="utf-8")
    (tmp_path / "docs" / "kernel_codes.txt").write_text(KERNEL, encoding="utf-8")
    (tmp_path / "src" / "governor.py").write_text(GOVERNOR, encoding="utf-8")
    (tmp_path / "src" / "mod.py").write_text(MOD, encoding="utf-8")
    (tmp_path / "src" / "test_mod.py").write_text(TESTS, encoding="utf-8")
    (tmp_path / ".github" / "known_xfail.txt").write_text("", encoding="utf-8")
    _csv(tmp_path, [ROW1, ROW2])
    return tmp_path


def _csv(root: Path, rows: list, header: str = HEADER) -> None:
    (root / "docs" / "control_map.csv").write_text("\n".join([header] + rows) + "\n", encoding="utf-8")


def _run(mod, root: Path, capsys, win=None, lin=None, extra=()) -> tuple[int, str]:
    args = ["--root", str(root), "--base-rev", "refs/b32-test/no-base"]   # дельта не зависит от внешнего git
    if win is not None:
        args += ["--junit", f"windows={_junit(root / 'w.xml', win)}"]
    if lin is not None:
        args += ["--junit", f"linux={_junit(root / 'l.xml', lin)}"]
    rc = mod.run(args + list(extra))
    return rc, capsys.readouterr().out


def both(mod, root, capsys, cases=None, win=None, lin=None, extra=()):
    c = ALL_PASS if cases is None else cases
    return _run(mod, root, capsys, dict(c if win is None else win), dict(c if lin is None else lin), extra)


# ---------------------------------------------------------------- норма
def test_all_pass_on_both_platforms_exit_0(repo, capsys):
    rc, out = both(V, repo, capsys)
    assert rc == 0, out
    assert "ROW INV-01 PASS behavior" in out and "ROW INV-02 PASS integration" in out
    assert "проверено на 2 платформах из 2" in out and "EXIT=0" in out


def test_acceptance_block_at_most_15_lines_and_last(repo, capsys):
    rc, out = both(V, repo, capsys)
    lines = out.strip().splitlines()
    i = lines.index("=== ПРИЁМКА control_map ===")
    assert len(lines) - i <= 15 and lines[-1] == "EXIT=0"
    assert any(x.startswith("дельта: база для дельты отсутствует") for x in lines[i:])


# ---------------------------------------------------------------- 1. ID 1-в-1
def test_missing_id_fails(repo, capsys):
    _csv(repo, [ROW1])
    rc, out = both(V, repo, capsys)
    assert rc == 1 and "только в INVARIANTS.md ['INV-02']" in out


def test_extra_id_fails(repo, capsys):
    _csv(repo, [ROW1, ROW2, "INV-03,src/mod.py::decide,src/test_mod.py::test_b,behavior,путь,"])
    rc, out = both(V, repo, capsys)
    assert rc == 1 and "только в csv ['INV-03']" in out


def test_duplicate_id_fails(repo, capsys):
    _csv(repo, [ROW1, ROW2, ROW2])
    rc, out = both(V, repo, capsys)
    assert rc == 1 and "повтор ID" in out


def test_wrong_header_fails(repo, capsys):
    _csv(repo, [ROW1, ROW2], header="id,implementation,tests,evidence,scope,known_gap,status")
    rc, out = both(V, repo, capsys)
    assert rc == 1 and "схема csv" in out


# ---------------------------------------------------------------- 2. код
def test_missing_file_fails(repo, capsys):
    _csv(repo, [ROW1.replace("src/mod.py", "src/nope.py"), ROW2])
    rc, out = both(V, repo, capsys)
    assert rc == 1 and "нет файла src/nope.py" in out


def test_missing_symbol_fails(repo, capsys):
    _csv(repo, [ROW1.replace("Gate.before", "Gate.after"), ROW2])
    rc, out = both(V, repo, capsys)
    assert rc == 1 and "нет символа Gate.after" in out


@pytest.mark.parametrize("rel", ["tools/mod.py", "docs/mod.py", "src/../tools/mod.py"])
def test_implementation_outside_code_roots_fails(repo, capsys, rel):
    """Корни implementation -- явный список src/ и oko_rag/, не «любой каталог»."""
    (repo / "tools").mkdir(exist_ok=True)
    (repo / "tools" / "mod.py").write_text(MOD, encoding="utf-8")
    (repo / "docs" / "mod.py").write_text(MOD, encoding="utf-8")
    _csv(repo, [ROW1.replace("src/mod.py", rel), ROW2])
    rc, out = both(V, repo, capsys)
    assert rc == 1 and "implementation вне корней src/, oko_rag/" in out


def test_implementation_in_oko_rag_root_accepted(repo, capsys):
    (repo / "oko_rag").mkdir()
    (repo / "oko_rag" / "retriever.py").write_text("def build_context():\n    return 1\n", encoding="utf-8")
    _csv(repo, [ROW1, ROW2.replace("src/mod.py::decide", "oko_rag/retriever.py::build_context")])
    rc, out = both(V, repo, capsys)
    assert rc == 0, out


def test_symbol_in_text_but_not_defined_fails(repo, capsys):
    """Поиск по AST, не по тексту: имя в комментарии или строке символом не считается."""
    (repo / "src" / "mod.py").write_text(MOD + "\n# def ghost(): pass\nX = 'def ghost'\n", encoding="utf-8")
    _csv(repo, [ROW1, ROW2.replace("::decide", "::ghost")])
    rc, out = both(V, repo, capsys)
    assert rc == 1 and "нет символа ghost" in out


# ---------------------------------------------------------------- 3. статусы JUnit
@pytest.mark.parametrize("bad", ["FAIL", "ERROR", "XFAIL", "XPASS_STRICT"])
def test_non_pass_status_fails(repo, capsys, bad):
    c = dict(ALL_PASS)
    c[("src.test_mod", "test_a")] = bad
    rc, out = both(V, repo, capsys, lin=c)
    assert rc == 1 and "ROW INV-01 FAIL" in out


def test_skip_without_known_gap_fails(repo, capsys):
    c = dict(ALL_PASS)
    c[("src.test_mod", "test_a")] = "SKIP"
    rc, out = both(V, repo, capsys, lin=c)
    assert rc == 1 and "[linux]: SKIP" in out


def test_platform_skip_with_known_gap_is_gap_not_pass(repo, capsys):
    _csv(repo, [ROW1 + "test_a на Linux пропускается (только Windows)", ROW2])
    c = dict(ALL_PASS)
    c[("src.test_mod", "test_a")] = "SKIP"
    rc, out = both(V, repo, capsys, lin=c)
    assert rc == 0 and "ROW INV-01 GAP" in out and "SKIP (known_gap) -> GAP" in out


def test_skip_with_known_gap_not_naming_the_test_fails(repo, capsys):
    """Пробел записан, но о другом: пропуск теста им не оправдан."""
    _csv(repo, [ROW1 + "другой пробел (test_ab -- не этот тест)", ROW2])
    c = dict(ALL_PASS)
    c[("src.test_mod", "test_a")] = "SKIP"
    rc, out = both(V, repo, capsys, lin=c)
    assert rc == 1 and "test_a [linux]: SKIP" in out


def test_missing_in_xml_fails(repo, capsys):
    c = {k: v for k, v in ALL_PASS.items() if k != ("src.test_mod.TestK", "test_m")}
    rc, out = both(V, repo, capsys, win=c)
    assert rc == 1 and "TestK::test_m [windows]: MISSING" in out


def test_pass_on_one_platform_only_fails(repo, capsys):
    c = dict(ALL_PASS)
    c[("src.test_mod", "test_b")] = "FAIL"
    rc, out = both(V, repo, capsys, win=c)
    assert rc == 1 and "ROW INV-02 FAIL" in out and "[windows]: FAIL" in out


def test_xpass_non_strict_by_xfail_mark_fails(repo, capsys):
    """Без strict pytest пишет XPASS как обычный проход: тест с меткой xfail подтверждающим быть не может."""
    _csv(repo, [ROW1, ROW2.replace("test_b", "test_x")])
    c = dict(ALL_PASS)
    c[("src.test_mod", "test_x")] = "PASS"
    rc, out = both(V, repo, capsys, cases=c)
    assert rc == 1 and "test_x [windows]: XPASS" in out


def test_known_xfail_listed_test_fails(repo, capsys):
    (repo / ".github" / "known_xfail.txt").write_text("src/test_mod.py::test_b\n", encoding="utf-8")
    rc, out = both(V, repo, capsys)
    assert rc == 1 and "в known_xfail" in out


def test_parametrized_all_instances_must_pass(repo, capsys):
    c = dict(ALL_PASS)
    del c[("src.test_mod", "test_b")]
    c[("src.test_mod", "test_b[1]")] = "PASS"
    c[("src.test_mod", "test_b[2]")] = "SKIP"
    rc, out = both(V, repo, capsys, cases=c)
    assert rc == 1 and "test_b [windows]: SKIP" in out
    c[("src.test_mod", "test_b[2]")] = "PASS"
    rc, out = both(V, repo, capsys, cases=c)
    assert rc == 0, out


def test_single_platform_never_pass(repo, capsys):
    rc, out = _run(V, repo, capsys, lin=dict(ALL_PASS))
    assert rc == 0 and "проверено на 1 платформе из 2: PASS не выдаётся" in out
    assert "ROW INV-01 PARTIAL" in out and " PASS behavior" not in out


def test_single_platform_refuses_map(repo, capsys):
    rc, out = _run(V, repo, capsys, lin=dict(ALL_PASS), extra=["--write-md", str(repo / "m.md")])
    assert rc == 2 and not (repo / "m.md").exists()


def test_no_junit_is_usage_error(repo, capsys):
    assert V.run(["--root", str(repo)]) == 2


# ---------------------------------------------------------------- 4. схема
def test_bad_evidence_fails(repo, capsys):
    _csv(repo, [ROW1.replace(",behavior,", ",unit,"), ROW2])
    rc, out = both(V, repo, capsys)
    assert rc == 1 and "недопустимый evidence «unit»" in out


def test_empty_scope_fails(repo, capsys):
    _csv(repo, [ROW1.replace(",путь A,", ",,"), ROW2])
    rc, out = both(V, repo, capsys)
    assert rc == 1 and "INV-01: пустой scope" in out


def test_manual_with_tests_fails_and_manual_needs_gap(repo, capsys):
    _csv(repo, [ROW1.replace(",behavior,", ",manual,"), "INV-02,,,manual,процесс,"])
    rc, out = both(V, repo, capsys)
    assert rc == 1 and "INV-01: manual с тестами" in out and "INV-02: manual без known_gap" in out


def test_manual_row_is_manual(repo, capsys):
    _csv(repo, [ROW1, "INV-02,,,manual,процесс,автотеста нет"])
    rc, out = both(V, repo, capsys)
    assert rc == 0 and "ROW INV-02 MANUAL manual" in out


def test_source_scan_disguised_as_behavior_fails(repo, capsys):
    _csv(repo, [ROW1, ROW2.replace("test_b", "test_scan")])
    rc, out = both(V, repo, capsys)
    assert rc == 1 and "читает исходник" in out and "это source-scan, не integration" in out
    _csv(repo, [ROW1, ROW2.replace("test_b", "test_scan").replace("integration", "source-scan")])
    rc, out = both(V, repo, capsys)
    assert rc == 0, out


# ---------------------------------------------------------------- 5. коды ядра
def test_kernel_codes_mismatch_fails(repo, capsys):
    (repo / "src" / "governor.py").write_text(GOVERNOR.replace('{"AAA_ONE"}', '{"AAA_ONE", "DDD_NEW"}'),
                                              encoding="utf-8")
    rc, out = both(V, repo, capsys)
    assert rc == 1 and "только в коде ['DDD_NEW']" in out


def test_kernel_codes_count_line_checked(repo, capsys):
    (repo / "docs" / "kernel_codes.txt").write_text(KERNEL.replace("Всего 3", "Всего 12"), encoding="utf-8")
    rc, out = both(V, repo, capsys)
    assert rc == 1 and "в документе 12, в реестре 3" in out


def test_kernel_registry_absent_is_gap_line_not_pass(repo, capsys):
    (repo / "src" / "governor.py").write_text("X = 1\n", encoding="utf-8")
    rc, out = both(V, repo, capsys)
    assert "CODES GAP реестр кодов отсутствует, сверка не выполнена" in out and "коды ядра: GAP" in out


# ---------------------------------------------------------------- 6. зоны и строки «что сломается»
def test_zone_missing_fails(repo, capsys):
    (repo / "docs" / "INVARIANTS.md").write_text(INVARIANTS.replace("| INV-02 | рабочая | — |\n", ""),
                                                 encoding="utf-8")
    rc, out = both(V, repo, capsys)
    assert rc == 1 and "INV-02: зон в INVARIANTS.md 0" in out


def test_plain_line_missing_fails(repo, capsys):
    (repo / "docs" / "control_map_plain.csv").write_text("id,breaks\nINV-01,x\n", encoding="utf-8")
    rc, out = both(V, repo, capsys)
    assert rc == 1 and "«что сломается» не 1-в-1" in out


# ---------------------------------------------------------------- 7. карта
def test_map_written_then_checked_ignoring_head_line(repo, capsys):
    md = repo / "docs" / "CONTROL_MAP.md"
    rc, _ = both(V, repo, capsys, extra=["--write-md", str(md), "--generated-on", "aaaa"])
    assert rc == 0 and V.GEN_PREFIX + "aaaa" in md.read_text(encoding="utf-8")
    rc, out = both(V, repo, capsys, extra=["--check-md", "docs/CONTROL_MAP.md", "--generated-on", "bbbb"])
    assert rc == 0 and "карта: совпадает со свежей" in out
    head = md.read_text(encoding="utf-8").splitlines()[:6]
    assert len([x for x in head if x]) <= 6 and V.UNREVIEWED in head


def test_stale_map_fails(repo, capsys):
    md = repo / "docs" / "CONTROL_MAP.md"
    both(V, repo, capsys, extra=["--write-md", str(md)])
    _csv(repo, [ROW1 + "новый пробел", ROW2])
    rc, out = both(V, repo, capsys, extra=["--check-md", "docs/CONTROL_MAP.md"])
    assert rc == 1 and "карта устарела, перегенерируй" in out


def test_map_crlf_checkout_still_matches(repo, capsys):
    """Windows checkout с autocrlf: та же карта с CRLF не считается устаревшей."""
    md = repo / "docs" / "CONTROL_MAP.md"
    both(V, repo, capsys, extra=["--write-md", str(md)])
    md.write_bytes(md.read_bytes().replace(b"\n", b"\r\n"))
    rc, out = both(V, repo, capsys, extra=["--check-md", "docs/CONTROL_MAP.md"])
    assert rc == 0, out


def test_inputs_with_bom_and_crlf_accepted(repo, capsys):
    """Эмуляция Windows: csv и INVARIANTS.md с BOM и CRLF разбираются так же."""
    for name in ("control_map.csv", "INVARIANTS.md", "control_map_plain.csv"):
        p = repo / "docs" / name
        p.write_bytes(b"\xef\xbb\xbf" + p.read_bytes().replace(b"\n", b"\r\n"))
    rc, out = both(V, repo, capsys)
    assert rc == 0, out


def test_output_is_utf8_bytes_under_cp1251_stdout(repo, monkeypatch):
    """Эмуляция консоли Windows (cp1251): вывод идёт байтами UTF-8 мимо кодировки локали."""
    raw = io.BytesIO()
    monkeypatch.setattr(sys, "stdout", io.TextIOWrapper(raw, encoding="cp1251"))
    rc = V.run(["--root", str(repo), "--junit", f"windows={_junit(repo / 'w.xml', ALL_PASS)}",
                "--junit", f"linux={_junit(repo / 'l.xml', ALL_PASS)}"])
    assert rc == 0 and "ПРИЁМКА".encode("utf-8") in raw.getvalue()


# ---------------------------------------------------------------- тесты-лжецы: мутанты верификатора
def _mutant(old: str, new: str, tag: str):
    src = TOOL.read_text(encoding="utf-8")
    assert src.count(old) == 1, (tag, src.count(old))
    return _load("vcm_mutant_" + tag, src.replace(old, new))


def test_liar_xfail_counted_as_pass_is_caught(repo, capsys):
    m = _mutant('st = "XFAIL"', 'st = "PASS"', "xfail")
    c = dict(ALL_PASS)
    c[("src.test_mod", "test_a")] = "XFAIL"
    assert both(V, repo, capsys, lin=c)[0] == 1
    assert both(m, repo, capsys, lin=c)[0] == 0          # мутант пропустил бы XFAIL -- сценарий его ловит


def test_liar_one_xml_is_enough_is_caught(repo, capsys):
    m = _mutant("full = set(jun) == set(need)", "full = bool(jun)", "onexml")
    rc, out = _run(V, repo, capsys, lin=dict(ALL_PASS))
    assert "PASS behavior" not in out
    rc_m, out_m = _run(m, repo, capsys, lin=dict(ALL_PASS))
    assert "ROW INV-01 PASS behavior" in out_m           # мутант выдал бы PASS по одному отчёту


def test_liar_skip_without_gap_as_gap_is_caught(repo, capsys):
    m = _mutant('if st == "SKIP" and _gap_names(row["known_gap"], t):', 'if st == "SKIP":', "skip")
    c = dict(ALL_PASS)
    c[("src.test_mod", "test_a")] = "SKIP"
    assert both(V, repo, capsys, lin=c)[0] == 1
    assert both(m, repo, capsys, lin=c)[0] == 0


def test_liar_xfail_mark_ignored_is_caught(repo, capsys):
    m = _mutant('if st == "PASS" and xm:', 'if False:', "xpass")
    _csv(repo, [ROW1, ROW2.replace("test_b", "test_x")])
    c = dict(ALL_PASS)
    c[("src.test_mod", "test_x")] = "PASS"
    assert both(V, repo, capsys, cases=c)[0] == 1
    assert both(m, repo, capsys, cases=c)[0] == 0


def test_liar_source_scan_check_removed_is_caught(repo, capsys):
    m = _mutant("if rs:", "if False:", "scan")
    _csv(repo, [ROW1, ROW2.replace("test_b", "test_scan")])
    assert both(V, repo, capsys)[0] == 1
    assert both(m, repo, capsys)[0] == 0


# ---------------------------------------------------------------- настоящая карта репозитория
def _repo_rows():
    text = (ROOT / "docs" / "control_map.csv").read_text(encoding="utf-8-sig")
    return list(csv.DictReader(io.StringIO(text)))


def _synthetic_all_pass(path: Path) -> Path:
    cases = {}
    for r in _repo_rows():
        for t in [x.strip() for x in r["tests"].split(";") if x.strip()]:
            cases[V.nodeid_key(t)] = "PASS"
    return _junit(path, cases)


def test_repo_map_structure_symbols_codes_and_generated_map(tmp_path, capsys):
    """Карта репозитория: ID 1-в-1, файлы и символы есть, коды ядра = реестр, тесты карты не читают исходник при
    evidence behavior / integration, docs/CONTROL_MAP.md = свежая карта (если все тесты карты прошли на обеих
    платформах). Настоящие статусы считает job control-map в CI по двум отчётам JUnit."""
    x = _synthetic_all_pass(tmp_path / "all.xml")
    rc = V.run(["--root", str(ROOT), "--junit", f"windows={x}", "--junit", f"linux={x}",
                "--check-md", "docs/CONTROL_MAP.md", "--base-rev", "HEAD"])
    out = capsys.readouterr().out
    assert rc == 0, out
    assert "CODES PASS" in out and "карта: совпадает со свежей" in out


def test_repo_map_has_every_evidence_counted_honestly():
    rows = _repo_rows()
    assert len(rows) == 21 and all(r["evidence"] in V.EVIDENCE for r in rows)
    for r in rows:
        if r["evidence"] == "manual":
            assert not r["tests"] and r["known_gap"]
