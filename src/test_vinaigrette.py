"""Приёмочные испытания «Алгоритма винегрета» (спецификация v1.0, §15–16).

Реальных устройств нет: исполнительный контур — InMemoryDeviceAdapter, Registry — SQLite в памяти,
время — ручные часы (ADR-012).
"""
from __future__ import annotations

import datetime as dt

import pytest

from oko import HomeConfig, Settings, VectorSpace, Vinaigrette
from oko.calibration import DEGRADED_MODE, NORMAL_MODE
from oko.calibration.confidence_model import conditional_entropy, smoothed
from oko.execution import InMemoryDeviceAdapter
from oko.memory import CrystallizationEngine, Evidence
from oko.parser import VectorParser
from oko.parser.normalization import morph, normalize, translit_candidates, within1
from oko.registry import open_db
from oko.registry import event_registry as ev

OP = "operator-1"


class Clock:
    def __init__(self):
        self.t = dt.datetime(2026, 11, 10, 12, 0, tzinfo=dt.timezone.utc)

    def __call__(self):
        self.t += dt.timedelta(milliseconds=1)
        return self.t


@pytest.fixture
def adapter():
    return InMemoryDeviceAdapter()


@pytest.fixture
def home():
    return HomeConfig(groups={"light_any": ("lamps",)},
                      rooms={"bedroom": ("спальня", "спальне", "спальню"), "kitchen": ("кухня", "кухне", "кухню")})


@pytest.fixture
def clock():
    return Clock()


@pytest.fixture
def oko(home, adapter, clock):
    return Vinaigrette(open_db(), home, adapter, clock=clock)


def types(oko, operator_id=OP):
    return [e.type for e in oko.registry.events(operator_id)]


def teach(oko, phrase, answers, sessions=("s1",), remember=True):
    """Оператор уточняет реплику и жмёт «Запомнить связку» (одного нажатия достаточно — решение 23:15 п.5)."""
    for s in sessions:
        out = oko.handle(phrase, OP, s)
        assert out.status == "CLARIFY"
        out = oko.resolve(out.request["request_id"], answers)
        assert out.status in ("EXECUTED", "FAILED")
        if remember:
            oko.remember(out.command_id)
    return out


# ---- T01–T10 ------------------------------------------------------------------------------

def test_t01_light_on_with_registered_group(oko, adapter):
    out = oko.handle("Включи свет", OP, "s1")
    assert out.status == "EXECUTED"
    assert (out.intent.target.value, out.intent.action.value) == ("lamps", "on")
    assert out.intent.target.source == "CONFIG"
    assert out.execution.status == "VERIFIED" and adapter.state["lamps"]["power"] is True


def test_t01_light_without_group_needs_clarification(adapter):
    oko = Vinaigrette(open_db(), HomeConfig(), adapter)
    out = oko.handle("Включи свет", OP, "s1")
    assert out.status == "CLARIFY"
    assert out.request["missing_field"] == "target"
    assert out.request["candidates"] == ["lamps", "tv_amb"]
    assert adapter.sent == []


def test_t02_synonyms_merge_into_one_pattern(oko):
    for phrase in ("Включи свет", "Свет включи", "Зажги люстру", "Вруби освещение"):
        assert oko.handle(phrase, OP, "s1").status == "EXECUTED"
    (rec,) = oko.repo.list(OP)
    assert rec.canonical_template == "LAMPS + ON"
    assert rec.observations == 4
    assert rec.aliases == []                      # наблюдение связку не создаёт (решение 23:15 п.5)
    assert oko.engine.compression_ratio(OP) == 4.0


def test_t03_one_press_makes_a_working_binding(oko, adapter):
    teach(oko, "Вруби телек", {"target": "tv_screen"})
    rec = oko.repo.get(oko.engine.pattern_id(OP, "tv_screen", "on", {}))
    assert (rec.status, rec.learned, rec.session_count) == ("CRYSTALLIZED", 1, 1)   # решение 23:15 п.5
    assert rec.confidence == pytest.approx(2 / 3)

    adapter.state["tv_screen"]["power"] = False
    out = oko.handle("Вруби телек", OP, "s1")
    assert out.status == "EXECUTED"
    assert (out.intent.target.value, out.intent.target.source) == ("tv_screen", "G6")
    assert adapter.state["tv_screen"]["power"] is True


def test_t03_choice_without_press_is_not_a_binding(oko):
    teach(oko, "Вруби телек", {"target": "tv_screen"}, remember=False)
    out = oko.handle("Вруби телек", OP, "s1")
    assert out.status == "CLARIFY" and out.intent.target.source != "G6"


def test_one_rejection_after_press_degrades_the_binding(oko):
    out = teach(oko, "Вруби телек", {"target": "tv_screen"})
    rec = oko.repo.get(oko.engine.pattern_id(OP, "tv_screen", "on", {}))
    oko.engine.record(Evidence(OP, "tv_screen", "on", "врубить телек", observed=False, validated=False))
    rec = oko.repo.get(rec.pattern_id)
    assert rec.status == "DEGRADED" and oko.handle("Вруби телек", OP, "s2").status == "CLARIFY"
    assert out.command_id


