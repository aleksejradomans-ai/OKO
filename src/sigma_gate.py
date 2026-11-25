"""sigma_gate.py -- подключение ядра Σ к исполнению команд ТВ (фаза 3, этап 3d).

Решения Оператора 16.11: 14:51 (fail-safe), 16:16 (λ_0), 18:21 (заморозка D при A_t = 1), 20:23 (Active Probing); реестр проверок
(R-01, R-09, защёлка A_t, длина серии, заморозка D при A_t = 1).

ГДЕ ВЫЗЫВАЕТСЯ. Единственная точка, через которую уходит ЛЮБАЯ команда ТВ, -- tv_bot_handler._execute (B33 v2:
пути мимо шлюза -- /cam calib auto, мост подсветки, audio_bridge, повтор пробуждения, CLI -- берут клиента только через
sigma_live.tv_client и при OKO_SIGMA_LIVE=1 не пишут ничего; сторож src/test_b33v2_sigma_single_point.py): туда
приходят и маршрут router.run_tv (свободный текст / голос), и /tv, и связки bind. Вызов только из router.run_tv
оставил бы /tv и bind без Σ (bot.py зовёт tv_bot_handler.handle напрямую), поэтому шлюз стоит перед отправкой
в _execute; router.py не меняется (шлюз в одной точке исполнения, а не в маршрутизаторе).

РЕЖИМЫ (флаг окружения службы OKO_SIGMA_LIVE, читается из os.environ; .env не правится):
  OKO_SIGMA_LIVE != "1" (по умолчанию, ТЕНЕВОЙ режим, решение 15:36 п.2): вердикт Σ считается и пишется в журнал
      (поля sigma / sigma_p / sigma_why / a_t / sigma_live строки outcome), команда исполняется ПО-СТАРОМУ:
      тот же ответ, те же вызовы ТВ. Active Probing в теневом режиме не читает ТВ (лишние вызовы изменили бы
      поведение); тревогу ведут исходы самих команд. Любой сбой шлюза глушится.
  OKO_SIGMA_LIVE == "1" (боевой, только после решения Оператора по Θ_intent / Θ_margin / Θ_goal):
      HALT / CLARIFY / OBSERVE -> команда не отправляется; A_t = 1 -> Active Probing: ни одной команды записи,
      только контрольное чтение признака в пределах R_t; удачное (d_∞ ≤ ε_norm) -> событие δ = 0, неудачное /
      расхождение / сбой чтения -> δ = 1 (решение 20:23).

СОБЫТИЯ ИНТЕГРАТОРА (event_id = seq журнала, строго растущий, п.5):
  исход команды FAIL -> δ = 1; PASS (verified) и WARN «предел» -> δ = 0; слепая команда (WARN blind) и
  изолированный признак volume (изолированный volume) -> события нет (сверки нет). Событие команды k учитывается
  guarded_decide_event ПЕРЕД командой k+1 (одно событие -- один импульс, дубль -> DUPLICATE_EVENT, R-09);
  событие readback Active Probing -- сразу.

СОСТОЯНИЕ СЛУЖБЫ (локально): JSON рядом с журналом -- <папка журнала>\\sigma_state.json (logs\\ gitignored) или
OKO_SIGMA_STATE_PATH. Хранит: A_t-автомат (DissonanceIntegrator.to_state), последний seq, отложенное событие,
счётчики переходов n(x', a, y) и остатков m(r) (п.5, теневой режим), последнее наблюдение признака, счётчики
метрики gate A. Восстанавливается при старте; повреждено / параметры изменились -> HALT с меткой (fail-closed).

ВХОДЫ РЕШЕНИЯ:
  C_state -- вера до действия: b⁻ = predict_belief(e_{x'}, T_Dirichlet(n, m), π_0, Δt, τ_b) -- ОДИН вызов на действие
      (R-01); C_state = Σ_y b⁻(y)·1[min(1, |y − x_target|/κ) ≤ ε_norm + 1e-9]; нет x' / признака вне шкал
      params -> NO_DATA -> HALT;
  q_best / q_second / C_goal / N_hyp -- источник оценки намерения NO_DATA (Θ_intent, Θ_margin, Θ_goal = NO_DATA в
      params) -> сквозной decide = HALT; sigma_p -- вердикт с ПРЕДЛОЖЕНИЕМ «Ока» (INTENT_PROPOSAL ниже), только
      телеметрия для решения Оператора;
  Authorization = 1 (права уже проверил handle), SafetyGate = 1 (подтверждение опасной команды уже пройдено
      handle: до _execute доходит только подтверждённая), R_t -- из params.

Только stdlib + abstraction_layer. Строку теневого журнала пишет tv_bot_handler.shadow_write по белому списку
SHADOW_FIELDS: значения признаков (s_exp, s_obs, s_raw), узлы меню (node_from, node_to), время (ts) и коды вердикта
Σ (sigma, sigma_p, sigma_why, a_t, sigma_live) в журнал пишутся; текст ввода/ответа, id пользователя/чата, ключи,
сырой ответ API -- нет. Состояние службы хранит последнее наблюдение признака (obs) -- значение и время.
"""
from __future__ import annotations

