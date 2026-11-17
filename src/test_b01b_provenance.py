"""(12.11.2026): защита от выдуманного статуса ТВ по происхождению данных, вторая линия -- правило «сторож выдуманного статуса ТВ».
Источники (разбор 11.11 22:4x):
- шаблон -- brief каталога «ТВ Philips: «подсветка музыка», цвет, яркость…» (первая выдумка 448, 13:54);
- метка -- блок фактов «[дата] ТВ: …», где BLIND/WARN/FAIL шли под той же меткой, что и прочитанное с ТВ;
- размножение -- окно истории.
Пропуск «сторож выдуманного статуса ТВ» в истории: 478 (22:12:56). Контрпримеры: 5 ложных + 5 пропусков."""
from __future__ import annotations

import datetime as dt

import pytest

import capabilities as caps
import event_log as el
import router as R

# --- правило (вторая линия) -----------------------------------------------------------------------------------

LIVE_478 = ("?status", "Статус системы: команда /status. Техника сейчас: Сушильная и стиральная машины не работают. "
                       "Гармин: данные на 21:00. ТВ: подсветка музыки, яркость 2 из 10.")


def test_live_478_mid_line_label_is_caught():
    u, a = LIVE_478
    assert R.fake_status(a) == "тв: подсветка музыки, яркость 2 из 10."
    assert R.state_snapshot(u, a)                       # вопрос: показать, не писать
    assert R.fake_done("vipolni status", a)            # не вопрос: ⚠️ и не в историю


@pytest.mark.parametrize("answer", [
    "Подсветка музыки.", "Яркость 2 из 10.", "Громкость 12/60.", "ТВ (прочитано с телевизора): Подсветка: яркость 2 из 10",
    "Око: всё ок. ТВ: режим кино", "tv: on",
])
def test_morphology_and_provenance_label_caught(answer):
    assert R.fake_status(answer), answer


REF_FP = [   # контрпримеры: справка, не статус
    "ТВ: в этой памятке сокращение обозначает устройство, а не его текущий статус",
    "Телевизор: в качестве примера можно рассмотреть переключение источника HDMI",
    "TV: здесь используется как название раздела документации",
    "Подсветка под музыку — это синхронизация светового эффекта с аудиосигналом",
    "Громкость звука — параметр, задающий уровень аудиовыхода",
]
REF_FN = [   # контрпримеры: выдумки без метки и без имени параметра
    "Сейчас выбран динамический режим",
    "Готово: установлен розовый оттенок",
    "Устройство уже переключено на статичный режим",
    "Применено: выбран красный цвет",
]


@pytest.mark.parametrize("answer", REF_FP)
def test_gpt_reference_answers_not_flagged(answer):
    assert R.fake_status(answer) is None, (answer, R.fake_status(answer))


@pytest.mark.parametrize("answer", REF_FN)
def test_gpt_set_state_claims_caught_even_on_translit_input(answer):
    assert R.fake_status(answer), answer
    assert R.fake_done("vrubi rozovuju", answer), answer     # _REQUEST_RE транслит не знает -- ловит fake_status


@pytest.mark.xfail(strict=True, reason="B01b: «работает <что-то>» без предмета ТВ -- общий глагол; закрывается только "
                                       "происхождением (ответ мозга не источник статуса), не словами")
def test_gpt_fn4_generic_verb_not_caught_by_words():
    assert R.fake_status("Сейчас работает аудиосинхронизация")


@pytest.mark.parametrize("answer", [
    "Не установлен цвет — команда не распознана.", "Цвет выбирается фразой «подсветка красная».",
    "Режим выбран не был: команда до телевизора не дошла.",
])
def test_negated_or_instruction_not_flagged(answer):
    assert R.fake_status(answer) is None, (answer, R.fake_status(answer))


# --- происхождение в журнале фактов ------------------------------------------------------------------------------

