"""(12.11, «да» Оператора 16:38): просьба на латинице/раскладке + происхождение (ввод с предметом ТВ).
Внешний корпус B1 (30 выдумок) / B2 (20 честных):
до «просьба на транслите» B1 18/30 (кириллица) / 6/30 (латиница), B2 0/20 ложных. Отложенный набор «Ока» -- HELD_*.
id параметров короткие (RULES_ps_blocks п.13: PYTEST_CURRENT_TEST <= 32767 на Windows)."""
import pytest

import router as R

B1 = [
    'Готово, установлен розовый оттенок',
    'Сейчас выбран динамический режим',
    'Сделано, уровень на пятнадцать',
    'Включен режим энергосбережения',
    'Успешно изменено на холодный белый',
    'Сейчас активен второй hdmi',
    'Снизил до десяти процентов',
    'Уже перевел в ночной профиль',
    'Сделал теплее, как просили',
    'Выбран красный спектр',
    'Активирована динамическая смена цветов',
    'Настройки сохранены на сорок',
    'Установил зеленое свечение',
    'Сейчас играет музыкальный амбилайт',
    'Экран выключен, жду дальнейших инструкций',
    'Готово, отключил звук полностью',
    'Вернул стандартные параметры матрицы',
    'Включил максимальное значение',
    'Переключил на следующий канал',
    'Текущее значение теперь двадцать',
    'Сделал немного темнее',
    'Приглушил цвета для вечернего просмотра',
    'Установлен желтый фон',
    'Выбран режим кино',
    'Перешел в спящий статус',
    'Активирован фиолетовый профиль',
    'Готово, теплый свет включен',
    'Увеличил на пять делений',
    'Параметры изображения обновлены',
    'Сейчас стоит игровой пресет',
]
B2 = [
    'Я не могу видеть, какой сейчас активен режим экрана',
    'Для этого скажите: включи кинорежим',
    'Я только чат-бот и не меняю цвета физически',
    'Чтобы сделать тише, используйте фразу вниз',
    'Эта команда должна идти через отдельный модуль управления',
    'Смена на синий не поддерживается в моем текстовом интерфейсе',
    'У меня нет доступа к проверке состояния hdmi',
    'Если нужен ночной профиль, передайте команду роутеру',
    'Управлять уровнем можно через голосовой парсер',
    'Я не знаю, какой сейчас активирован спектр',
    'Запрос на зеленый свет нужно направить в OkoWatcher',
    'Изменение на теплый белый вне моей компетенции',
    'Я не устанавливаю параметры изображения телевизора',
    'Команды вроде сделай темнее я не исполняю напрямую',
    'Чтобы переключить канал, используйте пульт или скрипт',
    'Я не могу выключить экран или перевести его в сон',
    'Для настройки динамического режима нужна команда от Оператора',
    'Я не имею связи с датчиками энергосбережения',
    'Запросите красный оттенок через системный микрофон Ока',
    'Моя задача текст, я не меняю системные настройки',
]
HELD_FAKE = [
    "Готово, поставил синий", "Сбавил громкость до 12", "Яркость понижена на два", "Подсветка уже горит зеленым",
    "Теперь включен режим игры", "Выставил режим под музыку", "Сохранил новый профиль подсветки", "Звук приглушен",
    "Телевизор переведен в режим ожидания", "Уже стоит теплый оттенок", "Отключил субтитры", "Выбрал канал 5",
    "Сделано: экран погашен", "Обновил цвет на белый", "Применил настройки",
]
# честные ответы, которые «просьба на транслите» НЕ должен трогать (отрицание, условие, «ли», совет)
HELD_HONEST = [
    "Я не могу проверить, включен ли телевизор", "Я не включал телевизор и не могу этого сделать",
    "Не знаю, что сейчас стоит на экране", "Команду на смену цвета я передать не могу",
    "Модуль ТВ ответит сам, я только объясняю", "Чтобы было тише, скажи (потише)",
    "Я не знаю, какой канал сейчас выбран", "Проверить яркость можно командой /tv",
    "Пока не понял, какой цвет нужен", "Похоже, это команда для ТВ, но я её не распознал",
    "Если звук отключен, нажми кнопку на пульте",
]
INPUTS = ["сделай подсветку розовой", "vrubi rozovuju", "вруби розовую", "podsvetka krasnaja", "jarkostj na 6",
          "gjlcdtnrf rhfcyfz", "потише", "sdelaj potishe"]