import json
import logging
import math
import os
import threading
import time
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any, Callable, Mapping, Optional

import abstraction_layer as AL

logger = logging.getLogger("oko.sigma_gate")

LIVE_ENV = "OKO_SIGMA_LIVE"
STATE_ENV = "OKO_SIGMA_STATE_PATH"
PARAMS_ENV = "OKO_SIGMA_PARAMS_PATH"
STATE_NAME = "sigma_state.json"
STATE_SCHEMA = "sigma_gate/3d-v1"
ROOT = Path(__file__).resolve().parent.parent
DEFAULT_PARAMS = ROOT / "config" / "sigma_params.json"
ISOLATED = frozenset({"volume"})          # изолированный volume / внешний аудиовыход, сверки нет
OUTCOMES = ("EXECUTE", "OBSERVE", "CLARIFY", "HALT")
PROBE_PAUSE_S = 0.25                       # пауза между контрольными чтениями (боевой режим)

# Предложение «Ока» по оценке намерения (этап 3d) -- ТОЛЬКО для поля sigma_p (телеметрия). Решение Оператора --
# до него в params Θ_intent / Θ_margin / Θ_goal = NO_DATA и сквозной decide = HALT.
#   rule  -- команда разобрана правилами (tv_nav / menu_syntax / tv_commands, закрытый каталог): гипотезы
#            {команда, «ничего не делать»}: N_hyp = 2, q_best = 1, q_second = 0;
#   model -- смысл извлекла модель tv_intent («Понял как …»), оценок вероятности модель не даёт:
#            q_best = 0,7, q_second = 0,3 -> зазор 0,4 < Θ_margin -> CLARIFY (переспросить, не исполнять);
#   C_goal -- verified-команда (ТВ подтверждает чтением) 1,0; слепая (blind) 0,5.
INTENT_PROPOSAL = {"theta_intent": 0.75, "theta_margin": 0.5, "theta_goal": 0.5}
INTENT_CLASSES = {"rule": (1.0, 0.0, 2), "model": (0.7, 0.3, 2)}
C_GOAL = {"verified": 1.0, "blind": 0.5}
# B27 (T4): сбой внутреннего пути шлюза -- не молчаливый pass, а явный исход HALT с кодом причины. Коды -- только здесь.
# В журнал службы и в счётчик (metric["faults"], metric[<код>]) идут код и класс исключения (или метка ядра), без
# текста исключения и без ввода пользователя. OKO_SIGMA_LIVE=0: вердикт HALT только пишется (команда
# идёт по-старому); OKO_SIGMA_LIVE=1: команда не отправляется.
FAULT_SCALES = "SIGMA_SCALES_ERROR"          # шкалы признаков (kappa_j / eps_norm) из params не прочитаны
FAULT_PI0 = "SIGMA_PI0_ERROR"                # π_0 признака из params не прочитан (вера считалась бы по равномерному)
FAULT_EVENT = "SIGMA_EVENT_REJECTED"         # отложенное событие отвергнуто интегратором (шаг не учтён)
FAULT_COUNT = "SIGMA_COUNT_ERROR"            # счётчики n / m по исходу команды не обновлены -> HALT следующего решения
FAULT_PROBE = "SIGMA_PROBE_EVENT_ERROR"      # событие readback Active Probing не учтено -> пробинг остановлен
FAULT_CODES = (FAULT_SCALES, FAULT_PI0, FAULT_EVENT, FAULT_COUNT, FAULT_PROBE)
CARRY_MAX = 8                                # сбоев after(), переносимых на следующее решение (потолок)