def test_t04_louder_without_target_clarifies(oko, adapter):
    out = oko.handle("Погромче", OP, "s1")
    assert out.status == "CLARIFY" and out.request["missing_field"] == "target"
    assert adapter.sent == []
    out = oko.resolve(out.request["request_id"], {"target": "audio"})
    assert out.status == "EXECUTED" and adapter.state["audio"]["level"] == 60.0
    assert out.intent.action.source == "BUILTIN" and out.intent.target.source == "OPERATOR"


def test_t04_context_target_carries_its_own_source(oko):
    out = oko.handle("Погромче", OP, "s1", {"active_target": "audio"})
    assert out.status == "EXECUTED" and out.intent.target.source == "CONTEXT"


def test_t05_bind(oko, adapter):
    out = oko.handle("Запомни новую команду", OP, "s1")
    assert out.status == "BIND" and out.intent.bind_payload == "новую команду"
    assert adapter.sent == [] and ev.BIND_REQUESTED in types(oko)


def test_t06_unknown_phrase_is_abstract_and_llm_only_advises(home, adapter):
    calls = []
    oko = Vinaigrette(open_db(), home, adapter, advisor=lambda text: calls.append(text) or "совет")
    out = oko.handle("Какая сегодня погода", OP, "s1")
    assert out.status == "ABSTRACT" and out.advisory == "совет"
    assert adapter.sent == []
    # LLM не участвует в обычном цикле распознавания команд
    oko.handle("Включи свет", OP, "s1")
    out = oko.handle("Погромче", OP, "s1")
    oko.resolve(out.request["request_id"], {"target": "audio"})
    assert calls == ["Какая сегодня погода"]


def test_t07_unknown_name_with_action_clarifies(oko, adapter):
    out = oko.handle("Включи пылесос", OP, "s1")
    assert out.status == "CLARIFY" and out.request["missing_field"] == "target"
    assert out.intent.target.is_unknown and out.intent.action.value == "on"
    assert adapter.sent == []


def test_t08_repeat_increments_counter_without_new_record(oko):
    for s in ("s1", "s2", "s3"):
        out = oko.handle("Включи свет", OP, s)
        oko.confirm(out.command_id, correct=True)
        assert oko.remember(out.command_id).status == "REMEMBERED"
    (rec,) = oko.repo.list(OP)
    assert (rec.observations, rec.confirmed_correct, rec.learned) == (3, 3, 3)    # метка не удвоилась
    assert len(rec.aliases) == 1 and rec.aliases[0]["count"] == 2      # использования после первой связки
    assert rec.examples == ["включи свет"]
    assert rec.status == "CRYSTALLIZED"


def test_t09_device_failure_is_infrastructure_incident(oko, adapter):
    adapter.unreachable.add("tv_screen")
    out = oko.handle("Вруби телек", OP, "s1")
    out = oko.resolve(out.request["request_id"], {"target": "tv_screen"})
    assert out.status == "FAILED" and out.execution.status == "FAILED"
    rec = oko.repo.get(oko.engine.pattern_id(OP, "tv_screen", "on", {}))
    assert (rec.execution_failures, rec.confirmed_incorrect, rec.learned) == (1, 0, 0)
    assert rec.confirmed_correct == 0 and rec.status == "CANDIDATE"   # выбор варианта — не метка
    assert oko.remember(out.command_id).status == "NOT_VERIFIED"      # связка без результата не сохраняется
    assert oko.repo.get(rec.pattern_id).aliases == [] and ev.BIND_REFUSED in types(oko)
    assert ev.INFRA_INCIDENT in types(oko)
    assert oko.monitor.accuracy(OP, 20) == (1.0, 1)                   # оценка парсера не пострадала


def test_t10_calibration_required_at_60_percent(oko):
    for i in range(10):
        out = oko.handle("Включи свет", OP, "s1")
        if i < 6:
            oko.confirm(out.command_id, correct=True)
            assert oko.calibration.mode(OP) == NORMAL_MODE
        else:
            oko.confirm(out.command_id, correct=False)
    assert oko.monitor.accuracy(OP, 20) == (0.6, 10)
    assert oko.calibration.mode(OP) == DEGRADED_MODE
    (event,) = oko.registry.events(OP, (ev.CALIBRATION_REQUIRED,))
    assert event.payload["n_valid"] == 10 and event.payload["axis_errors"]["T"] == 4
    assert ev.MEMORY_RECOUNT in types(oko)


# ---- отказ от исполнения при конфликтах -----------------------------------------------------

@pytest.mark.parametrize("phrase, field", [
    ("Включи выключи люстру", "action"),        # два действия
    ("Включи люстру и телевизор", "target"),    # две цели
    ("Вкключи люстру", "action"),               # опечатка на расстоянии 1 и от «включи», и от «выключи»
    ("Не включай свет", "action"),             # отрицание
    ("Покрась звук в красный", "target"),      # устройство не умеет действие
    ("Люстру на 150", "action"),                # действие не названо, уровень вне диапазона
])
def test_conflicting_matches_never_execute(oko, adapter, phrase, field):
    out = oko.handle(phrase, OP, "s1")
    assert out.status == "CLARIFY" and field in out.intent.missing
    assert adapter.sent == []


def test_router_refuses_incomplete_intent(oko):
    intent = oko.parser.parse("Погромче", OP)
    with pytest.raises(ValueError):
        oko.router.build(intent)


