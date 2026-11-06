import queue
import threading

import sounddevice as sd
import torch


class AudioEngine:
    """Озвучка текста через Silero TTS. Метод speak() кладёт текст в очередь
    и сразу возвращает управление — генерация и проигрывание идут в фоновом
    потоке, чат не ждёт, пока договорит предыдущая фраза."""

    def __init__(self, speaker: str = "xenia", model_id: str = "v3_1_ru", sample_rate: int = 48000):
        self.device = torch.device("cpu")  # Silero на GPU не даёт выигрыша, только ест VRAM
        self.sample_rate = sample_rate
        self.speaker = speaker

        self.model, _ = torch.hub.load(
            repo_or_dir="snakers4/silero-models",
            model="silero_tts",
            language="ru",
            speaker=model_id,
        )
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
                print(f"[Audio Error]: {e}")
            finally:
                self._queue.task_done()

    def speak(self, text: str) -> None:
        text = text.strip()
        if text:
            self._queue.put(text)

    def shutdown(self) -> None:
        self._queue.put(None)
        self._worker.join(timeout=5)
