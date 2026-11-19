"""voice_kit (голос бота, подготовка без сети): v5, числа, хвосты, время, «ё», слаги, каталоги из кода, manifest,
пачки, CLI voice_prep с чтением назад. Файл фраз -- синтетический (утверждённый v4 вне репозитория)."""
import importlib.util
import json
from pathlib import Path

import pytest

import voice_kit as k

ROOT = Path(__file__).resolve().parent.parent


def fake_v4():
    return [
        {"id": "standby", "ask": ["Выключить телевизор? Скажи «да»."], "say": ["Команда выключения отправлена."]},
        {"id": "volume_get", "ok": ["Громкость: {n}."], "fail": ["Нет данных о громкости."],
         "glue": {"prefix": "Громкость:", "value": "{n}"}},
        {"id": "status", "ok": ["Телевизор: {summary}, на {time}."], "fail": ["Нет данных о состоянии телевизора."]},
        {"id": "colour_желтый", "ok": ["Подсветка жёлтая!"], "check": ["не озвучивается"]},
        {"id": "gen_understood", "say": ["Понято: {cmd}."]},
        {"id": "gen_last_known", "say": ["Последнее известное: {time}."]},
        {"id": "gen_loop", "say": ["Одна и та же команда {n} раз подряд."]},
        {"id": "gen_series_stopped", "say": ["Серия остановлена на шаге {i} из {m}."]},
    ]


def test_v5_changes_only_two_records_and_diff():
    v4 = fake_v4()
    v5, diff = k.make_v5(v4)
    by = {r["id"]: r for r in v5}
    assert by["gen_series_stopped"]["say"] == ["Серия остановлена на шаге {i}, всего шагов: {m}."]
    assert by["gen_loop"]["glue"]["b_few"] == "раза подряд." and by["gen_loop"]["glue"]["b_many"] == "раз подряд."
    assert all(by[r["id"]] == r for r in v4 if r["id"] not in k.V5_CHANGES)
    assert any(d.startswith("gen_series_stopped.say:") for d in diff) and v4[-1]["say"][0].endswith("из {m}.")


def test_v5_requires_both_records():
    with pytest.raises(ValueError):
        k.make_v5([r for r in fake_v4() if r["id"] != "gen_loop"])


@pytest.mark.parametrize("n,tail", [(1, "b_many"), (2, "b_few"), (3, "b_few"), (4, "b_few"), (5, "b_many"),
                                    (11, "b_many"), (12, "b_many"), (13, "b_many"), (14, "b_many"), (21, "b_many"),
                                    (22, "b_few"), (34, "b_few"), (100, "b_many"), (112, "b_many"), (102, "b_few")])
def test_loop_tail_rule(n, tail):
    assert k.loop_tail(n) == tail


def test_number_words():
    assert [k.number_words(n) for n in (0, 1, 2, 10, 15, 20, 25, 99, 100)] == [
        "ноль", "один", "два", "десять", "пятнадцать", "двадцать", "двадцать пять", "девяносто девять", "сто"]
    with pytest.raises(ValueError):
        k.number_words(101)


def test_time_files():
    assert k.time_files(14, 0) == ["num_014", "num_000", "num_000"]
    assert k.time_files(9, 5) == ["num_009", "num_000", "num_005"]
    assert k.time_files(23, 59) == ["num_023", "num_059"]
    with pytest.raises(ValueError):
        k.time_files(24, 0)


def test_speakable_and_yo_and_slug():
    assert k.speakable("Выключить телевизор? Скажи «да»!") == "Выключить телевизор. Скажи «да»."
    assert k.yo("Подсветка Зеленый, режим включен.")[0] == "Подсветка Зелёный, режим включён."
    assert k.slug("colour_желтый") == "colour_zheltyy" and k.slug("key_CursorUp") == "key_cursorup"
    assert len(k.slug("очень длинное имя записи для проверки длины")) <= 24


def test_catalogs_from_code_have_sources_and_no_gaps():
    summ = k.summary_dict(ROOT)
    assert [s["text"] for s in summ][:6] == ["включён", "выключен", "в режиме ожидания", "на главном экране",
                                            "в меню настроек", "нет сигнала"]
    assert all(s["source"] for s in summ) and len({s["file"] for s in summ}) == len(summ)
    cat = k.cmd_catalog(ROOT)
    ids = {c["id"] for c in cat}
    assert {"standby", "volume_up", "status", "menu_route", "key_CursorUp", "amb_colour_krasnyy"} <= ids
    assert not ids & set(k.CMD_LOCAL)
    assert all(c["source"].startswith("src/") and ":" in c["source"] for c in cat)
    assert [c["n"] for c in cat] == list(range(1, len(cat) + 1)) and len({c["file"] for c in cat}) == len(cat)


