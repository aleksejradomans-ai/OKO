"""stt: параллельная загрузка (прогрев + первое голосовое) и ограниченное ожидание распознавания (30.10.2026).
Модель подменяется фабрикой: faster-whisper и видеокарта не нужны."""
import sys
import threading
import time
import types

import pytest

import stt


class FakeModel:
    def __init__(self, device, slow=0.0):
        self.device = device
        self.slow = slow

    def transcribe(self, audio, **kw):
        time.sleep(self.slow)
        return iter([types.SimpleNamespace(text=" вниз ")]), None


@pytest.fixture(autouse=True)
def clean(monkeypatch):
    stt.reset()
    monkeypatch.setitem(sys.modules, "numpy", types.SimpleNamespace(zeros=lambda n, dtype=None: [0.0] * n))
    monkeypatch.setitem(sys.modules, "faster_whisper", types.SimpleNamespace())
    yield
    stt.reset()


def test_parallel_load_creates_model_once(monkeypatch):
    """Прогрев и голосовое одновременно: модель создаётся ОДИН раз, второй поток берёт готовую."""
    monkeypatch.setenv("OKO_STT_DEVICE", "cpu")
    made = []

    def factory(name, device, compute):
        made.append(device)
        return FakeModel(device, slow=0.3)        # проба идёт 0,3 с — окно гонки

    res = []
    ts = [threading.Thread(target=lambda: res.append(stt.load(factory))) for _ in range(2)]
    [t.start() for t in ts]
    [t.join(5) for t in ts]
    assert made == ["cpu"] and len(res) == 2 and res[0][0] is res[1][0]


def test_cuda_failure_remembered_without_factory(monkeypatch):
    monkeypatch.setenv("OKO_STT_DEVICE", "auto")
    calls = []

    def fake_default(name, device, compute):
        calls.append(device)
        if device == "cuda":
            raise RuntimeError("cublas64_12.dll is not found")
        return FakeModel(device)

    monkeypatch.setattr(stt, "_default_factory", fake_default)
    stt.load()
    assert calls == ["cuda", "cpu"] and stt._cuda_failed
    stt._model = None                              # модель забыта, но CUDA второй раз не пробуется
    stt.load()
    assert calls == ["cuda", "cpu", "cpu"]


def test_transcribe_lock_wait_is_bounded(monkeypatch, tmp_path):
    monkeypatch.setattr(stt, "LOCK_WAIT_S", 0.2)
    stt._lock.acquire()                             # «зависшее» предыдущее распознавание
    try:
        t0 = time.time()
        with pytest.raises(stt.SttUnavailable, match="ещё распознаётся"):
            stt.transcribe(tmp_path / "v.ogg", factory=lambda *a: FakeModel("cpu"))
        assert time.time() - t0 < 2
    finally:
        stt._lock.release()


def test_transcribe_releases_lock_on_error(monkeypatch, tmp_path):
    monkeypatch.setenv("OKO_STT_DEVICE", "cpu")

    class Bad(FakeModel):
        def transcribe(self, audio, **kw):
            if isinstance(audio, str):
                raise ValueError("битый файл")
            return super().transcribe(audio, **kw)

    with pytest.raises(ValueError):
        stt.transcribe(tmp_path / "v.ogg", factory=lambda *a: Bad("cpu"))
    assert stt._lock.acquire(timeout=0.1)
    stt._lock.release()
