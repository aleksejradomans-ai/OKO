"""stt (28.10.2026): выбор устройства с ПРОВЕРКОЙ (cuda грузится, но падает на распознавании -> cpu),
склейка сегментов, честная недоступность. Настоящая модель не грузится — фабрика подменена."""
import pytest

pytest.importorskip("numpy")
import stt


class Seg:
    def __init__(self, t):
        self.text = t


class FakeModel:
    def __init__(self, device, fail_on_transcribe=False, text=(" Вниз", " три. ")):
        self.device, self.fail, self.text = device, fail_on_transcribe, text
        self.calls = []

    def transcribe(self, audio, **kw):
        self.calls.append(kw)
        if self.fail:
            raise RuntimeError("Library cublas64_12.dll is not found")
        return iter([Seg(t) for t in self.text]), None


@pytest.fixture(autouse=True)
def fresh(monkeypatch):
    stt.reset()
    monkeypatch.delenv("OKO_STT_DEVICE", raising=False)
    yield
    stt.reset()


def test_cuda_that_fails_on_first_transcribe_falls_back_to_cpu():
    made = []

    def factory(name, device, compute):
        m = FakeModel(device, fail_on_transcribe=(device == "cuda"))
        made.append((device, compute))
        return m

    assert stt.transcribe("x.ogg", factory=factory) == "Вниз три."
    assert made == [("cuda", "float16"), ("cpu", "int8")] and stt.device() == "cpu/int8"


def test_cuda_works_used_and_cached():
    made = []

    def factory(name, device, compute):
        made.append(device)
        return FakeModel(device)

    stt.transcribe("a.ogg", factory=factory)
    stt.transcribe("b.ogg", factory=factory)
    assert made == ["cuda"] and stt.device() == "cuda/float16"


def test_transcribe_uses_russian_vad_and_command_prompt():
    m = FakeModel("cpu")
    stt.transcribe("a.ogg", factory=lambda *a: m)
    kw = m.calls[-1]
    assert kw["language"] == "ru" and kw["vad_filter"] is True and "вниз" in kw["initial_prompt"]


def test_forced_cpu_does_not_try_cuda(monkeypatch):
    monkeypatch.setenv("OKO_STT_DEVICE", "cpu")
    made = []
    stt.transcribe("a.ogg", factory=lambda n, d, c: made.append(d) or FakeModel(d))
    assert made == ["cpu"]


def test_nothing_loads_is_unavailable_with_reasons():
    def factory(name, device, compute):
        raise OSError(f"no {device}")

    with pytest.raises(stt.SttUnavailable) as ei:
        stt.transcribe("a.ogg", factory=factory)
    assert "cuda" in str(ei.value) and "cpu" in str(ei.value)


def test_silence_gives_empty_text():
    assert stt.transcribe("a.ogg", factory=lambda *a: FakeModel("cpu", text=())) == ""