def test_clarify_accepts_only_missing_coordinates(oko):
    out = oko.handle("Погромче", OP, "s1")
    rid = out.request["request_id"]
    with pytest.raises(ValueError):
        oko.resolve(rid, {"action": "down"})           # действие парсер определил сам
    with pytest.raises(ValueError):
        oko.resolve(rid, {"target": "пылесос"})        # цели нет в конфигурации
    assert oko.resolve(rid, {"target": "audio"}).status == "EXECUTED"
    with pytest.raises(KeyError):
        oko.resolve(rid, {"target": "audio"})


def test_color_requires_value(oko, adapter):
    out = oko.handle("Поменяй цвет люстры", OP, "s1")
    assert out.status == "CLARIFY" and out.request["missing_field"] == "color"
    out = oko.resolve(out.request["request_id"], {"color": "синий"})
    assert out.status == "EXECUTED" and adapter.state["lamps"]["color"] == "синий"


# ---- парсер ---------------------------------------------------------------------------------

def test_normalization():
    assert normalize("  ВКЛЮЧИ-ка, пожалуйста, СВЁТ! ").tokens == ("включи", "свет")
    assert within1("вклучи", "включи") and not within1("вклучи", "выключи")


@pytest.mark.parametrize("latin, expected", [
    ("vkljuci", "включи"), ("vikljuci", "выключи"), ("cvet", "цвет"), ("podsvetku", "подсветку"),
    ("ljustru", "люстру"), ("zelenij", "зеленый"), ("potolok", "потолок"), ("svet", "свет"),
])
def test_translit_picks_the_dictionary_reading(home, latin, expected):
    forms = VectorParser(home).forms
    assert normalize(latin, forms.keys()).tokens == (expected,)


def test_translit_without_dictionary_takes_first_by_table():
    assert translit_candidates("cvet") == ["цвет", "чвет"]
    assert translit_candidates("wi") == ["ши", "шы", "щи", "щы"]
    assert normalize("cerz").tokens == ("церз",)
    assert normalize("maj").tokens == ("май",) and normalize("ogonj").tokens == ("огонь",)


def test_typo_search_runs_after_translit_and_from_four_letters():
    forms = {"через": "через"}
    norm = normalize("cerz", forms.keys())           # «церз» — 2 правки, «черз» — 1
    (tok,) = morph(norm.tokens, forms, 4, norm.variants)
    assert (tok.lemma, tok.fuzzy) == ("через", True)
    (tok,) = morph(("черз",), forms, 5)              # прежний порог |x| >= 5 это слово не брал
    assert tok.lemma == "черз"


def test_translit_readings_with_different_meanings_stay_ambiguous():
    forms = {"шина": "A", "щека": "B"}
    norm = normalize("wika", forms.keys())           # шика / щика / шыка / щыка
    (tok,) = morph(norm.tokens, forms, 4, norm.variants)
    assert not tok.known and tok.alternatives == ("A", "B")


def test_translit_command_end_to_end(oko, adapter):
    out = oko.handle("vkljuci ljustru", OP, "s1")
    assert out.status == "EXECUTED" and out.intent.template == "включить люстра"
    assert out.intent.action.source == "BUILTIN" and adapter.state["lamps"]["power"] is True


# ---- предмет --------------------------------------------------------------------------------

@pytest.mark.parametrize("phrase", ["Включи лампу", "Включи лампы", "Включи лампочки", "vkljuci lampy"])
def test_bare_lamps_are_ambiguous(oko, adapter, phrase):
    out = oko.handle(phrase, OP, "s1")
    assert out.status == "CLARIFY" and out.request["missing_field"] == "target"
    assert out.intent.target.is_ambiguous
    assert out.request["candidates"] == ["lamps", "tv_amb"]
    assert adapter.sent == []


@pytest.mark.parametrize("phrase", [
    "Включи люстру", "Включи свет на потолке", "Включи потолочный свет", "Включи штангу",
    "Включи планку", "Включи верхний свет", "Включи лампы на потолке", "Включи лампы на штанге",
])
def test_ceiling_words_mean_lamps(home, phrase):
    intent = VectorParser(HomeConfig()).parse(phrase, OP)      # группа «свет» не зарегистрирована
    assert (intent.target.value, intent.modality) == ("lamps", "EXEC")


def test_lamps_of_backlight_mean_tv_amb(home):
    intent = VectorParser(home).parse("Включи лампы подсветки", OP)
    assert (intent.target.value, intent.modality) == ("tv_amb", "EXEC")


def test_single_light_source_makes_lamps_unambiguous():
    home = HomeConfig(devices={"lamps": frozenset({"on", "off"})})
    intent = VectorParser(home).parse("Включи лампы", OP)
    assert (intent.target.value, intent.target.source) == ("lamps", "CONFIG")


def test_floor_lamp_is_not_builtin_and_comes_only_from_teaching(oko):
    out = oko.handle("Включи торшер", OP, "s1")
    assert out.status == "CLARIFY" and out.intent.target.is_unknown
    oko.remember(oko.resolve(out.request["request_id"], {"target": "lamps"}).command_id)
    out = oko.handle("Включи торшер", OP, "s2")
    assert out.status == "EXECUTED" and out.intent.target.source == "G6"


def test_operator_habit_resolves_bare_lamps_after_crystallization(oko):
    teach(oko, "Включи лампы", {"target": "tv_amb"})
    out = oko.handle("Включи лампы", OP, "s3")
    assert out.status == "EXECUTED"
    assert (out.intent.target.value, out.intent.target.source) == ("tv_amb", "G6")