def live_enabled() -> bool:
    return os.getenv(LIVE_ENV, "").strip() == "1"


def _now_ms() -> int:
    return int(time.time() * 1000)


def _is_int(v: Any) -> bool:
    return isinstance(v, int) and not isinstance(v, bool)


def _is_num(v: Any) -> bool:
    return isinstance(v, (int, float)) and not isinstance(v, bool) and math.isfinite(v)


def journal_max_seq(paths: "list[Path]") -> Optional[int]:
    """Наибольший seq в журнале (текущий + ротированный .1). Только поле seq; битые строки пропускаются."""
    best = None
    for p in paths:
        try:
            raw = p.read_bytes()
        except OSError:
            continue
        for b in raw.splitlines():
            if b"\"seq\"" not in b:
                continue
            try:
                s = json.loads(b.decode("utf-8")).get("seq")
            except (ValueError, UnicodeDecodeError, AttributeError):
                continue
            if _is_int(s) and s >= 0 and (best is None or s > best):
                best = s
    return best


@dataclass(frozen=True)
class Verdict:
    outcome: str                  # вердикт Σ по params (EXECUTE / OBSERVE / CLARIFY / HALT)
    why: Optional[str]            # первая причина (код)
    proposal: Optional[str]       # вердикт с предложением INTENT_PROPOSAL (телеметрия)
    alarm: bool                   # A_t на момент решения (неизвестно -> True, fail-closed)
    live: bool
    feature: Optional[str] = None
    c_state: Optional[float] = None

    @property
    def execute(self) -> bool:
        return self.outcome == "EXECUTE"


def _fault_tag(e: BaseException) -> str:
    """Метка ядра (INVALID_TIMESTAMP ...) или имя класса исключения -- без текста исключения."""
    lab = getattr(e, "label", None)
    return lab.value if isinstance(lab, AL.Label) else type(e).__name__


def _code(reason: Optional[str]) -> Optional[str]:
    if not reason:
        return None
    c = "".join(ch if (ch.isascii() and (ch.isalnum() or ch == "_")) else "_" for ch in reason)[:48]
    return c or None


