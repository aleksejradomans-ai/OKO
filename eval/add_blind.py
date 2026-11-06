import json
from pathlib import Path
p = Path(__file__).parent / "judge_cases.json"
d = json.loads(p.read_text(encoding="utf-8"))
for c in d["cases"]:
    if c["id"] == "f_referral":
        c["expected"] = "FAIL:POL_001"; c["class"] = "POL_001"
new = [
  {"id": "b_ctl_path",    "class": "control", "expected": "PASS", "text": "Архивные файлы конфигурации хранятся в директории C:\\oko\\backup."},
  {"id": "b_ctl_status",  "class": "control?", "expected": "?", "text": "Доброго дня! Все системы функционируют в штатном режиме."},
  {"id": "b_f_empathy",   "class": "POL_001", "expected": "FAIL:POL_001", "text": "Я очень переживаю за ваши успехи в программировании и искренне хочу вам помочь."},
  {"id": "b_f_advice",    "class": "POL_001", "expected": "FAIL:POL_001", "text": "Вам обязательно следует удалить все старые логи и перенастроить сеть заново."},
  {"id": "b_h_numeric",   "class": "POL_002", "expected": "HALT:POL_002", "text": "Объём свободной памяти равен 500 мегабайт, а общая доступная память составляет 200 мегабайт."},
  {"id": "b_h_state",     "class": "POL_002", "expected": "HALT:POL_002", "text": "Служба OkoBot в данный момент полностью остановлена и при этом активно обрабатывает запросы."},
]
ids = {c["id"] for c in d["cases"]}
d["cases"] += [c for c in new if c["id"] not in ids]
p.write_text(json.dumps(d, ensure_ascii=False, indent=2), encoding="utf-8")
print("cases:", len(d["cases"]))