def test_translit_and_typo_resolve_as_fuzzy(oko):
    out = oko.handle("vkluchi svet", OP, "s1")
    assert out.status == "EXECUTED" and out.intent.action.source == "FUZZY"
    assert out.intent.template == "включить свет"


def test_parameters_and_rooms_split_patterns(oko):
    oko.handle("Включи люстру", OP, "s1")
    oko.handle("Включи люстру на 30", OP, "s1")
    oko.handle("Включи люстру на 70%", OP, "s1")
    oko.handle("Включи люстру в спальне", OP, "s1")
    oko.handle("Включи люстру на кухне", OP, "s1")
    templates = sorted(r.canonical_template for r in oko.repo.list(OP))
    assert templates == ["LAMPS + ON", "LAMPS + ON room=bedroom", "LAMPS + ON room=kitchen", "LAMPS + ON {level}"]


def test_vector_encoding(home):
    parser = VectorParser(home)
    space = VectorSpace(rooms=home.rooms, devices=())
    known = space.encode(parser.parse("Включи люстру на 50", OP))
    unknown = space.encode(parser.parse("Погромче", OP))
    assert len(known) == len(unknown) == space.dim
    assert known[2] == 1.0 and known[15:17] == [0.5, 0.0]          # lamps; уровень задан
    assert unknown[5] == 1.0 and any(unknown)                      # ⊥_T — отдельное состояние, не ноль
    assert unknown[15:17] == [0.0, 1.0]                            # уровень не задан


# ---- обучение -------------------------------------------------------------------------------

def test_no_learning_without_operator_validation(oko):
    for s in ("s1", "s2", "s3", "s4"):
        oko.handle("Включи свет", OP, s)
    (rec,) = oko.repo.list(OP)
    assert (rec.observations, rec.verified_executions, rec.learned) == (4, 4, 0)
    assert rec.status == "CANDIDATE"
    assert oko.monitor.accuracy(OP, 20)[1] == 0                    # молчание Оператора — не метка


def test_acknowledged_is_not_verified(oko, adapter):
    adapter.no_telemetry.add("tv_screen")
    out = teach(oko, "Вруби телек", {"target": "tv_screen"})
    assert out.execution.status == "ACKNOWLEDGED"
    rec = oko.repo.get(oko.engine.pattern_id(OP, "tv_screen", "on", {}))
    assert (rec.confirmed_correct, rec.learned, rec.status, rec.aliases) == (0, 0, "CANDIDATE", [])


def test_state_mismatch_is_not_verified(oko, adapter):
    adapter.stuck.add("lamps")
    out = oko.handle("Включи свет", OP, "s1")
    assert out.status == "FAILED" and out.execution.status == "MISMATCH"
    assert ev.INFRA_INCIDENT in types(oko)


def test_wrong_mapping_with_successful_device_counts_against_parser(oko, adapter):
    out = oko.handle("Включи свет", OP, "s1")
    assert out.execution.status == "VERIFIED"                      # лампы включились…
    fixed = oko.confirm(out.command_id, correct=False, corrected={"target": "tv_amb"})
    assert fixed.status == "EXECUTED" and adapter.state["tv_amb"]["power"] is True
    wrong = oko.repo.get(oko.engine.pattern_id(OP, "lamps", "on", {}))
    right = oko.repo.get(oko.engine.pattern_id(OP, "tv_amb", "on", {}))
    assert (wrong.confirmed_incorrect, wrong.status) == (1, "DEGRADED")   # …но сопоставление неверно
    assert (right.confirmed_correct, right.status) == (0, "CANDIDATE")    # исправление — не обучение
    assert oko.remember(fixed.command_id).status == "REMEMBERED"
    right = oko.repo.get(right.pattern_id)
    assert (right.confirmed_correct, right.learned, right.status) == (1, 1, "CRYSTALLIZED")
    with pytest.raises(ValueError):
        oko.remember(out.command_id)                                  # отвергнутое не запоминается
    assert oko.monitor.accuracy(OP, 20) == (0.0, 1)
    with pytest.raises(ValueError):
        oko.confirm(out.command_id, correct=True)


def test_memory_is_personal(oko):
    teach(oko, "Вруби телек", {"target": "tv_screen"})
    assert oko.handle("Вруби телек", OP, "s3").status == "EXECUTED"
    assert oko.handle("Вруби телек", "operator-2", "s3").status == "CLARIFY"
    assert oko.repo.list("operator-2") == []


def test_alias_conflict_blocks_auto_execution(oko, adapter):
    teach(oko, "Вруби телек", {"target": "tv_screen"})
    engine = oko.engine
    for s in ("s1", "s1", "s2"):       # та же формулировка подтверждена и для другой цели
        engine.record(Evidence(OP, "tv_amb", "on", "врубить телек", session_id=s, validated=True,
                               exec_status="VERIFIED", remember=True))
    assert engine.aliases.conflicts(OP) == ["врубить телек"]
    adapter.sent.clear()
    out = oko.handle("Вруби телек", OP, "s3")
    assert out.status == "CLARIFY" and out.intent.target.is_ambiguous
    assert sorted(out.request["candidates"]) == ["tv_amb", "tv_screen"]
    assert adapter.sent == []