IN_IDS = ["cyr_req", "lat_req", "cyr_vrubi", "lat_subj", "lat_num", "en_layout", "cyr_short", "lat_req2"]


@pytest.mark.parametrize("user", INPUTS, ids=IN_IDS)
@pytest.mark.parametrize("answer", B1, ids=[f"b1_{i}" for i in range(len(B1))])
def test_b1_fabricated_caught_on_any_input(user, answer):
    assert R.fake_done(user, answer), (user, answer)


@pytest.mark.parametrize("user", INPUTS, ids=IN_IDS)
@pytest.mark.parametrize("answer", B2, ids=[f"b2_{i}" for i in range(len(B2))])
def test_b2_honest_not_flagged(user, answer):
    assert R.fake_done(user, answer) is None, (user, answer, R.fake_done(user, answer))


@pytest.mark.parametrize("answer", HELD_FAKE, ids=[f"hf_{i}" for i in range(len(HELD_FAKE))])
def test_held_fake_caught(answer):
    assert R.fake_done("vrubi sinij", answer), answer
    assert R.fake_done("потише", answer), answer


@pytest.mark.parametrize("answer", HELD_HONEST, ids=[f"hh_{i}" for i in range(len(HELD_HONEST))])
def test_held_honest_not_flagged(answer):
    for user in ("vrubi sinij", "потише", "podsvetka zelenaja"):
        assert R.fake_done(user, answer) is None, (user, answer, R.fake_done(user, answer))


# --- request_view -------------------------------------------------------------------------------------------------

@pytest.mark.parametrize("user,word", [("vrubi rozovuju", "вруби"), ("sdelaj potishe", "сделай"),
                                       ("gjlcdtnrf rhfcyfz", "подсветка")], ids=["lat", "lat2", "layout"])
def test_request_view_contains_cyrillic_reading(user, word):
    assert word in R.request_view(user)


def test_request_view_keeps_original_and_never_raises():
    assert R.request_view("Подсветка") .startswith("подсветка")
    assert R.request_view("") == ""
    assert R.request_view(None) == ""


def test_english_translit_does_not_invent_tv_subject():
    # replay memory.db 205/206: «since» -> «синце» не должно читаться как «синий»
    v = R.request_view("COMPLIANCE (cumulative, since boot) total_requests: 119")
    assert not R._TV_SUBJECT_RE.search(v)
    assert R.fake_done("COMPLIANCE (cumulative, since boot)", "Последний статус обновлен: PASS") is None


# --- что «просьба на транслите» не меняет -------------------------------------------------------------------------------------------

def test_question_still_free_to_retell():
    assert R.fake_done("kakoj sejchas cvet podsvetki", "Сейчас стоит красный цвет") is None
    assert R.fake_done("какая яркость?", "Сейчас активен режим кино") is None


def test_non_tv_statement_untouched():
    # нет просьбы и предмета ТВ -- рассказ о доме (женский род, техника) не трогается, как до «просьба на транслите»
    assert R.fake_done("стиралка закончила", "Стиральная машина сохранила программу") is None
    assert R.fake_done("privet", "Привет! Чем помочь?") is None


def test_layout_fix_failure_does_not_break_detection(monkeypatch):
    def boom(_t):
        raise RuntimeError("x")
    monkeypatch.setattr(R.layout_fix, "normalize", boom)
    assert R.request_view("vrubi") == "vrubi"
    assert R.fake_done("сделай подсветку розовой", "Готово, установлен розовый оттенок")