class Gate:
    """Состояние службы + шлюз решения. Потокобезопасен (один RLock). Методы наружу не бросают."""

    def __init__(self, state_path: Path, params_path: Path, journal_paths: "list[Path]") -> None:
        self.state_path = Path(state_path)
        self.params_path = Path(params_path)
        self.journal_paths = list(journal_paths)
        self.lock = threading.RLock()
        self.params: Optional[AL.AbstractionParams] = None
        self.doc: dict = {}
        self.param_error: Optional[str] = None
        self.integ: Optional[AL.DissonanceIntegrator] = None
        self.state_error: Optional[str] = None
        self.last_seq: Optional[int] = None
        self.pending: list = []             # [[seq, ts, delta]] события, ещё не учтённые guarded_decide_event
        self.n: dict = {}                   # n[feature][action][x'][y]
        self.m: dict = {}                   # m[feature][r]
        self.obs: dict = {}                 # obs[feature] = [value, ts]
        self.feat_of: dict = {}             # action -> feature (по исходам)
        self.metric: dict = {"sent": 0, "verified": 0, "fail": 0, "events": 0, "probe_ok": 0, "probe_fail": 0,
                             "probe_runs": 0, "writes_blocked": 0, "faults": 0}
        self.probe_ok_run = 0
        self._faults: list = []             # B27: сбои текущего вызова ("<код>:<метка|класс>")
        self.carry: list = []               # B27: сбои after() -> HALT следующего решения (один раз)
        self._load_params()
        self._load_state()

    # ------------------------------------------------------------------ загрузка
    def _load_params(self) -> None:
        try:
            self.doc = json.loads(self.params_path.read_text(encoding="utf-8"))
            self.params = AL.params_from_doc(self.doc)
        except Exception as e:  # noqa: BLE001 -- нет параметров -> HALT
            self.params, self.param_error = None, type(e).__name__
            return
        try:
            self.integ = AL.DissonanceIntegrator(self.params)
        except Exception as e:  # noqa: BLE001
            self.integ, self.param_error = None, type(e).__name__

    def _load_state(self) -> None:
        try:
            raw = self.state_path.read_text(encoding="utf-8")
        except FileNotFoundError:
            return
        except OSError as e:
            self.state_error = "STATE_UNREADABLE_" + type(e).__name__
            return
        try:
            st = json.loads(raw)
            if not isinstance(st, dict) or st.get("schema") != STATE_SCHEMA:
                raise AL.InvalidStateError("schema")
            if _is_int(st.get("last_seq")):
                self.last_seq = st["last_seq"]
            for k in ("n", "m", "obs", "feat_of"):
                if isinstance(st.get(k), dict):
                    setattr(self, k, st[k])
            if isinstance(st.get("metric"), dict):
                self.metric.update({k: v for k, v in st["metric"].items() if _is_int(v)})
            if _is_int(st.get("probe_ok_run")):
                self.probe_ok_run = st["probe_ok_run"]
            carry = st.get("carry")
            if isinstance(carry, list):
                self.carry = [c for c in carry if isinstance(c, str) and c.split(":", 1)[0] in FAULT_CODES][:CARRY_MAX]
            pend = st.get("pending")
            if isinstance(pend, list):
                self.pending = [p for p in pend if isinstance(p, list) and len(p) == 3]
            ist = st.get("integrator")
            if ist is not None and self.params is not None:
                self.integ = AL.DissonanceIntegrator.from_state(self.params, ist)
        except AL.InvalidConfigError:
            self.state_error = AL.Label.INVALID_CONFIG.value     # параметры изменились с прошлого запуска
            self.integ = None
        except Exception:  # noqa: BLE001 -- повреждённое состояние -> fail-closed
            self.state_error = AL.Label.INVALID_STATE.value
            self.integ = None

    def save(self) -> None:
        """Атомарная запись состояния (tmp + replace). Сбой записи не влияет на команду."""
        try:
            st = {"schema": STATE_SCHEMA, "last_seq": self.last_seq, "pending": self.pending,
                  "integrator": self.integ.to_state() if self.integ is not None and self.state_error is None else None,
                  "state_error": self.state_error, "live": live_enabled(), "n": self.n, "m": self.m,
                  "obs": self.obs, "feat_of": self.feat_of, "metric": self.metric, "probe_ok_run": self.probe_ok_run,
                  "carry": self.carry}
            self.state_path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.state_path.with_name(self.state_path.name + ".tmp")
            tmp.write_text(json.dumps(st, ensure_ascii=False, separators=(",", ":"), sort_keys=True), encoding="utf-8")
            for i in range(3):
                try:
                    os.replace(tmp, self.state_path)
                    break
                except PermissionError:
                    if i == 2:
                        raise
                    time.sleep(0.05)
        except Exception as e:  # noqa: BLE001
            logger.warning("[SIGMA_GATE] состояние не записано: %s", type(e).__name__)

    def _fault(self, code: str, e: BaseException) -> None:
        """B27 (T4): сбой пути шлюза -> код в текущем вызове, счётчик, строка журнала (код + класс, без текста)."""
        tag = _fault_tag(e)
        self._faults.append(code + ":" + tag)
        self.metric["faults"] = int(self.metric.get("faults", 0)) + 1
        self.metric[code] = int(self.metric.get(code, 0)) + 1
        logger.warning("[SIGMA_GATE] HALT code=%s (%s)", code, tag)

    # ------------------------------------------------------------------ seq (п.5)
    def next_seq(self) -> int:
        """Строго растущий seq: max(последний + 1, текущие мс). Переживает перезапуск (состояние + журнал),
        перевод часов назад (последний + 1) и ротацию журнала (seq не берётся из одного файла)."""
        with self.lock:
            if self.last_seq is None:
                self.last_seq = journal_max_seq(self.journal_paths)
            last = self.last_seq if self.last_seq is not None else -1
            s = max(last + 1, _now_ms())
            self.last_seq = s
            self.save()
            return s

    # ------------------------------------------------------------------ признаки
    def scales(self) -> dict:
        """Шкала признака [0, κ_j] (κ_j = max − min API ТВ, нижняя граница 0) и ε_norm_j -- из params."""
        out = {}
        try:
            kap = self.doc["params"]["kappa_j"]["value"]
            eps = self.doc["eps_norm"]["per_feature"]
            if kap is None or eps is None:            # NO_DATA в params: шкал нет -> C_state NO_DATA -> HALT (не сбой)
                return out
            for f, k in kap.items():
                if _is_num(k) and k > 0 and _is_num(eps.get(f)) and f not in ISOLATED:
                    out[f] = (0.0, float(k), float(eps[f]))
        except Exception as e:  # noqa: BLE001 -- B27: HALT SIGMA_SCALES_ERROR
            self._fault(FAULT_SCALES, e)
        return out

    def _pi0(self, f: str, states: tuple) -> tuple:
        try:
            node = self.doc["params"]["pi_0"]
            if node.get("value") is None:             # NO_DATA (n = 0, синтетический дефолт): равномерный π_0 -- штатно
                return tuple(1.0 for _ in states)
            v = node["value"][f]
            seen, unseen = v.get("seen", {}), v.get("unseen")
            p = tuple(float(seen.get(str(int(s)), unseen)) for s in states)
            if all(_is_num(x) and x > 0 for x in p):
                return p
        except Exception as e:  # noqa: BLE001 -- B27: HALT SIGMA_PI0_ERROR (вера по равномерному не исполняется)
            self._fault(FAULT_PI0, e)
        return tuple(1.0 for _ in states)

    def _budget(self) -> AL.Budget:
        r = self.doc["params"]["R_t"]["value"]
        return AL.Budget(n_obs=int(r["N_obs"]), n_route=int(r["N_route"]), n_reflect=int(r["N_reflect"]))

    @staticmethod
    def action_key(mode: str, v: Any) -> str:
        return f"{mode}:{int(v) if float(v).is_integer() else v}"

    # ------------------------------------------------------------------ C_state (R-01: один predict_belief)
    def c_state(self, feature: Optional[str], mode: Optional[str], v: Any, now: float) -> float:
        sc = self.scales()
        if feature not in sc or mode not in ("SET", "STEP") or v is None:
            raise AL.NoDataError("C_state: признак/действие без шкалы или без величины")
        lo, hi, eps = sc[feature]
        prev = self.obs.get(feature)
        if not prev or not _is_num(prev[0]):
            raise AL.NoDataError("C_state: нет прошлого наблюдения x'")
        xp, t_prev = AL.check_observation(prev[0], lo, hi), float(prev[1])
        states = tuple(float(s) for s in range(int(lo), int(hi) + 1))
        cm = AL.CmdMode.SET if mode == "SET" else AL.CmdMode.STEP
        tgt = AL.transition_target(cm, lo, hi, x_prev=xp, v_cmd=v if mode == "SET" else None,
                                   d_cmd=v if mode == "STEP" else None)
        key = self.action_key(mode, v)
        cnt = self.n.get(feature, {}).get(key, {}).get(str(int(xp)) if float(xp).is_integer() else str(xp), {})
        row_counts = [float(cnt.get(str(int(y)), 0)) for y in states]
        res = {float(k): float(c) for k, c in self.m.get(feature, {}).items()}
        g = AL.residual_backoff(states, tgt, res)
        row = AL.dirichlet_row(row_counts, g).probs
        # b_{t−1} = e_{x'} (последнее наблюдение) -> b⁻ использует только строку x'; матрица из одинаковых строк даёт
        # тот же результат при одном вызове predict_belief (R-01) без построения всех |S| строк.
        b_prev = tuple(1.0 if s == xp else 0.0 for s in states)
        if sum(b_prev) != 1.0:
            raise AL.NoDataError("C_state: x' вне сетки состояний")
        b_minus = AL.predict_belief(b_prev, [row] * len(states), self._pi0(feature, states),
                                    max(0.0, now - t_prev), self.params.tau_b)
        return AL.state_admissibility_inf(b_minus, [(s,) for s in states], (tgt,), (hi - lo,), (eps,), ())

    # ------------------------------------------------------------------ решение перед командой
    def _inputs(self, feature, mode, v, kind, src, now) -> AL.DecisionInputs:
        c = self.c_state(feature, mode, v, now)
        qb, qs, nh = INTENT_CLASSES.get(src, INTENT_CLASSES["model"])
        return AL.DecisionInputs(c_state=c, q_best=qb, q_second=qs, c_goal=C_GOAL.get(kind, 0.5), d_t=0.0,
                                 authorization=1, safety_gate=1, budget_remaining=self._budget().remaining,
                                 dissonance_alarm=False, n_hyp=nh)

    def _decide(self, params: AL.AbstractionParams, build: Callable[[], AL.DecisionInputs]) -> AL.Decision:
        """Отложенные события: все, кроме последнего, -- шаг интегратора; последнее -- через guarded_decide_event."""
        if self.state_error is not None:
            return AL.Decision(AL.Outcome.HALT, (self.state_error,))
        if self.integ is None:
            return AL.Decision(AL.Outcome.HALT, ("NO_DATA:kernel" if self.param_error is None
                                                 else "PARAMS_" + self.param_error,))
        pend, self.pending = self.pending, []
        for eid, ts, dl in pend[:-1]:
            try:
                self.integ.step(int(eid), float(ts), [float(dl)])
            except Exception as e:  # noqa: BLE001 -- INVALID_TIMESTAMP: событие отвергнуто, состояние прежнее;
                self._fault(FAULT_EVENT, e)   # B27: HALT SIGMA_EVENT_REJECTED (не молча)
        if pend:
            eid, ts, dl = pend[-1]
            return AL.guarded_decide_event(params, self.integ, int(eid), float(ts), [float(dl)], build)
        integ = self.integ
        return AL.guarded_decide(params, lambda: integ.decision_inputs(build()))

    def before(self, action: Optional[str], kind: Optional[str], d_cmd: Optional[int], v_set: Any,
               src: str = "rule", now: Optional[float] = None) -> Verdict:
        now = time.time() if now is None else now
        live = live_enabled()
        with self.lock:
            self._faults, self.carry = list(self.carry), []        # B27: сбои прошлого after() -> HALT этого решения
            feature = self.feat_of.get(action) if action else None
            mode, v = ("STEP", d_cmd) if d_cmd is not None else (("SET", v_set) if v_set is not None else (None, None))
            memo: dict = {}

            def build() -> AL.DecisionInputs:
                # R-01: входы (и predict_belief) считаются ОДИН раз на действие; повторный запрос -- тот же итог
                if "x" not in memo:
                    try:
                        memo["x"] = self._inputs(feature, mode, v, kind, src, now)
                    except Exception as e:  # noqa: BLE001
                        memo["x"] = e
                if isinstance(memo["x"], Exception):
                    raise memo["x"]
                return memo["x"]
            try:
                if self.params is None:
                    raise AL.NoDataError("params")
                dec = self._decide(self.params, build)
                prop = AL.guarded_decide(replace(self.params, **INTENT_PROPOSAL),
                                         lambda: self.integ.decision_inputs(build())) if self.integ is not None else dec
                x = memo.get("x")
                cs = x.c_state if isinstance(x, AL.DecisionInputs) else None
            except Exception as e:  # noqa: BLE001
                dec = prop = AL.Decision(AL.Outcome.HALT, ("KERNEL_ERROR:" + type(e).__name__,))
                cs = None
            if self._faults:                                      # B27 (T4): сбой пути шлюза -> HALT с его кодом
                dec = prop = AL.Decision(AL.Outcome.HALT, (self._faults[0],))
            alarm = True if (self.integ is None or self.state_error) else bool(self.integ.alarm)
            self.save()
            return Verdict(outcome=dec.outcome.value, why=_code(dec.reasons[0] if dec.reasons else None),
                           proposal=prop.outcome.value, alarm=alarm, live=live, feature=feature, c_state=cs)

    # ------------------------------------------------------------------ исход команды -> событие
    def after(self, seq: Optional[int], *, action: Optional[str], kind: Optional[str], outcome: Optional[str],
              err: Optional[str], feature: Optional[str], expected: Any, observed: Any, d_cmd: Any,
              sent: bool, now: Optional[float] = None) -> Optional[int]:
        """Исход команды: метрика gate A, счётчики n / m, событие δ (отложенное). Возвращает δ или None.
        expected -- s_exp строки outcome (цель записи), observed -- s_obs (чтение после записи)."""
        now = time.time() if now is None else now
        with self.lock:
            self._faults = []
            try:
                if feature and action:
                    self.feat_of[action] = feature
                if feature in ISOLATED:
                    return None
                if sent and kind == "verified":
                    self.metric["sent"] += 1
                    if outcome == "PASS" and _is_num(observed):
                        self.metric["verified"] += 1
                if outcome == "FAIL":
                    self.metric["fail"] += 1
                self._count(feature, expected, observed, d_cmd, now)
                if outcome == "FAIL":
                    delta = 1
                elif outcome == "PASS" and kind == "verified":
                    delta = 0
                elif outcome == "WARN" and err == "warn":
                    delta = 0
                else:
                    return None                          # слепая команда / нет исхода -- сверки нет, события нет
                if not _is_int(seq):
                    return None
                self.pending.append([seq, now, delta])
                self.metric["events"] += 1
                return delta
            finally:
                if self._faults:                                  # B27: HALT следующего решения (один раз)
                    self.carry = (self.carry + self._faults)[:CARRY_MAX]
                self.save()

    def _count(self, feature, expected, observed, d_cmd, now) -> None:
        """п.5: n(x', a, y) и m(r) по исходу (теневой режим; только локальное состояние)."""
        sc = self.scales()
        if feature not in sc or not _is_num(observed):
            return
        lo, hi, _ = sc[feature]
        if not (lo <= observed <= hi):
            return                                       # INVALID_OBSERVATION: в модель не идёт (наблюдение вне шкалы)
        prev = self.obs.get(feature)
        if prev and _is_num(prev[0]):
            xp = prev[0]
            if _is_int(d_cmd):
                mode, v = "STEP", d_cmd
            elif _is_num(expected):
                mode, v = "SET", expected
            else:
                mode = v = None
            if mode is not None:
                try:
                    cm = AL.CmdMode.SET if mode == "SET" else AL.CmdMode.STEP
                    r = AL.transition_residual(cm, observed, lo, hi, x_prev=xp, v_cmd=v if mode == "SET" else None,
                                               d_cmd=v if mode == "STEP" else None)
                    key = self.action_key(mode, v)
                    row = self.n.setdefault(feature, {}).setdefault(key, {}).setdefault(_k(xp), {})
                    row[_k(observed)] = row.get(_k(observed), 0) + 1
                    mm = self.m.setdefault(feature, {})
                    mm[_k(r.residual)] = mm.get(_k(r.residual), 0) + 1
                except Exception as e:  # noqa: BLE001 -- B27: HALT SIGMA_COUNT_ERROR (следующее решение)
                    self._fault(FAULT_COUNT, e)
        self.obs[feature] = [observed, now]

    # ------------------------------------------------------------------ Active Probing (боевой режим, п.3)
    def probe(self, tv: Any, feature: Optional[str], next_seq: Callable[[], int],
              pause: float = PROBE_PAUSE_S) -> dict:
        """Только чтение признака (get_*), не больше R_t чтений. Удачное (d_∞ ≤ ε_norm к последнему наблюдению) ->
        событие δ = 0; расхождение / сбой чтения -> δ = 1, счёт удачных подряд сбрасывается. Каждое чтение --
        отдельное событие seq. Остановка: тревога снята (D < Θ_reset) или бюджет исчерпан."""
        res = {"reads": 0, "ok": 0, "fail": 0, "cleared": False, "reason": None, "halt": None}
        with self.lock:
            self._faults = []
            self.metric["probe_runs"] += 1
            sc = self.scales()
            if self._faults:                                      # B27: шкалы не прочитаны -> HALT, чтений нет
                res["reason"], res["halt"] = "HALT", _code(self._faults[0])
                self.save()
                return res
            f = feature if feature in sc else next((x for x in sorted(self.obs) if x in sc), None)
            if f is None or self.integ is None or self.params is None or self.state_error:
                res["reason"] = "PROBE_NO_REF"
                self.save()
                return res
            lo, hi, eps = sc[f]
            try:
                budget = self._budget()
            except Exception:  # noqa: BLE001
                res["reason"] = "NO_DATA_R_t"
                self.save()
                return res
            kinds = ("obs", "route", "reflect")
            while self.integ.alarm:
                kind = next((k for k in kinds if getattr(budget, "n_" + k) > 0), None)
                if kind is None:
                    res["reason"] = "BUDGET_EXHAUSTED"
                    break
                budget = budget.spend(kind)
                ref = self.obs.get(f)
                val = None
                try:
                    val = read_feature(tv, f)
                except Exception:  # noqa: BLE001
                    val = None
                res["reads"] += 1
                ok = False
                if _is_num(val) and ref and _is_num(ref[0]):
                    try:
                        AL.check_observation(val, lo, hi)
                        ok = AL.state_admissible_inf((val,), (ref[0],), (hi - lo,), (eps,))
                    except Exception:  # noqa: BLE001
                        ok = False
                delta = 0 if ok else 1
                now = time.time()
                try:
                    self.pending.append([next_seq(), now, delta])
                    self._decide(self.params, _probe_inputs)    # событие readback -> guarded_decide_event
                except Exception as e:  # noqa: BLE001 -- B27: HALT SIGMA_PROBE_EVENT_ERROR, пробинг остановлен
                    self._fault(FAULT_PROBE, e)
                if ok:
                    res["ok"] += 1
                    self.probe_ok_run += 1
                    self.metric["probe_ok"] += 1
                else:
                    res["fail"] += 1
                    self.probe_ok_run = 0
                    self.metric["probe_fail"] += 1
                if _is_num(val) and lo <= val <= hi:
                    self.obs[f] = [val, now]             # карта синхронизирована с прочитанным
                if self._faults:                         # B27 (T4): событие не учтено -> HALT, дальше не читаем
                    res["reason"], res["halt"] = "HALT", _code(self._faults[0])
                    break
                if self.integ.alarm and pause > 0:
                    time.sleep(pause)
            res["cleared"] = not self.integ.alarm
            res["feature"] = f
            self.save()
            return res