def test_bind_limit_asks_before_evicting_the_oldest(home, adapter, clock):
    oko = Vinaigrette(open_db(), home, adapter, settings=Settings(bind_limit=2), clock=clock)
    first = teach(oko, "Вруби телек", {"target": "tv_screen"}, sessions=("s1",))
    teach(oko, "Включи торшер", {"target": "lamps"}, sessions=("s1",))
    assert oko.handle("Вруби телек", OP, "s1").status == "EXECUTED"   # телек использован позже торшера
    assert oko.engine.bind_count(OP) == 2
    out = oko.handle("Включи подсветку", OP, "s1")
    assert out.status == "EXECUTED"
    ask = oko.remember(out.command_id)
    assert ask.status == "EVICT" and ask.request["event"] == ev.EVICT_REQUEST
    assert ask.request["text"] == "Таблица связок полна: 2. Самая старая: «включить торшер». Заменить?"
    assert oko.engine.bind_count(OP) == 2 and ev.BIND_EVICTED not in types(oko)   # молча не вытесняется

    done = oko.resolve(ask.request["request_id"])
    assert done.status == "REMEMBERED" and oko.engine.bind_count(OP) == 2
    templates = {a["template"] for r in oko.repo.list(OP) for a in r.aliases}
    assert "включить торшер" not in templates and "врубить телек" in templates
    (evt,) = oko.registry.events(OP, (ev.BIND_EVICTED,))
    assert evt.payload["template"] == "включить торшер" and evt.payload["reason"] == "lru"
    assert evt.payload["n"] == 2 and evt.payload["limit"] == 2
    n_events = len(oko.registry.events(OP))
    assert oko.remember(out.command_id).status == "ALREADY"          # повтор нажатия
    assert len(oko.registry.events(OP)) == n_events

    before = oko.repo.list(OP)
    oko.engine.recount(OP)                                     # вытеснение переигрывается из журнала
    assert oko.repo.list(OP) == before
    assert first.command_id


def test_declined_eviction_keeps_table_and_saves_nothing(home, adapter, clock):
    oko = Vinaigrette(open_db(), home, adapter, settings=Settings(bind_limit=1), clock=clock)
    teach(oko, "Вруби телек", {"target": "tv_screen"}, sessions=("s1",))
    out = oko.handle("Включи свет", OP, "s1")
    ask = oko.remember(out.command_id)
    assert ask.status == "EVICT"
    assert oko.cancel(ask.request["request_id"]).status == "CANCELLED"
    assert [a["template"] for r in oko.repo.list(OP) for a in r.aliases] == ["врубить телек"]
    assert ev.BIND_EVICTED not in types(oko)
    with pytest.raises(ValueError):
        oko.resolve(oko.remember(out.command_id).request["request_id"], {"target": "lamps"})


def test_eviction_question_expires(home, adapter, clock):
    oko = Vinaigrette(open_db(), home, adapter, settings=Settings(bind_limit=1), clock=clock)
    teach(oko, "Вруби телек", {"target": "tv_screen"}, sessions=("s1",))
    out = oko.handle("Включи свет", OP, "s1")
    ask = oko.remember(out.command_id)
    clock.t += dt.timedelta(seconds=91)
    assert oko.resolve(ask.request["request_id"]).status == "EXPIRED"
    assert oko.engine.bind_count(OP) == 1 and ev.BIND_EVICTED not in types(oko)


def test_engine_refuses_bind_over_limit_without_journal(home, adapter):
    from oko.memory import BindTableFull
    oko = Vinaigrette(open_db(), home, adapter, settings=Settings(bind_limit=1))
    oko.engine.record(Evidence(OP, "lamps", "on", "a", exec_status="VERIFIED", remember=True))
    n = len(oko.registry.events(OP))
    with pytest.raises(BindTableFull) as e:
        oko.engine.record(Evidence(OP, "lamps", "on", "b", exec_status="VERIFIED", remember=True))
    assert (e.value.n, e.value.template) == (1, "a") and len(oko.registry.events(OP)) == n
    oko.engine.record(Evidence(OP, "lamps", "on", "a", exec_status="VERIFIED", remember=True))   # повтор — не новая
    oko.engine.record(Evidence(OP, "lamps", "on", "b", exec_status="VERIFIED"))                  # наблюдение — не связка
    assert oko.engine.bind_count(OP) == 1


def test_lru_uses_last_use_not_creation(home, adapter):
    oko = Vinaigrette(open_db(), home, adapter, settings=Settings(bind_limit=2))
    for t in ("a", "b"):
        oko.engine.record(Evidence(OP, "lamps", "on", t, exec_status="VERIFIED", remember=True))
    oko.engine.record(Evidence(OP, "lamps", "on", "a", exec_status="VERIFIED"))   # «a» использована позже
    from oko.memory import BindTableFull
    with pytest.raises(BindTableFull) as e:
        oko.engine.record(Evidence(OP, "lamps", "on", "c", exec_status="VERIFIED", remember=True))
    assert e.value.template == "b"