def test_catalog_fails_on_command_without_wording(tmp_path, monkeypatch):
    monkeypatch.setattr(k, "CMD_SAY", {x: y for x, y in k.CMD_SAY.items() if x != "status"})
    with pytest.raises(ValueError, match="status"):
        k.cmd_catalog(ROOT)


def test_manifest_layout_and_counts():
    v5, _ = k.make_v5(fake_v4())
    summ = [{"text": "включён", "file": "stat_vklyuchen", "source": "x"}]
    cat = [{"n": 1, "id": "status", "text": "состояние телевизора", "file": "cmd_001_status", "source": "y"}]
    m = k.build_manifest(v5, summ, cat)
    files = {e["file"]: e for e in m["entries"]}
    assert "p001_standby__ask__1" in files and files["p001_standby__ask__1"]["text"] == "Выключить телевизор. Скажи «да»."
    assert files["p002_volume_get__ok__1__a"]["text"] == "Громкость:" and files["p002_volume_get__ok__1__a"]["join_tail"]
    assert {"p003_status__ok__1__a", "p003_status__ok__1__b", "p005_gen_understood__say__1__plain",
            "p007_gen_loop__say__1__b_few", "p007_gen_loop__say__1__b_many", "p008_gen_series_stopped__say__1__b"} <= set(files)
    assert files["num_025"]["text"] == "Текущее значение: двадцать пять." and "num_100" in files and "num_101" not in files
    assert m["counts"]["num"] == 101 and m["counts"]["total"] == len(m["entries"])
    assert not any("check" == e.get("field") for e in m["entries"])


def test_batches_limits_tuning_line_and_positions():
    v5, _ = k.make_v5(fake_v4())
    m = k.build_manifest(v5, [], [])
    bs = k.make_batches(m)
    for b in bs:
        est = sum(k.estimate_s(e["text"]) for e in b) + k.estimate_s(k.TUNING_LINE)
        assert len(b) <= k.BATCH_MAX and est <= k.BATCH_HARD_S + 1e-9
        t = k.batch_text(b)
        assert t.splitlines()[0] == k.TUNING_LINE and "?" not in t and "!" not in t
        assert [e["pos"] for e in b] == list(range(1, len(b) + 1))
    assert sum(len(b) for b in bs) == len(m["entries"]) and m["batches"] == len(bs)


def _load_cli():
    spec = importlib.util.spec_from_file_location("voice_prep_cli", ROOT / "tools" / "voice_prep.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_cli_voice_prep_stops_on_unapproved_counts(tmp_path, capsys):
    v4 = tmp_path / "v4.jsonl"
    v4.write_text("".join(json.dumps(r, ensure_ascii=False) + "\n" for r in fake_v4()), encoding="utf-8")
    cli = _load_cli()
    assert cli.main(["--v4", str(v4), "--data", str(tmp_path / "d"), "--batches", str(tmp_path / "b")]) == 2
    assert "STOP" in capsys.readouterr().out and not (tmp_path / "d").exists()


def test_cli_voice_prep_writes_and_reads_back(tmp_path, capsys, monkeypatch):
    v4 = tmp_path / "v4.jsonl"
    v4.write_text("".join(json.dumps(r, ensure_ascii=False) + "\n" for r in fake_v4()), encoding="utf-8")
    cli = _load_cli()
    monkeypatch.setattr(cli.k, "EXPECTED_V4", k.counts(fake_v4()) | {})
    monkeypatch.setitem(cli.k.EXPECTED_V4, "voiced", k.counts(fake_v4())["voiced"])
    assert cli.main(["--v4", str(v4), "--data", str(tmp_path / "d"), "--batches", str(tmp_path / "b")]) == 0
    o = capsys.readouterr().out
    assert "WRITTEN" in o and "read_back=ok" in o and "DIFF gen_series_stopped.say" in o
    m = json.loads((tmp_path / "d" / "voice" / "despina" / "manifest.json").read_text(encoding="utf-8"))
    assert (tmp_path / "b" / "batch_01.txt").read_text(encoding="utf-8").startswith(k.TUNING_LINE)
    assert (tmp_path / "b" / "00_STYLE_PROMPT.txt").read_text(encoding="utf-8").startswith("Говори на русском")
    assert "batch_01.wav" in (tmp_path / "b" / "00_HOWTO.txt").read_text(encoding="utf-8")
    assert all("batch" in e for e in m["entries"])
    v5 = [json.loads(x) for x in (tmp_path / "d" / "voice" / "VOICE_phrases_v5.jsonl").read_text(encoding="utf-8").splitlines()]
    assert len(v5) == len(fake_v4())


def test_write_back_detects_mismatch(tmp_path, monkeypatch):
    cli = _load_cli()
    p = tmp_path / "x.txt"
    real = Path.read_bytes
    monkeypatch.setattr(Path, "read_bytes", lambda self: b"other" if self == p else real(self))
    with pytest.raises(OSError, match="чтение назад"):
        cli.write_back(p, "текст")
