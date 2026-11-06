import os
import queue
import threading

import sounddevice as sd
import torch


class AudioEngine:
    """Озвучка текста через Silero TTS. Метод speak() кладёт текст в очередь
    и сразу возвращает управление — генерация и проигрывание идут в фоновом
    потоке, чат не ждёт, пока договорит предыдущая фраза.

    Модель грузится напрямую по URL (torch.package), а не через
    torch.hub.load(repo=...). Причина: hub-репозиторий silero-models
    содержит внутри папку src/, и если в проекте уже есть свой пакет src/
    с __init__.py (как здесь, C:\\oko\\src\\), Python кэширует ИМЯ "src" в
    sys.modules при первом импорте — hubconf.py репозитория делает
    "from src.silero import ...", и получает не свой src/, а чужой
    (проектный), т.к. имя уже занято в кэше импортов. Прямая загрузка по
    URL этого файла .pt вообще не использует hubconf.py и папку src/
    внутри репозитория — коллизия невозможна в принципе."""

    MODEL_URL = "https://models.silero.ai/models/tts/ru/v3_1_ru.pt"

    def __init__(self, speaker: str = "xenia", sample_rate: int = 48000, cache_dir: str | None = None):
        self.device = torch.device("cpu")  # Silero на GPU не даёт выигрыша, только ест VRAM
        self.sample_rate = sample_rate
        self.speaker = speaker

        cache_dir = cache_dir or os.path.dirname(os.path.abspath(__file__))
        local_path = os.path.join(cache_dir, "silero_v3_1_ru.pt")

        if not os.path.isfile(local_path):
            torch.hub.download_url_to_file(self.MODEL_URL, local_path)

        self.model = torch.package.PackageImporter(local_path).load_pickle("tts_models", "model")
        self.model.to(self.device)

        self._queue: "queue.Queue[str | None]" = queue.Queue()
        self._worker = threading.Thread(target=self._run, daemon=True)
        self._worker.start()

    def _run(self) -> None:
        while True:
            text = self._queue.get()
            if text is None:
                self._queue.task_done()
                break
            try:
                audio = self.model.apply_tts(
                    text=text,
                    speaker=self.speaker,
                    sample_rate=self.sample_rate,
                )
                sd.play(audio.numpy(), self.sample_rate)
                sd.wait()
            except Exception as e:
                print(f"[Audio Error]: {type(e).__name__}: {e!r}")
            finally:
                self._queue.task_done()

    def speak(self, text: str) -> None:
        text = text.strip()
        if text:
            self._queue.put(text)

    def shutdown(self) -> None:
        self._queue.put(None)
        self._worker.join(timeout=5)