def test_rollback_across_eviction_restores_binding(home, adapter, clock):
    oko = Vinaigrette(open_db(), home, adapter, settings=Settings(bind_limit=1), clock=clock)
    teach(oko, "Вруби телек", {"target": "tv_screen"}, sessions=("s1",))
    kept = oko.repo.version(OP)
    out = oko.handle("Включи свет", OP, "s1")
    oko.resolve(oko.remember(out.command_id).request["request_id"])
    assert [a["template"] for r in oko.repo.list(OP) for a in r.aliases] == ["включить свет"]
    oko.rollback(OP, kept)
    assert [a["template"] for r in oko.repo.list(OP) for a in r.aliases] == ["врубить телек"]
    restored = oko.repo.list(OP)
    oko.engine.recount(OP)                                     # аннулированное вытеснение не переигрывается
    assert oko.repo.list(OP) == restored


def test_confirm_after_remember_is_refused(oko):
    out = oko.handle("Включи свет", OP, "s1")
    oko.remember(out.command_id)
    with pytest.raises(ValueError):
        oko.confirm(out.command_id, correct=True)
    (rec,) = oko.repo.list(OP)
    assert rec.confirmed_correct == 1


def test_lru_does_not_depend_on_clock_resolution(home, adapter):
    frozen = dt.datetime(2026, 11, 11, 10, 28, tzinfo=dt.timezone.utc)
    oko = Vinaigrette(open_db(), home, adapter, settings=Settings(bind_limit=2), clock=lambda: frozen)
    for t in ("b", "a"):                                       # все метки времени равны, как на Windows
        oko.engine.record(Evidence(OP, "lamps", "on", t, exec_status="VERIFIED", remember=True))
    oko.engine.record(Evidence(OP, "lamps", "on", "b", exec_status="VERIFIED"))
    from oko.memory import BindTableFull
    with pytest.raises(BindTableFull) as e:
        oko.engine.record(Evidence(OP, "lamps", "on", "c", exec_status="VERIFIED", remember=True))
    assert e.value.template == "a"


def test_replace_is_one_transaction(home, adapter, clock, tmp_path, monkeypatch):
    path = str(tmp_path / "g6.db")
    oko = Vinaigrette(open_db(path), home, adapter, settings=Settings(bind_limit=1), clock=clock)
    teach(oko, "Вруби телек", {"target": "tv_screen"})
    out = oko.handle("Включи свет", OP, "s1")
    ask = oko.remember(out.command_id)
    real = oko.engine.record

    def boom(evidence, frozen=False):
        if evidence.remember:
            raise RuntimeError("disk")
        return real(evidence, frozen)
    monkeypatch.setattr(oko.engine, "record", boom)
    with pytest.raises(RuntimeError):
        oko.resolve(ask.request["request_id"])
    fresh = Vinaigrette(open_db(path), home, adapter, settings=Settings(bind_limit=1), clock=clock)
    assert [a["template"] for r in fresh.repo.list(OP) for a in r.aliases] == ["врубить телек"]
    assert ev.BIND_EVICTED not in types(fresh)


def test_verified_of_another_command_or_device_gives_no_binding(oko, adapter):
    """REVIEW 11.11 §4: VERIFIED чужой команды / устройства связку не даёт."""
    ok = oko.handle("Включи подсветку", OP, "s1")                 # tv_amb: VERIFIED
    adapter.unreachable.add("lamps")
    bad = oko.handle("Включи люстру", OP, "s1")                    # lamps: FAILED
    assert (ok.execution.status, bad.execution.status) == ("VERIFIED", "FAILED")
    assert oko.remember(bad.command_id).status == "NOT_VERIFIED"   # чужой VERIFIED не засчитан
    lamps = oko.repo.get(oko.engine.pattern_id(OP, "lamps", "on", {}))
    assert lamps.aliases == [] and lamps.learned == 0
    assert oko.remember(ok.command_id).status == "REMEMBERED"
    (evt,) = [e for e in oko.registry.events(OP, (ev.EVIDENCE,)) if e.payload.get("remember")]
    assert evt.command_id == ok.command_id and evt.payload["target"] == "tv_amb"
    with pytest.raises(KeyError):
        oko.remember("no-such-command")                            # выдуманный command_id


def test_default_bind_limit_is_500():
    assert Settings().bind_limit == 500


def test_choice_in_clarify_does_not_learn(oko):
    teach(oko, "Вруби телек", {"target": "tv_screen"}, remember=False)
    rec = oko.repo.get(oko.engine.pattern_id(OP, "tv_screen", "on", {}))
    assert (rec.learned, rec.aliases, rec.status) == (0, [], "CANDIDATE")
    assert ev.ALIAS_ADDED not in types(oko)
    assert oko.handle("Вруби телек", OP, "s3").status == "CLARIFY"


def test_crystallization_law():
    c = CrystallizationEngine()
    assert c.quality(0, 0) == 0.5 and c.quality(3, 0) == pytest.approx(0.8)
    assert c.learn(True, True, True, True)
    assert not c.learn(True, True, True, False) and not c.learn(False, True, True, True)


# ---- калибровка -----------------------------------------------------------------------------

def degrade(oko):
    """Оператор отвергает сопоставления, пока точность на окне не упадёт до порога."""
    for _ in range(20):
        if oko.calibration.mode(OP) == DEGRADED_MODE:
            return
        out = oko.handle("Включи свет", OP, "s1")
        oko.confirm(out.command_id, correct=False)
    raise AssertionError("calibration was not triggered")