def _probe_inputs() -> AL.DecisionInputs:
    """Входы решения на шаге readback: исполнения нет (решение нужно только для учёта события и A_t)."""
    return AL.DecisionInputs(c_state=0.0, q_best=0.0, q_second=0.0, c_goal=0.0, d_t=0.0, authorization=1,
                             safety_gate=1, budget_remaining=1, dissonance_alarm=False, n_hyp=0)


def _k(v: Any) -> str:
    return str(int(v)) if float(v).is_integer() else repr(float(v))


def read_feature(tv: Any, feature: str) -> Optional[float]:
    """Контрольное чтение признака -- ТОЛЬКО методы get_* клиента ТВ."""
    if feature.startswith("setting_"):
        v = tv.get_setting_value(int(feature.split("_", 1)[1]))
        return float(v) if _is_num(v) else None
    if feature == "lounge_brightness":
        r = tv.get_lounge_color()
        c = r.get("color", r) if isinstance(r, Mapping) else None
        b = c.get("brightness") if isinstance(c, Mapping) else None
        return float(b) if _is_num(b) else None
    return None


_GATES: dict = {}
_GATES_LOCK = threading.Lock()


def get_gate(journal_path: Path) -> Gate:
    """Шлюз для текущих путей окружения (кэш по путям: тесты меняют окружение, служба -- нет)."""
    jp = Path(journal_path)
    sp = Path(os.getenv(STATE_ENV, "").strip() or jp.with_name(STATE_NAME))
    pp = Path(os.getenv(PARAMS_ENV, "").strip() or DEFAULT_PARAMS)
    key = (str(sp), str(pp), str(jp))
    with _GATES_LOCK:
        g = _GATES.get(key)
        if g is None:
            g = Gate(sp, pp, [jp.with_name(jp.name + ".1"), jp])
            _GATES[key] = g
        return g


def reset() -> None:
    """Сброс кэша (тесты; эмуляция перезапуска службы)."""
    with _GATES_LOCK:
        _GATES.clear()