@pytest.mark.parametrize("reply,outcome,kind", [
    ("Подсветка: режим один цвет (статичный), цвет красная, hue 0, sat 255, bri 255", "PASS", "verified"),
    ("Понял как «подсветка красная» (связка). Подсветка: режим один цвет (статичный), цвет красная", "PASS", "verified"),
    ("Отправил: кнопка «Назад»", "PASS", "blind"),
    ("Понял как «назад» (связка). Отправил: кнопка «Назад»", "PASS", "blind"),
    ("Подсветка: запрошено погасить (яркость цвета 0), телевизор сообщает яркость 255", "WARN", "warn"),
    ("📺 Телевизор не отвечает", "FAIL", "fail"),
    ("Не выполнено", None, None),
])
def test_tv_kind(reply, outcome, kind):
    assert R.tv_kind(reply, outcome) == kind


NOW = dt.datetime(2026, 11, 12, 9, 0, tzinfo=dt.timezone(dt.timedelta(hours=2)))


def _ts(h, m):
    return f"2026-11-12T{h:02d}:{m:02d}:00+02:00"


def test_kind_stored_and_rendered_in_facts(tmp_path):
    el.init(tmp_path / "e.jsonl")
    try:
        el._DEFAULT.record("tv", "Отправил: кнопка «Назад»", ts=_ts(8, 50), kind="blind")
        lines = el.facts_block(now=NOW).splitlines()
        assert lines[-1] == "[12.11 08:50] ТВ (отправлено, не проверено): Отправил: кнопка «Назад»"
        assert "не обязательно сейчас" in lines[1]
        reloaded = el.EventLog(tmp_path / "e.jsonl")             # поле переживает перезапуск
        assert reloaded.recent(now=NOW)[-1]["kind"] == "blind"   # было без now -- часовая мина (13.11 упал)
    finally:
        el.reset()


@pytest.mark.parametrize("kind,note", [("verified", "прочитано с телевизора"), ("warn", "сверка не сошлась"),
                                       ("fail", "ошибка")])
def test_every_kind_has_note(tmp_path, kind, note):
    log = el.EventLog(tmp_path / "e.jsonl")
    rec = log.record("tv", "x", ts=_ts(8, 0), kind=kind)
    assert el.line(rec) == f"[12.11 08:00] ТВ ({note}): x"


def test_old_records_without_kind_render_as_before(tmp_path):
    log = el.EventLog(tmp_path / "e.jsonl")
    rec = log.record("tv", "Подсветка: On", ts=_ts(8, 0))
    assert "kind" not in rec and el.line(rec) == "[12.11 08:00] ТВ: Подсветка: On"
    assert "kind" not in log.record("tv", "y", ts=_ts(8, 1), kind="bogus")     # неизвестный kind не пишется


def test_fact_line_with_provenance_is_caught_if_model_copies_it(tmp_path):
    log = el.EventLog(tmp_path / "e.jsonl")
    line = el.line(log.record("tv", "Подсветка: яркость 2 из 10", ts=_ts(8, 0), kind="verified"))
    copied = line.split("] ", 1)[1]                        # модель копирует строку факта без даты
    assert R.fake_done("krasnaja", copied)


def test_bot_passes_kind_for_tv_events():
    from pathlib import Path
    text = (Path(__file__).resolve().parent / "bot.py").read_text(encoding="utf-8-sig")
    body = text[text.index("async def _run_tv_route"):]
    body = body[:body.index("\nasync def ", 1)] if "\nasync def " in body[1:] else body
    assert '_event("tv", answer, router.tv_kind(answer, outcome))' in body


# --- brief каталога ----------------------------------------------------------------------------------------------

def test_tv_brief_is_not_status_shaped():
    tv = next(c for c in caps.DECLARED if c.key == "tv")
    b = tv.brief.lower()
    assert not b.startswith(("тв", "телевизор:")), tv.brief         # нет «метка: …» в начале
    assert "подсветка музыка" not in b                              # шаблон выдумки 448
    assert R.fake_status(tv.brief) is None                          # сам brief правилом не ловится
    assert "/tv" not in b and "не видишь" not in b                  # без навязывания /tv и «ты слепая»
    assert "блока фактов" in caps.RULE                              # источник состояния -- факты
    cat = caps.build_catalog()
    assert R.fake_status(cat) is None, R.fake_status(cat)