def test_below_min_valid_no_calibration(oko):
    for _ in range(9):
        out = oko.handle("Включи свет", OP, "s1")
        oko.confirm(out.command_id, correct=False)
    assert oko.calibration.mode(OP) == NORMAL_MODE


def test_degraded_blocks_aliases_freezes_promotion_and_requires_confirmation(oko, adapter):
    teach(oko, "Вруби телек", {"target": "tv_screen"})
    assert oko.handle("Вруби телек", OP, "s3").status == "EXECUTED"
    degrade(oko)
    adapter.sent.clear()

    out = oko.handle("Вруби телек", OP, "s4")           # обученный псевдоним больше не применяется
    assert out.status == "CLARIFY" and adapter.sent == []

    out = oko.handle("Включи подсветку", OP, "s4")      # полная команда ждёт ручного подтверждения
    assert out.status == "CONFIRM" and adapter.sent == []
    assert out.request["event"] == ev.CONFIRM_REQUEST
    for s in ("s4", "s5", "s6"):
        if out is None:
            out = oko.handle("Включи подсветку", OP, s)
        done = oko.resolve(out.request["request_id"])
        assert done.status == "EXECUTED"
        assert oko.remember(done.command_id).status == "REMEMBERED"
        out = None
    rec = oko.repo.get(oko.engine.pattern_id(OP, "tv_amb", "on", {}))
    assert rec.learned == 3 and rec.session_count == 3
    assert rec.status == "CANDIDATE"                    # повышение статусов заморожено


def test_recovery_resumes_and_releases_frozen_promotions(oko):
    degrade(oko)
    for i in range(10):
        out = oko.handle("Включи подсветку", OP, f"r{i}")
        assert out.status == "CONFIRM"
        oko.remember(oko.resolve(out.request["request_id"]).command_id)
    assert oko.calibration.mode(OP) == NORMAL_MODE
    assert ev.CALIBRATION_RESUMED in types(oko)
    rec = oko.repo.get(oko.engine.pattern_id(OP, "tv_amb", "on", {}))
    assert rec.status == "CRYSTALLIZED"
    out = oko.handle("Включи подсветку", OP, "s9")      # старые промахи окно больше не тянут
    assert out.status == "EXECUTED" and oko.calibration.mode(OP) == NORMAL_MODE


def test_correction_during_confirmation_keeps_system_degraded(oko, adapter):
    degrade(oko)
    for i in range(10):
        out = oko.handle("Включи подсветку", OP, "s1")
        answers = {"target": "lamps"} if i % 2 else None
        oko.resolve(out.request["request_id"], answers)
    assert oko.monitor.accuracy(OP, 10) == (0.5, 10)
    assert oko.calibration.mode(OP) == DEGRADED_MODE


# ---- версии, откат, пересчёт ----------------------------------------------------------------

def test_memory_versions_and_rollback(oko):
    teach(oko, "Вруби телек", {"target": "tv_screen"})
    pid = oko.engine.pattern_id(OP, "tv_screen", "on", {})
    versions = oko.repo.versions(OP)
    assert [v["reason"] for v in versions] == [ev.PATTERN_CREATED, ev.PATTERN_STATUS]   # CANDIDATE -> CRYSTALLIZED
    assert oko.repo.get(pid).status == "CRYSTALLIZED"

    oko.rollback(OP, 1)
    rec = oko.repo.get(pid)
    assert (rec.status, rec.learned, rec.aliases) == ("CANDIDATE", 0, [])
    assert oko.handle("Вруби телек", OP, "s3").status == "CLARIFY"
    assert ev.MEMORY_ROLLBACK in types(oko) and oko.repo.version(OP) == 3
    with pytest.raises(KeyError):
        oko.rollback(OP, 99)


def test_recount_reproduces_memory_and_respects_rollback(oko):
    teach(oko, "Вруби телек", {"target": "tv_screen"})
    oko.handle("Включи свет", OP, "s1")
    before = oko.repo.list(OP)
    oko.engine.recount(OP)
    assert oko.repo.list(OP) == before                  # G6 — производное состояние журнала

    oko.rollback(OP, 1)
    after_rollback = oko.repo.list(OP)
    oko.engine.recount(OP)
    assert oko.repo.list(OP) == after_rollback          # аннулированные свидетельства не возвращаются


def test_every_transition_is_registered(oko):
    out = oko.handle("Погромче", OP, "s1")
    oko.resolve(out.request["request_id"], {"target": "audio"})
    assert types(oko) == [
        ev.OPERATOR_INPUT, ev.INTENT_PARSED, ev.CLARIFY_REQUEST, ev.OPERATOR_RESPONSE, ev.EXECUTION,
        ev.EVIDENCE, ev.PATTERN_CREATED, ev.VALIDATION]
    oko.remember(out.command_id)
    assert types(oko)[-3:] == [ev.EVIDENCE, ev.ALIAS_ADDED, ev.PATTERN_STATUS]
    command_ids = {e.command_id for e in oko.registry.events(OP)}
    assert command_ids == {out.command_id}


# ---- потенциальные паттерны -----------------------------------------------------------------

def test_smoothed_probability_and_entropy():
    assert smoothed(3, 4, 1.0, 2) == pytest.approx(4 / 6)
    assert conditional_entropy({"evening": {"a": 5}}) == 0.0                 # Оператор предсказуем
    assert conditional_entropy({"evening": {"a": 2, "b": 2}}) == pytest.approx(1.0)
    assert conditional_entropy({}) == 0.0


def test_frequency_ranks_candidates_but_never_executes(oko, adapter):
    evening = {"daypart": "evening"}
    out = oko.handle("Погромче", OP, "s1", evening)
    oko.remember(oko.resolve(out.request["request_id"], {"target": "tv_screen"}).command_id)
    pid = oko.engine.pattern_id(OP, "tv_screen", "up", {})
    assert oko.confidence.probability(OP, "daypart=evening", pid) == pytest.approx(1.0)
    assert oko.confidence.entropy(OP) == 0.0
    adapter.sent.clear()
    out = oko.handle("Потише", OP, "s5", evening)       # другая реплика, цель по-прежнему не названа
    assert out.status == "CLARIFY" and adapter.sent == []
    out = oko.handle("Сделай погромче", OP, "s5", evening)
    assert out.status == "CLARIFY" and out.request["candidates"][0] == "tv_screen"
    assert adapter.sent == []


# ---- срок жизни запроса ---------------------------------------------------------------------

def test_pending_request_expires_after_ttl(oko, adapter, clock):
    out = oko.handle("Погромче", OP, "s1")
    rid = out.request["request_id"]
    clock.t += dt.timedelta(seconds=91)
    late = oko.resolve(rid, {"target": "audio"})
    assert late.status == "EXPIRED" and late.execution is None
    assert adapter.sent == []
    (event,) = oko.registry.events(OP, (ev.REQUEST_EXPIRED,))
    assert event.payload == {"request_id": rid, "kind": "CLARIFY", "ttl_s": 90.0}
    assert event.command_id == out.command_id
    assert ev.EXECUTION not in types(oko) and oko.repo.list(OP) == []
    with pytest.raises(KeyError):
        oko.resolve(rid, {"target": "audio"})


def test_pending_request_is_valid_within_ttl(oko, clock):
    out = oko.handle("Погромче", OP, "s1")
    clock.t += dt.timedelta(seconds=89)
    assert oko.resolve(out.request["request_id"], {"target": "audio"}).status == "EXECUTED"


def test_stale_requests_are_swept_on_next_input(oko, clock):
    out = oko.handle("Погромче", OP, "s1")
    clock.t += dt.timedelta(seconds=120)
    oko.handle("Включи свет", OP, "s1")
    assert [e.payload["request_id"] for e in oko.registry.events(OP, (ev.REQUEST_EXPIRED,))] == [
        out.request["request_id"]]
    with pytest.raises(KeyError):
        oko.resolve(out.request["request_id"], {"target": "audio"})


# ---- опасные команды ------------------------------------------------------------------------

def test_tv_off_always_goes_through_confirm(oko, adapter):
    adapter.state["tv_screen"]["power"] = True
    out = oko.handle("Выключи телевизор", OP, "s1")
    assert out.status == "CONFIRM" and out.request["reason"] == "needs_confirm"
    assert adapter.sent == [] and adapter.state["tv_screen"]["power"] is True
    out = oko.resolve(out.request["request_id"])
    assert out.status == "EXECUTED" and adapter.state["tv_screen"]["power"] is False
    assert oko.handle("Включи телевизор", OP, "s1").status == "EXECUTED"      # включение не опасно


def test_crystallized_tv_off_still_needs_confirm(oko, adapter):
    out = oko.handle("Выруби телек", OP, "s1")
    assert out.status == "CLARIFY"
    out = oko.resolve(out.request["request_id"], {"target": "tv_screen"})
    assert out.status == "CONFIRM" and adapter.sent == []         # ответ на уточнение — ещё не согласие
    done = oko.resolve(out.request["request_id"])
    assert done.status == "EXECUTED"
    oko.remember(done.command_id)
    rec = oko.repo.get(oko.engine.pattern_id(OP, "tv_screen", "off", {}))
    assert rec.status == "CRYSTALLIZED"

    adapter.sent.clear()
    adapter.state["tv_screen"]["power"] = True
    out = oko.handle("Выруби телек", OP, "s3")
    assert out.intent.target.source == "G6"
    assert out.status == "CONFIRM" and out.request["reason"] == "needs_confirm"
    assert adapter.sent == [] and adapter.state["tv_screen"]["power"] is True


def test_declined_confirm_executes_nothing(oko, adapter):
    out = oko.handle("Выключи телевизор", OP, "s1")
    assert oko.cancel(out.request["request_id"]).status == "CANCELLED"
    assert adapter.sent == [] and ev.REQUEST_CANCELLED in types(oko)
    with pytest.raises(KeyError):
        oko.resolve(out.request["request_id"])


def test_correction_into_dangerous_command_needs_confirm(oko, adapter):
    out = oko.handle("Выключи подсветку", OP, "s1")
    assert out.status == "EXECUTED"
    adapter.sent.clear()
    out = oko.confirm(out.command_id, correct=False, corrected={"target": "tv_screen"})
    assert out.status == "CONFIRM" and adapter.sent == []


def test_router_refuses_unconfirmed_dangerous_command(oko, adapter):
    command = oko.router.build(oko.parser.parse("Выключи телевизор", OP))
    assert command.needs_confirm
    with pytest.raises(PermissionError):
        oko.router.execute(command)
    assert adapter.sent == []
