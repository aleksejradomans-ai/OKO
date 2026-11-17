"""abstraction_layer.py -- ядро слоя Σ, контур A (фаза 1: этап 1b «ядро Σ этапа 1b»; строгий отказ -- этап 1d «строгий отказ этапа 1d»;
фаза 3: этап 3b ядро по реестру проверок, см. раздел «Этап 3b» ниже).

Чистая математика слоя Σ (спецификация, §3). Только stdlib:
без железа, сети, бота, баз. Модуль никуда не подключён (вшивание -- фаза 3).

Все пороги в AbstractionParams по умолчанию NO_DATA (None): калибровка -- фаза 2.
Любой порог решения NO_DATA (включая Θ_reset) -> decide = HALT (отказ по умолчанию).

Строгий отказ (решение Оператора 15.11 00:18/00:51, этап 1d):
  - R_t = 0 -> строго HALT;
  - входы decide C_state, q_best, q_second, C_goal, D_t ∈ [0, 1] и конечны, иначе HALT BAD_INPUT;
  - молчаливого обрезания нет: δ_t/D_t вне [0, 1] и бесконечности в d_Σ -> ValueError (ошибка входа);
  - ковариация: несимметричная, не положительно полуопределённая или численно вырожденная
    -> CovarianceError; ложного r² = 0 из-за среза отрицательного значения нет;
  - guarded_decide: любые ошибки ядра (вырождение, масштаб, переполнение, NO_DATA параметра формулы)
    наружу не выходят -> HALT с причиной.
Не здесь (фаза 3, этап 3d): передача защёлки гистерезиса из router.py и подключение к боту.

Этап 3b (решения Оператора 14:51 и 16:16; реестр проверок):
  - интегратор λ_0 (темп Δt_0, длина серии, решение 16:16): внутри сессии -- шаг по событию
      D_n = min(1, λ_0·D_{n−1} + (1 − λ_0)·δ_n),  λ_0 = exp(−Δt_0/τ_D)  (Δt_0, τ_D -- из AbstractionParams);
    δ_n = 1[∃k e_k > 0] (δ по любому сбою); граница сессии -- пауза Δt > τ_D: перед событием D ← D·exp(−Δt/τ_D)
    (реальное время только здесь; распад D по паузе); Δt < 0 -> INVALID_TIMESTAMP -> HALT (R-08);
  - автомат A_t (R-06, защёлка A_t): вход D ≥ Θ_crit, выход D < Θ_reset; состояние -- словарь примитивов
    (to_state / from_state) для восстановления после перезапуска службы; дедупликация по event_id = seq (R-09);
  - решение о допуске -- d_∞ = max_j min(1, |Δ_j|/κ_j) ≤ ε_norm_j + 1e-9 (допуск d_∞, R-04);
    d_Σ -- только телеметрия, α_j ≥ 0 и Σα = 1, иначе INVALID_CONFIG;
  - метки MODEL_MISMATCH (Z = 0: b = b⁻, OBSERVE; R-02), INVALID_DISTRIBUTION (R-05),
    UNSEEN_TRANSITION (строка/переход без наблюдений; сглаживание Дирихле -- этап 3c);
  - decide: N_hyp < 2 -> CLARIFY (R-07); NO_DATA порога -> HALT (как в 1d).
  Прежняя форма dissonance_step (λ_t = exp(−Δt/τ_D) на каждом шаге) оставлена без изменений как форма фазы 2
  (её вызывают tools/check_sigma_2e.py и тесты 1d); путь решения фазы 3 -- DissonanceIntegrator.

Этап 3c (решения Оператора 15:36 и 18:21; реестр проверок):
  - модель переходов по остатку (остаток SET/STEP): r = x_obs − x_target; SET -> x_target = clamp(v_cmd), STEP ->
    x_target = clamp(x' + Δ_cmd), clamp -- в шкалу признака [lo, hi];
  - наблюдение вне шкалы / не число -> INVALID_OBSERVATION до расчёта переходов -> HALT;
  - сглаживание Дирихле α = 1 по переходам с backoff (аудит 01):
      T(y|x', a) = (n(x', a, y) + α·G(y|c)) / (N(x', a) + α),  G(y|c) = (m(y − x_target) + α·U(y)) / (M + α),
    m -- счёт остатков признака (контекст c), U -- равномерное по шкале; строка без наблюдений = G и метка
    UNSEEN_TRANSITION, но вероятность любого перехода > 0 (метка больше не единственная реакция);
  - заморозка D при A_t = 1, fail-closed (решение 18:21): при A_t = 1 распад D по Δt на границе сессии не применяется (D заморожен,
    A_t сохраняется); снятие тревоги -- только успехами по D < Θ_reset. При A_t = 0 -- как в 3b (распад D по паузе);
  - Δt_0 -- из файла параметров (раздел `dt_0`, MANUAL): params_from_doc(doc) -- документ config/sigma_params.json,
    уже прочитанный вызывающим (ядро без ввода-вывода: контракт 1d -- только math/dataclasses/enum/typing).

Формулы §3 (не менять):
  ρ_t   = exp(−Δt/τ_b)
  b_t⁻  = ρ_t · Σ_x' T(x|x',a) · b_{t−1}(x') + (1 − ρ_t) · π_0(x)
  b_t   = Z(O|x) · b_t⁻(x) / Σ_z Z(O|z) · b_t⁻(z)
  d_Σ   = Σ_j α_j · min(1, d_j/κ_j)
  C_state = Σ_{s∈A_ε} P(s|s_obs)             (любой H_k = 0 -> C_state = 0)
  Σ_i   = Σ_host + Σ_obj_i + Σ_cal + ε_cov·I
  r_i²  = ΔL_iᵀ Σ_i⁻¹ ΔL_i
  u_i   = q_i·c_h·c_i·exp(−a_h/τ_h)·exp(−a_i/τ_i)·exp(−κ_space·r_i²)
  w_i   = u_i / Σ_j u_j ;  W_i = w_i · r̂_i
  δ_t   = Σ_k β_k·e_k ;  λ_t = exp(−Δt/τ_D) ;  D_t = λ_t·D_{t−1} + (1 − λ_t)·δ_t
  EXECUTE <=> C_state ≥ Θ_state ∧ q_best ≥ Θ_intent ∧ (q_best − q_second) ≥ Θ_margin
              ∧ C_goal ≥ Θ_goal ∧ D_t < Θ_crit ∧ Authorization = 1 ∧ SafetyGate = 1
"""
from __future__ import annotations

import math
from dataclasses import dataclass, fields, replace
from enum import Enum
from typing import Callable, Mapping, Optional, Sequence, Tuple

NO_DATA = None

Vector = Tuple[float, ...]
Matrix = Tuple[Tuple[float, ...], ...]


class Outcome(str, Enum):
    EXECUTE = "EXECUTE"
    OBSERVE = "OBSERVE"
    CLARIFY = "CLARIFY"
    HALT = "HALT"


class NoDataError(ValueError):
    """Параметр формулы = NO_DATA (не откалиброван)."""


class CovarianceError(ValueError):
    """Ковариация несимметрична, не положительно полуопределена или численно вырождена."""


class Label(str, Enum):
    """Метки ядра фазы 3 (реестр проверок): причина HALT / пометка исхода."""
    INVALID_TIMESTAMP = "INVALID_TIMESTAMP"        # R-08: Δt < 0 или не число
    INVALID_DISTRIBUTION = "INVALID_DISTRIBUTION"  # R-05: пустое / нулевое / NaN / отрицательное распределение
    INVALID_CONFIG = "INVALID_CONFIG"              # допуск d_∞: α_j < 0 или Σα ≠ 1; ε_norm вне [0, 1]; чужие параметры A_t
    INVALID_STATE = "INVALID_STATE"                # R-06: повреждённое сохранённое состояние A_t
    MODEL_MISMATCH = "MODEL_MISMATCH"              # R-02: Z·b⁻ = 0 -> b = b⁻, OBSERVE
    UNSEEN_TRANSITION = "UNSEEN_TRANSITION"        # строка / переход T без наблюдений
    DUPLICATE_EVENT = "DUPLICATE_EVENT"            # R-09: event_id уже учтён -- D не меняется
    INVALID_OBSERVATION = "INVALID_OBSERVATION"    # (этап 3c): наблюдение вне шкалы признака / не число


class InvalidTimestampError(ValueError):
    """Δt < 0 (или не число): метка INVALID_TIMESTAMP -> HALT (R-08)."""
    label = Label.INVALID_TIMESTAMP


class InvalidDistributionError(ValueError):
    """Невалидное распределение: метка INVALID_DISTRIBUTION -> HALT (R-05)."""
    label = Label.INVALID_DISTRIBUTION


class InvalidConfigError(ValueError):
    """Невалидная конфигурация (α, ε_norm, параметры A_t): метка INVALID_CONFIG -> HALT (допуск d_∞)."""
    label = Label.INVALID_CONFIG


class InvalidStateError(ValueError):
    """Повреждённое сохранённое состояние автомата A_t: метка INVALID_STATE -> HALT (R-06)."""
    label = Label.INVALID_STATE


class InvalidObservationError(ValueError):
    """Наблюдение вне шкалы признака или не конечное число: метка INVALID_OBSERVATION -> HALT (этап 3c)."""
    label = Label.INVALID_OBSERVATION


_LABELED_ERRORS = (InvalidTimestampError, InvalidDistributionError, InvalidConfigError, InvalidStateError,
                   InvalidObservationError)


# ---------------------------------------------------------------- параметры
@dataclass(frozen=True)
class AbstractionParams:
    """Параметры слоя Σ. Всё по умолчанию NO_DATA (None)."""
    # постоянные времени (> 0)
    tau_b: Optional[float] = NO_DATA        # забывание веры
    tau_D: Optional[float] = NO_DATA        # интегратор диссонанса
    tau_h: Optional[float] = NO_DATA        # возраст кадра хозяина (PTZ)
    tau_i: Optional[float] = NO_DATA        # возраст кадра объекта
    kappa_space: Optional[float] = NO_DATA  # ≥ 0
    eps_cov: Optional[float] = NO_DATA      # > 0, регуляризация Σ_i
    eps_state: Optional[float] = NO_DATA    # ≥ 0, радиус A_ε по d_Σ
    # пороги решения
    theta_state: Optional[float] = NO_DATA
    theta_intent: Optional[float] = NO_DATA
    theta_margin: Optional[float] = NO_DATA
    theta_goal: Optional[float] = NO_DATA
    theta_crit: Optional[float] = NO_DATA
    theta_reset: Optional[float] = NO_DATA
    # номинальный шаг серии (> 0): λ_0 = exp(−Δt_0/τ_D) интегратора λ_0 (этап 3b, решение 16:16)
    dt_0: Optional[float] = NO_DATA

    _POSITIVE = ("tau_b", "tau_D", "tau_h", "tau_i", "eps_cov", "dt_0")
    _NONNEG = ("kappa_space", "eps_state", "theta_margin")
    _UNIT = ("theta_state", "theta_intent", "theta_goal", "theta_crit", "theta_reset")

    def __post_init__(self) -> None:
        for f in fields(self):
            v = getattr(self, f.name)
            if v is None:
                continue
            if isinstance(v, bool) or not isinstance(v, (int, float)) or not math.isfinite(v):
                raise ValueError(f"{f.name}: ожидается конечное число или NO_DATA, получено {v!r}")
            if f.name in self._POSITIVE and v <= 0:
                raise ValueError(f"{f.name} должен быть > 0")
            if f.name in self._NONNEG and v < 0:
                raise ValueError(f"{f.name} должен быть ≥ 0")
            if f.name in self._UNIT and not (0.0 <= v <= 1.0):
                raise ValueError(f"{f.name} должен быть в [0, 1]")
        if self.theta_reset is not None and self.theta_crit is not None and self.theta_reset >= self.theta_crit:
            raise ValueError("гистерезис: требуется Θ_reset < Θ_crit")

    def missing(self) -> Tuple[str, ...]:
        """Имена параметров со значением NO_DATA."""
        return tuple(f.name for f in fields(self) if getattr(self, f.name) is None)


DECISION_THRESHOLDS = ("theta_state", "theta_intent", "theta_margin", "theta_goal", "theta_crit", "theta_reset")
DECISION_INPUTS_UNIT = ("c_state", "q_best", "q_second", "c_goal", "d_t")


# ---------------------------------------------------------------- проверки
def _need(value: Optional[float], name: str) -> float:
    if value is None:
        raise NoDataError(f"{name} = NO_DATA")
    return float(value)


def _finite(x: float, name: str) -> float:
    x = float(x)
    if math.isnan(x):
        raise ValueError(f"{name}: NaN")
    return x


def _nonneg_vec(v: Sequence[float], name: str) -> Vector:
    out = tuple(_finite(x, name) for x in v)
    if any(x < 0 or math.isinf(x) for x in out):
        raise ValueError(f"{name}: нужны конечные значения ≥ 0")
    return out


def _distribution(v: Sequence[float], name: str) -> Vector:
    """Нормированное распределение. Пустое / нулевое / NaN / ±Inf / отрицательное -> InvalidDistributionError
    (подкласс ValueError; метка INVALID_DISTRIBUTION, R-05)."""
    try:
        p = _nonneg_vec(v, name)
    except InvalidDistributionError:
        raise
    except (ValueError, TypeError) as e:
        raise InvalidDistributionError(f"{name}: {e}") from None
    s = math.fsum(p)
    if not p or not (s > 0) or not math.isfinite(s):
        raise InvalidDistributionError(f"{name}: пустое или нулевое распределение")
    return tuple(x / s for x in p)


# ---------------------------------------------------------------- 1. Байес с затуханием
def decay(dt: float, tau: Optional[float], name: str = "tau") -> float:
    """exp(−Δt/τ). Δt = 0 -> 1 ровно; Δt = +inf -> 0. Δt < 0 или NaN -> InvalidTimestampError (R-08)."""
    try:
        dt = _finite(dt, "dt")
    except ValueError:
        raise InvalidTimestampError("dt: NaN") from None
    if dt < 0:
        raise InvalidTimestampError("dt < 0")
    if dt == 0:
        return 1.0
    if math.isinf(dt):
        return 0.0
    return math.exp(-dt / _need(tau, name))


def predict_belief(b_prev: Sequence[float], transition: Sequence[Sequence[float]],
                   pi0: Sequence[float], dt: float, tau_b: Optional[float]) -> Vector:
    """b_t⁻. transition[x'][x] = T(x | x', a_{t−1}) для уже выбранного действия a_{t−1}."""
    b = _distribution(b_prev, "b_prev")
    p0 = _distribution(pi0, "pi0")
    n = len(b)
    if len(p0) != n or len(transition) != n:
        raise ValueError("размерности b_prev / pi0 / transition не совпадают")
    rows = [_distribution(r, f"T[{i}]") for i, r in enumerate(transition)]
    if any(len(r) != n for r in rows):
        raise ValueError("transition должна быть n×n")
    rho = decay(dt, tau_b, "tau_b")
    pred = [math.fsum(rows[xp][x] * b[xp] for xp in range(n)) for x in range(n)]
    if rho == 1.0:
        return tuple(pred)
    if rho == 0.0:
        return p0
    return tuple(rho * pred[x] + (1.0 - rho) * p0[x] for x in range(n))


@dataclass(frozen=True)
class BeliefUpdate:
    belief: Vector
    outcome: Optional[Outcome]   # None -- штатно; OBSERVE -- наблюдение несовместимо ни с одним состоянием
    normalizer: float
    label: Optional[Label] = None   # MODEL_MISMATCH при Z·b⁻ = 0 (R-02)


def update_belief(b_minus: Sequence[float], likelihood: Sequence[float]) -> BeliefUpdate:
    """b_t = Z·b⁻ / Σ Z·b⁻. Σ Z·b⁻ = 0 -> вера не меняется (b = b⁻), исход OBSERVE, метка MODEL_MISMATCH
    (R-02: без outlier-шума; без деления на 0)."""
    bm = _distribution(b_minus, "b_minus")
    z = _nonneg_vec(likelihood, "likelihood")
    if len(z) != len(bm):
        raise ValueError("размерности b_minus / likelihood не совпадают")
    num = [z[i] * bm[i] for i in range(len(bm))]
    s = math.fsum(num)
    if not (s > 0) or not math.isfinite(s):
        return BeliefUpdate(belief=bm, outcome=Outcome.OBSERVE, normalizer=0.0, label=Label.MODEL_MISMATCH)
    return BeliefUpdate(belief=tuple(x / s for x in num), outcome=None, normalizer=s)


def transition_status(counts_row: Sequence[float], x_next: Optional[int] = None) -> Optional[Label]:
    """Метка UNSEEN_TRANSITION (этап 3b п.4): строка T(·|x', a) не имеет наблюдений (Σ n = 0) или наблюдённый
    переход x' -> x_next не встречался (n = 0). Пометка; вероятность такому переходу даёт dirichlet_row (этап 3c).
    counts_row -- счётчики переходов из x' (конечные, ≥ 0)."""
    n = _nonneg_vec(counts_row, "counts_row")
    if not n:
        raise ValueError("counts_row: пустая строка")
    if x_next is not None:
        if isinstance(x_next, bool) or not isinstance(x_next, int) or not (0 <= x_next < len(n)):
            raise ValueError("x_next: индекс состояния вне строки")
    if math.fsum(n) == 0 or (x_next is not None and n[x_next] == 0):
        return Label.UNSEEN_TRANSITION
    return None


# ---------------------------------------------------------------- 1b. Этап 3c: модель переходов по остатку + Дирихле
DIRICHLET_ALPHA = 1.0      # масса априора Дирихле по переходам и по backoff (Дирихле α = 1, аудит 01: α = 1)


class CmdMode(str, Enum):
    """Режим команды признака (остаток SET/STEP): SET -- абсолютное значение v_cmd; STEP -- шаг Δ_cmd от x'."""
    SET = "SET"
    STEP = "STEP"


@dataclass(frozen=True)
class Residual:
    target: float      # x_target (после clamp в шкалу)
    residual: float    # r = x_obs − x_target
    observed: float    # x_obs (проверенное наблюдение)


@dataclass(frozen=True)
class TransitionRow:
    probs: Vector                    # T(·|x', a), Σ = 1, все > 0
    label: Optional[Label] = None    # UNSEEN_TRANSITION -- строка без наблюдений (вся масса из backoff)


def _real(v: object, name: str) -> float:
    """Конечное число (bool -- не число), иначе ValueError."""
    if isinstance(v, bool) or not isinstance(v, (int, float)) or not math.isfinite(v):
        raise ValueError(f"{name}: нужно конечное число")
    return float(v)


def scale_bounds(lo: object, hi: object) -> Tuple[float, float]:
    """Шкала признака [lo, hi]: конечные числа, lo < hi; иначе InvalidConfigError (INVALID_CONFIG)."""
    try:
        a, b = _real(lo, "lo"), _real(hi, "hi")
    except ValueError:
        raise InvalidConfigError("шкала признака: нужны конечные lo < hi") from None
    if not a < b:
        raise InvalidConfigError("шкала признака: нужны конечные lo < hi")
    return a, b


def clamp(v: object, lo: object, hi: object) -> float:
    """clamp(v) = min(hi, max(lo, v)) -- в шкалу признака (остаток SET/STEP)."""
    a, b = scale_bounds(lo, hi)
    return min(b, max(a, _real(v, "v")))


def check_observation(x: object, lo: object, hi: object) -> float:
    """Наблюдение (сырое значение признака, наблюдение вне шкалы): конечное число в [lo, hi], иначе InvalidObservationError
    (INVALID_OBSERVATION). Наблюдение не обрезается и не округляется: обрезание спрятало бы источник выброса."""
    a, b = scale_bounds(lo, hi)
    if isinstance(x, bool) or not isinstance(x, (int, float)) or not math.isfinite(x):
        raise InvalidObservationError("наблюдение: не конечное число")
    if not (a <= x <= b):
        raise InvalidObservationError("наблюдение вне шкалы признака")
    return float(x)


def transition_target(mode: CmdMode, lo: object, hi: object, x_prev: object = None, v_cmd: object = None,
                      d_cmd: object = None) -> float:
    """x_target (остаток SET/STEP): SET -> clamp(v_cmd); STEP -> clamp(x' + Δ_cmd). x' -- прошлое наблюдение того же признака:
    вне шкалы -> INVALID_OBSERVATION. Нет v_cmd / Δ_cmd / x' для своего режима -> ValueError."""
    a, b = scale_bounds(lo, hi)
    if not isinstance(mode, CmdMode):
        raise ValueError("mode: нужен CmdMode.SET или CmdMode.STEP")
    if mode is CmdMode.SET:
        if v_cmd is None:
            raise ValueError("SET: нет v_cmd")
        return clamp(v_cmd, a, b)
    if d_cmd is None or x_prev is None:
        raise ValueError("STEP: нужны x' и Δ_cmd")
    xp = check_observation(x_prev, a, b)
    return clamp(xp + _real(d_cmd, "d_cmd"), a, b)


def transition_residual(mode: CmdMode, x_obs: object, lo: object, hi: object, x_prev: object = None,
                        v_cmd: object = None, d_cmd: object = None) -> Residual:
    """r = x_obs − x_target (остаток SET/STEP). Наблюдение проверяется ДО расчёта перехода (наблюдение вне шкалы): вне шкалы -> INVALID_OBSERVATION."""
    xo = check_observation(x_obs, lo, hi)
    t = transition_target(mode, lo, hi, x_prev=x_prev, v_cmd=v_cmd, d_cmd=d_cmd)
    return Residual(target=t, residual=xo - t, observed=xo)


def _alpha(alpha: object) -> float:
    try:
        a = _real(alpha, "alpha")
    except ValueError:
        raise InvalidConfigError("Дирихле: α -- конечное число > 0") from None
    if not a > 0:
        raise InvalidConfigError("Дирихле: α -- конечное число > 0")
    return a


def residual_backoff(states: Sequence[float], target: float, residual_counts: Mapping[float, float],
                     alpha: float = DIRICHLET_ALPHA) -> Vector:
    """Backoff (второй уровень Дирихле): G(y|c) ∝ m(y − x_target) + α·U(y) по состояниям шкалы, U = 1/|S|.
    m -- счётчики остатков r признака (контекст c; конечные ≥ 0). Остаток, уводящий за шкалу от этого x_target,
    массы не даёт; итог нормируется по сумме. При α > 0 все G(y) > 0; без остатков (M = 0) G = U."""
    a = _alpha(alpha)
    st = tuple(_real(x, "state") for x in states)
    if not st or len(set(st)) != len(st):
        raise InvalidConfigError("states: пусто или повтор")
    t = _real(target, "target")
    m = {}
    for k, v in dict(residual_counts).items():
        kk, vv = _real(k, "residual"), _real(v, "count")
        if vv < 0:
            raise ValueError("count: нужно ≥ 0")
        m[kk] = m.get(kk, 0.0) + vv
    big_m = math.fsum(m.values())
    u = a / len(st)
    raw = [(m.get(y - t, 0.0) + u) / (big_m + a) for y in st]
    return _distribution(raw, "backoff")


def dirichlet_row(counts_row: Sequence[float], backoff: Sequence[float], alpha: float = DIRICHLET_ALPHA) -> TransitionRow:
    """T(y|x', a) = (n_y + α·G_y) / (N + α) (аудит 01: Дирихле α = 1 по переходам с backoff G).
    N = 0 -> строка = G и метка UNSEEN_TRANSITION; G -- распределение (иначе INVALID_DISTRIBUTION)."""
    a = _alpha(alpha)
    n = _nonneg_vec(counts_row, "counts_row")
    g = _distribution(backoff, "backoff")
    if len(n) != len(g) or not n:
        raise ValueError("counts_row / backoff: размерности не совпадают или пусто")
    big_n = math.fsum(n)
    row = _distribution([(n[i] + a * g[i]) / (big_n + a) for i in range(len(n))], "T_row")
    return TransitionRow(probs=row, label=Label.UNSEEN_TRANSITION if big_n == 0 else None)


def transition_matrix_dirichlet(states: Sequence[float], mode: CmdMode, lo: object, hi: object,
                                counts: Mapping[float, Sequence[float]], residual_counts: Mapping[float, float],
                                v_cmd: object = None, d_cmd: object = None,
                                alpha: float = DIRICHLET_ALPHA) -> Tuple[Tuple[Vector, ...], Tuple[Optional[Label], ...]]:
    """Матрица transition[x'][y] для predict_belief при действии a = (mode, v_cmd | Δ_cmd): по каждой строке x'
    x_target = transition_target(...), G = residual_backoff(...), T = dirichlet_row(...). counts[x'] -- счётчики
    переходов из x' при том же действии (нет ключа -> нули -> UNSEEN_TRANSITION). Состояния -- в шкале признака
    (иначе INVALID_OBSERVATION)."""
    a, b = scale_bounds(lo, hi)
    st = tuple(check_observation(x, a, b) for x in states)
    rows, labels = [], []
    for xp in st:
        t = transition_target(mode, a, b, x_prev=xp, v_cmd=v_cmd, d_cmd=d_cmd)
        g = residual_backoff(st, t, residual_counts, alpha)
        r = dirichlet_row(counts.get(xp, [0.0] * len(st)), g, alpha)
        rows.append(r.probs)
        labels.append(r.label)
    return tuple(rows), tuple(labels)


# ---------------------------------------------------------------- 2. Допустимость состояния
def state_distance(s: Sequence[float], s_exp: Sequence[float],
                   alpha: Sequence[float], kappa: Sequence[float]) -> float:
    """d_Σ = Σ α_j · min(1, d_j/κ_j), d_j = |s_j − s_exp_j|; κ_j > 0, α_j ≥ 0."""
    n = len(s)
    if not (len(s_exp) == len(alpha) == len(kappa) == n):
        raise ValueError("размерности s / s_exp / alpha / kappa не совпадают")
    a = _nonneg_vec(alpha, "alpha")
    k = _nonneg_vec(kappa, "kappa")
    if any(x <= 0 for x in k):
        raise ValueError("kappa_j должны быть > 0")
    total = []
    for j in range(n):
        sj, ej = _finite(s[j], "s"), _finite(s_exp[j], "s_exp")
        if math.isinf(sj) or math.isinf(ej):
            raise ValueError("s / s_exp: бесконечность в d_Σ -- отказ, не насыщение")
        d = abs(sj - ej)
        if not math.isfinite(d):
            raise ValueError("d_j не конечно (переполнение)")
        total.append(a[j] * min(1.0, d / k[j]))
    return math.fsum(total)


def state_admissibility(posterior: Sequence[float], distances: Sequence[float],
                        eps_state: Optional[float], invariants: Sequence[int]) -> float:
    """C_state = Σ_{s: d_Σ(s) ≤ ε} P(s|s_obs) ∈ [0, 1]; любой H_k = 0 -> 0."""
    for h in invariants:
        if isinstance(h, float) and math.isnan(h) or h not in (0, 1):
            raise ValueError("H_k ∈ {0, 1}")
    if any(h == 0 for h in invariants):
        return 0.0
    eps = _need(eps_state, "eps_state")
    p = _nonneg_vec(posterior, "posterior")
    d = _nonneg_vec(distances, "distances")
    if len(p) != len(d):
        raise ValueError("размерности posterior / distances не совпадают")
    s = math.fsum(p)
    if s <= 0:
        return 0.0
    c = math.fsum(p[i] for i in range(len(p)) if d[i] <= eps) / s
    if not (0.0 <= c <= 1.0):   # по построению невозможно; не обрезаем молча
        raise ValueError(f"C_state вне [0, 1]: {c!r}")
    return c


# ---------------------------------------------------------------- 2b. Этап 3b: d_∞ для решения, d_Σ -- телеметрия
ADMISSIBLE_SLACK = 1e-9   # зазор сравнения d_∞ ≤ ε_norm_j + 1e-9 (допуск d_∞, R-04; float ε_norm·κ)
ALPHA_SUM_TOL = 1e-9      # допуск Σα = 1


def validate_alpha(alpha: Sequence[float]) -> Vector:
    """α_j конечные, ≥ 0, Σα = 1 (±1e-9); иначе InvalidConfigError (INVALID_CONFIG, допуск d_∞)."""
    try:
        a = tuple(float(x) for x in alpha)
    except (TypeError, ValueError):
        raise InvalidConfigError("alpha: не числа") from None
    if not a or any(isinstance(x, bool) for x in alpha):
        raise InvalidConfigError("alpha: пусто или bool")
    if any(not math.isfinite(x) or x < 0 for x in a):
        raise InvalidConfigError("alpha: нужны конечные α_j ≥ 0")
    if abs(math.fsum(a) - 1.0) > ALPHA_SUM_TOL:
        raise InvalidConfigError("alpha: Σα ≠ 1")
    return a


def validate_eps_norm(eps_norm: Sequence[float]) -> Vector:
    """ε_norm_j ∈ [0, 1], конечные; иначе InvalidConfigError (INVALID_CONFIG)."""
    try:
        e = tuple(float(x) for x in eps_norm)
    except (TypeError, ValueError):
        raise InvalidConfigError("eps_norm: не числа") from None
    if not e or any(isinstance(x, bool) for x in eps_norm) or any(not (0.0 <= x <= 1.0) for x in e):
        raise InvalidConfigError("eps_norm: нужны ε_norm_j ∈ [0, 1]")
    return e


def _ratios(s: Sequence[float], ref: Sequence[float], kappa: Sequence[float]) -> Vector:
    """r_j = min(1, |s_j − ref_j|/κ_j); κ_j > 0 конечные (иначе INVALID_CONFIG); ∞ / переполнение -> ValueError."""
    n = len(s)
    if not (len(ref) == len(kappa) == n) or n == 0:
        raise ValueError("размерности s / ref / kappa не совпадают или пусты")
    try:
        k = _nonneg_vec(kappa, "kappa")
    except ValueError:
        raise InvalidConfigError("kappa: нужны конечные κ_j > 0") from None
    if any(x <= 0 for x in k):
        raise InvalidConfigError("kappa: нужны конечные κ_j > 0")
    out = []
    for j in range(n):
        sj, ej = _finite(s[j], "s"), _finite(ref[j], "ref")
        if math.isinf(sj) or math.isinf(ej):
            raise ValueError("s / ref: бесконечность -- отказ, не насыщение")
        d = abs(sj - ej)
        if not math.isfinite(d):
            raise ValueError("d_j не конечно (переполнение)")
        out.append(min(1.0, d / k[j]))
    return tuple(out)


def state_distance_inf(s: Sequence[float], ref: Sequence[float], kappa: Sequence[float]) -> float:
    """d_∞ = max_j min(1, |s_j − ref_j|/κ_j) (допуск d_∞, R-04): признак не маскируется суммой."""
    return max(_ratios(s, ref, kappa))


def state_admissible_inf(s: Sequence[float], ref: Sequence[float], kappa: Sequence[float],
                         eps_norm: Sequence[float]) -> bool:
    """Допуск для решения: для каждого j  min(1, |s_j − ref_j|/κ_j) ≤ ε_norm_j + 1e-9 (ε_norm по признакам,
    `01` §8 п.7). Скаляр на все признаки не используется."""
    r = _ratios(s, ref, kappa)
    e = validate_eps_norm(eps_norm)
    if len(e) != len(r):
        raise InvalidConfigError("eps_norm: размерность ≠ числу признаков")
    return all(r[j] <= e[j] + ADMISSIBLE_SLACK for j in range(len(r)))


def state_admissibility_inf(posterior: Sequence[float], states: Sequence[Sequence[float]], ref: Sequence[float],
                            kappa: Sequence[float], eps_norm: Sequence[float], invariants: Sequence[int]) -> float:
    """C_state = Σ_{s ∈ A_ε} P(s|O), A_ε = {s : state_admissible_inf(s, ref)} (этап 3b, допуск d_∞); любой H_k = 0 -> 0.
    posterior -- распределение (иначе INVALID_DISTRIBUTION)."""
    for h in invariants:
        if isinstance(h, float) and math.isnan(h) or h not in (0, 1):
            raise ValueError("H_k ∈ {0, 1}")
    if any(h == 0 for h in invariants):
        return 0.0
    p = _distribution(posterior, "posterior")
    if len(states) != len(p):
        raise ValueError("размерности posterior / states не совпадают")
    c = math.fsum(p[i] for i in range(len(p)) if state_admissible_inf(states[i], ref, kappa, eps_norm))
    if not (0.0 <= c <= 1.0 + 1e-12):   # по построению невозможно; не обрезаем молча
        raise ValueError(f"C_state вне [0, 1]: {c!r}")
    return min(c, 1.0)


def state_distance_telemetry(s: Sequence[float], ref: Sequence[float], alpha: Sequence[float],
                             kappa: Sequence[float]) -> float:
    """d_Σ = Σ α_j · min(1, |s_j − ref_j|/κ_j) -- ТОЛЬКО телеметрия фазы 3 (в решение не входит).
    α проверяется строго: α_j ≥ 0, Σα = 1, иначе InvalidConfigError (INVALID_CONFIG)."""
    a = validate_alpha(alpha)
    r = _ratios(s, ref, kappa)
    if len(a) != len(r):
        raise InvalidConfigError("alpha: размерность ≠ числу признаков")
    return math.fsum(a[j] * r[j] for j in range(len(r)))


# ---------------------------------------------------------------- 3. Пространственный вес
def _square(m: Sequence[Sequence[float]], name: str) -> Matrix:
    n = len(m)
    out = tuple(tuple(_finite(x, name) for x in row) for row in m)
    if n == 0 or any(len(r) != n for r in out) or any(math.isinf(x) for r in out for x in r):
        raise ValueError(f"{name}: нужна конечная квадратная матрица")
    return out


_SYM_RTOL = 1e-12    # допуск симметрии и ведущего элемента -- относительно max|a_ij|


def _scale(m: Matrix) -> float:
    return max(abs(x) for r in m for x in r)


def _check_symmetric(m: Matrix, name: str) -> None:
    sc = _scale(m)
    n = len(m)
    for i in range(n):
        for j in range(i + 1, n):
            if abs(m[i][j] - m[j][i]) > _SYM_RTOL * sc:
                raise CovarianceError(f"{name}: несимметрична ([{i}][{j}] ≠ [{j}][{i}])")


def _ldl(m: Matrix, name: str, shift: float = 0.0) -> Tuple[Tuple[Vector, ...], Vector]:
    """LDLᵀ (без перестановок) для симметричной m + shift·I. Ведущий элемент ≤ _SYM_RTOL·max|a| ->
    CovarianceError (вырождена или не положительно определена). Возвращает (L, D), D_k > 0."""
    n = len(m)
    sc = _scale(m) + shift
    if not (sc > 0) or not math.isfinite(sc):
        raise CovarianceError(f"{name}: нулевая или не конечная матрица")
    tol = _SYM_RTOL * sc
    L = [[0.0] * n for _ in range(n)]
    D = [0.0] * n
    for j in range(n):
        dj = m[j][j] + shift - math.fsum(L[j][k] * L[j][k] * D[k] for k in range(j))
        if not math.isfinite(dj):
            raise CovarianceError(f"{name}: переполнение при разложении")
        if dj < -tol:
            raise CovarianceError(f"{name}: не положительно полуопределена (D[{j}] < 0)")
        if dj <= tol:
            raise CovarianceError(f"{name}: численно вырождена (D[{j}] ≈ 0 при масштабе {sc:.3g})")
        D[j] = dj
        L[j][j] = 1.0
        for i in range(j + 1, n):
            v = (m[i][j] - math.fsum(L[i][k] * L[j][k] * D[k] for k in range(j))) / dj
            if not math.isfinite(v):
                raise CovarianceError(f"{name}: переполнение при разложении")
            L[i][j] = v
    return tuple(tuple(r) for r in L), tuple(D)


def _check_psd(m: Matrix, name: str) -> None:
    """Слагаемое ковариации: симметрично и положительно ПОЛУопределено (нулевая и ранга < n -- допустимы).
    Проверка: LDLᵀ для m + τ·I, τ = 1e-9·max|a| (численный допуск); отрицательное собственное число
    по модулю больше τ -> CovarianceError."""
    _check_symmetric(m, name)
    sc = _scale(m)
    if sc == 0:
        return
    _ldl(m, name, shift=1e-9 * sc)


def combined_covariance(sigma_host: Sequence[Sequence[float]], sigma_obj: Sequence[Sequence[float]],
                        sigma_cal: Sequence[Sequence[float]], eps_cov: Optional[float]) -> Matrix:
    """Σ_i = Σ_host + Σ_obj_i + Σ_cal + ε_cov·I (ε_cov > 0). Каждое слагаемое -- симметричное ПСД,
    иначе CovarianceError (невалидное слагаемое не маскируется суммой)."""
    e = _need(eps_cov, "eps_cov")
    if e <= 0:
        raise ValueError("eps_cov должен быть > 0")
    h, o, c = _square(sigma_host, "sigma_host"), _square(sigma_obj, "sigma_obj"), _square(sigma_cal, "sigma_cal")
    n = len(h)
    if len(o) != n or len(c) != n:
        raise ValueError("размерности ковариаций не совпадают")
    for m, nm in ((h, "sigma_host"), (o, "sigma_obj"), (c, "sigma_cal")):
        _check_psd(m, nm)
    out = tuple(tuple(h[i][j] + o[i][j] + c[i][j] + (e if i == j else 0.0) for j in range(n)) for i in range(n))
    if any(math.isinf(x) for r in out for x in r):
        raise CovarianceError("Σ_i: переполнение суммы")
    return out


def mahalanobis_sq(delta: Sequence[float], sigma: Sequence[Sequence[float]]) -> float:
    """r² = ΔLᵀ Σ⁻¹ ΔL через LDLᵀ: r² = Σ_k y_k²/D_k, y = L⁻¹ΔL, D_k > 0 -> r² ≥ 0 по построению.
    Σ несимметрична / не положительно определена / численно вырождена -> CovarianceError (не r² = 0)."""
    s = _square(sigma, "sigma")
    dl = tuple(_finite(x, "delta") for x in delta)
    if len(dl) != len(s) or any(math.isinf(x) for x in dl):
        raise ValueError("delta: конечный вектор размерности Σ")
    _check_symmetric(s, "sigma")
    L, D = _ldl(s, "sigma")
    n = len(s)
    y = [0.0] * n
    for i in range(n):
        y[i] = dl[i] - math.fsum(L[i][k] * y[k] for k in range(i))
        if not math.isfinite(y[i]):
            raise CovarianceError("sigma: переполнение при решении")
    r2 = math.fsum(y[k] * y[k] / D[k] for k in range(n))
    if not math.isfinite(r2):
        raise CovarianceError("r² не конечно (плохой масштаб Σ)")
    return r2


def spatial_utility(q: float, c_h: float, c_i: float, a_h: float, a_i: float, r2: float,
                    tau_h: Optional[float], tau_i: Optional[float], kappa_space: Optional[float]) -> float:
    """u_i ≥ 0. Очень старый кадр (a -> ∞) -> 0 без переполнения."""
    for v, n in ((q, "q"), (c_h, "c_h"), (c_i, "c_i")):
        v = _finite(v, n)
        if not (0.0 <= v <= 1.0):
            raise ValueError(f"{n} ∈ [0, 1]")
    for v, n in ((a_h, "a_h"), (a_i, "a_i"), (r2, "r2")):
        if _finite(v, n) < 0:
            raise ValueError(f"{n} ≥ 0")
    ks = _need(kappa_space, "kappa_space")
    f_h = decay(a_h, tau_h, "tau_h")
    f_i = decay(a_i, tau_i, "tau_i")
    x = ks * float(r2)
    f_r = 0.0 if math.isinf(x) else math.exp(-x)
    return float(q) * float(c_h) * float(c_i) * f_h * f_i * f_r


@dataclass(frozen=True)
class IntentWeights:
    weights: Vector
    outcome: Optional[Outcome]   # CLARIFY -- все u_i = 0


def intent_weights(u: Sequence[float]) -> IntentWeights:
    """w_i = u_i / Σ u_j. Σ u = 0 (или пусто) -> нули и CLARIFY, без деления на 0."""
    uu = _nonneg_vec(u, "u")
    s = math.fsum(uu)
    if not uu or not (s > 0):
        return IntentWeights(weights=tuple(0.0 for _ in uu), outcome=Outcome.CLARIFY)
    return IntentWeights(weights=tuple(x / s for x in uu), outcome=None)


def intent_vectors(weights: Sequence[float], deltas: Sequence[Sequence[float]]) -> Tuple[Vector, ...]:
    """W_i = w_i · r̂_i, r̂_i = ΔL_i/|ΔL_i| (нулевой ΔL -> нулевой вектор)."""
    if len(weights) != len(deltas):
        raise ValueError("размерности weights / deltas не совпадают")
    out = []
    for w, d in zip(weights, deltas):
        d = tuple(_finite(x, "delta") for x in d)
        norm = math.sqrt(math.fsum(x * x for x in d))
        out.append(tuple(0.0 for _ in d) if norm == 0 else tuple(float(w) * x / norm for x in d))
    return tuple(out)


# ---------------------------------------------------------------- 4. Диссонанс, гистерезис
def dissonance_step(d_prev: float, errors: Sequence[float], betas: Sequence[float],
                    dt: float, tau_D: Optional[float]) -> float:
    """D_t = λ·D_{t−1} + (1 − λ)·δ_t, δ_t = Σ β_k e_k, обе величины в [0, 1]."""
    d_prev = _finite(d_prev, "d_prev")
    if not (0.0 <= d_prev <= 1.0):
        raise ValueError("D_{t−1} ∈ [0, 1]")
    e = _nonneg_vec(errors, "errors")
    b = _nonneg_vec(betas, "betas")
    if len(e) != len(b):
        raise ValueError("размерности errors / betas не совпадают")
    delta = math.fsum(b[k] * e[k] for k in range(len(e)))
    if not (0.0 <= delta <= 1.0):      # без молчаливого среза (решение 15.11, п.3)
        raise ValueError(f"δ_t вне [0, 1]: {delta!r}")
    lam = decay(dt, tau_D, "tau_D")
    d_t = lam * d_prev + (1.0 - lam) * delta
    if not (0.0 <= d_t <= 1.0):
        raise ValueError(f"D_t вне [0, 1]: {d_t!r}")
    return d_t


class DissonanceMonitor:
    """Гистерезис D_t: тревога при D_t ≥ Θ_crit, держится до D_t < Θ_reset. K_high -- шагов подряд в тревоге."""

    def __init__(self, params: AbstractionParams) -> None:
        self.theta_crit = _need(params.theta_crit, "theta_crit")
        self.theta_reset = _need(params.theta_reset, "theta_reset")
        if self.theta_reset >= self.theta_crit:   # AbstractionParams уже проверил; защита от подмены
            raise ValueError("гистерезис: требуется Θ_reset < Θ_crit")
        self.alarm = False
        self.k_high = 0

    def update(self, d_t: float) -> bool:
        d_t = _finite(d_t, "D_t")
        if not (0.0 <= d_t <= 1.0):
            raise ValueError("D_t ∈ [0, 1]")
        if not self.alarm and d_t >= self.theta_crit:
            self.alarm = True
        elif self.alarm and d_t < self.theta_reset:
            self.alarm = False
        self.k_high = self.k_high + 1 if self.alarm else 0
        return self.alarm


# ---------------------------------------------------------------- 4b. Этап 3b: интегратор λ_0 + автомат A_t
STATE_SCHEMA = "sigma_A_t/3b-v1"


def delta_any_fail(errors: Sequence[float]) -> int:
    """δ = 1[∃k e_k > 0] (δ по любому сбою, решение 12:59): 1 на любом сбое, 0 на успехе; e_k конечные ≥ 0."""
    e = _nonneg_vec(errors, "errors")
    if not e:
        raise ValueError("errors: пустой вектор")
    return 1 if any(x > 0 for x in e) else 0


def lambda_0(params: AbstractionParams) -> float:
    """λ_0 = exp(−Δt_0/τ_D) (решение 16:16); Δt_0, τ_D -- из параметров (NO_DATA -> NoDataError)."""
    return math.exp(-_need(params.dt_0, "dt_0") / _need(params.tau_D, "tau_D"))


def dissonance_step_l0(d_prev: float, delta: int, lam0: float) -> float:
    """Шаг серии: D_n = min(1, λ_0·D_{n−1} + (1 − λ_0)·δ_n), δ_n ∈ {0, 1}, λ_0 ∈ (0, 1)."""
    d_prev = _finite(d_prev, "d_prev")
    if not (0.0 <= d_prev <= 1.0):
        raise ValueError("D_{n−1} ∈ [0, 1]")
    if isinstance(delta, bool) or delta not in (0, 1):
        raise ValueError("δ_n ∈ {0, 1}")
    lam0 = _finite(lam0, "lambda_0")
    if not (0.0 < lam0 < 1.0):
        raise ValueError("λ_0 ∈ (0, 1)")
    return min(1.0, lam0 * d_prev + (1.0 - lam0) * delta)


@dataclass(frozen=True)
class IntegratorStep:
    d: float                       # D после события (или прежний при дубле)
    alarm: bool                    # A_t после события
    delta: Optional[int]           # δ события (None -- дубль, не учтён)
    session_break: bool            # пауза Δt > τ_D перед событием (D ← D·exp(−Δt/τ_D) при A_t = 0)
    label: Optional[Label] = None  # DUPLICATE_EVENT
    frozen: bool = False           # заморозка D при A_t = 1: граница сессии при A_t = 1 -- распад не применён, D заморожен


def _ts(v: object, name: str) -> float:
    if isinstance(v, bool) or not isinstance(v, (int, float)) or not math.isfinite(v):
        raise InvalidTimestampError(f"{name}: нужен конечный момент времени")
    return float(v)


def _event_id(v: object) -> int:
    if isinstance(v, bool) or not isinstance(v, int) or v < 0:
        raise ValueError("event_id: целое ≥ 0 (seq журнала)")
    return v


class DissonanceIntegrator:
    """Интегратор λ_0 с автоматом гистерезиса A_t (этап 3b п.1–2; решения 14:51, 16:16; защёлка A_t, δ по любому сбою, распад D по паузе, темп Δt_0, длина серии,
    R-06/08/09).

    Событие (event_id = seq, ts в секундах, e_k):
      1) event_id ≤ последнего учтённого -> дубль: D, A_t, ts не меняются, метка DUPLICATE_EVENT (R-09);
      2) Δt = ts − ts_prev < 0 -> InvalidTimestampError (INVALID_TIMESTAMP -> HALT), состояние не меняется (R-08);
      3) Δt > τ_D -> граница сессии: при A_t = 0 D ← D·exp(−Δt/τ_D) (распад D по паузе); при A_t = 1 распад НЕ применяется --
         D заморожен, A_t сохраняется (заморозка D при A_t = 1, fail-closed, решение 18:21); Δt ≤ τ_D -- та же сессия,
         реальное время в D не входит (темп Δt_0);
      4) D ← min(1, λ_0·D + (1 − λ_0)·δ), δ = 1[∃k e_k > 0];
      5) A_t: 0 -> 1 при D ≥ Θ_crit; 1 -> 0 при D < Θ_reset (строго; снимают только успехи -- заморозка D при A_t = 1, точные границы порогов);
         K_high -- событий подряд в тревоге.
    Состояние -- словарь примитивов (to_state / from_state), хранится в локальном состоянии службы."""

    def __init__(self, params: AbstractionParams) -> None:
        if not isinstance(params, AbstractionParams):
            raise InvalidConfigError("params: нужен AbstractionParams")
        self.tau_D = _need(params.tau_D, "tau_D")
        self.dt_0 = _need(params.dt_0, "dt_0")
        self.theta_crit = _need(params.theta_crit, "theta_crit")
        self.theta_reset = _need(params.theta_reset, "theta_reset")
        if self.theta_reset >= self.theta_crit:   # AbstractionParams уже проверил; защита от подмены
            raise InvalidConfigError("гистерезис: требуется Θ_reset < Θ_crit")
        self.lam0 = lambda_0(params)
        self.d = 0.0
        self.alarm = False
        self.k_high = 0
        self.last_ts: Optional[float] = None
        self.last_event_id: Optional[int] = None

    # -- проекция без изменения состояния (решение перед командой в новой сессии)
    def project(self, now_ts: float) -> Tuple[float, bool]:
        """(D, A_t) на момент now_ts без события. A_t = 1 -> (D, True) при любой паузе: тишина тревогу не снимает
        (заморозка D при A_t = 1, fail-closed). A_t = 0 и пауза > τ_D -> D·exp(−Δt/τ_D) (вход в тревогу от затухания невозможен).
        Δt < 0 -> InvalidTimestampError."""
        now = _ts(now_ts, "now_ts")
        if self.last_ts is None:
            return self.d, self.alarm
        dt = now - self.last_ts
        if dt < 0:
            raise InvalidTimestampError("Δt < 0 (now_ts раньше последнего события)")
        if self.alarm:
            return self.d, True
        d = self.d * decay(dt, self.tau_D, "tau_D") if dt > self.tau_D else self.d
        return d, False

    def step(self, event_id: int, ts: float, errors: Sequence[float]) -> IntegratorStep:
        eid = _event_id(event_id)
        if self.last_event_id is not None and eid <= self.last_event_id:
            return IntegratorStep(d=self.d, alarm=self.alarm, delta=None, session_break=False,
                                  label=Label.DUPLICATE_EVENT)
        t = _ts(ts, "event_ts")
        delta = delta_any_fail(errors)
        d = self.d
        brk = frz = False
        if self.last_ts is not None:
            dt = t - self.last_ts
            if dt < 0:
                raise InvalidTimestampError("Δt < 0 (ts раньше последнего события)")
            if dt > self.tau_D:
                brk = True
                if self.alarm:
                    frz = True        # заморозка D при A_t = 1: при A_t = 1 множитель распада = 1 (D заморожен)
                else:
                    d = d * decay(dt, self.tau_D, "tau_D")
        d = dissonance_step_l0(d, delta, self.lam0)
        alarm = self.alarm
        if not alarm and d >= self.theta_crit:
            alarm = True
        elif alarm and d < self.theta_reset:
            alarm = False
        # фиксация -- только после всех проверок (ошибка выше состояние не меняет)
        self.d, self.alarm = d, alarm
        self.k_high = self.k_high + 1 if alarm else 0
        self.last_ts, self.last_event_id = t, eid
        return IntegratorStep(d=d, alarm=alarm, delta=delta, session_break=brk, frozen=frz)

    def decision_inputs(self, x: "DecisionInputs", now_ts: Optional[float] = None) -> "DecisionInputs":
        """Входы decide с D_t и защёлкой A_t ЭТОГО автомата (защёлка A_t: флаг вызывающего не нужен и не снимает
        тревогу -- итог alarm = A_t ∨ x.dissonance_alarm). now_ts -- проекция паузы (project)."""
        if not isinstance(x, DecisionInputs):
            raise TypeError("x: нужен DecisionInputs")
        d, alarm = (self.d, self.alarm) if now_ts is None else self.project(now_ts)
        if not isinstance(x.dissonance_alarm, bool):   # неверный тип флага не маскируется -> decide даст HALT
            return replace(x, d_t=d)
        return replace(x, d_t=d, dissonance_alarm=bool(alarm or x.dissonance_alarm))

    # -- сериализация (R-06)
    def to_state(self) -> dict:
        return {"schema": STATE_SCHEMA, "d": self.d, "alarm": self.alarm, "k_high": self.k_high,
                "last_ts": self.last_ts, "last_event_id": self.last_event_id,
                "tau_D": self.tau_D, "dt_0": self.dt_0, "theta_crit": self.theta_crit,
                "theta_reset": self.theta_reset}

    @classmethod
    def from_state(cls, params: AbstractionParams, state: dict) -> "DissonanceIntegrator":
        """Восстановление после перезапуска. Параметры состояния ≠ текущим -> InvalidConfigError;
        повреждённое / противоречивое состояние -> InvalidStateError (оба -> HALT)."""
        obj = cls(params)
        if not isinstance(state, dict) or state.get("schema") != STATE_SCHEMA:
            raise InvalidStateError("state: нет схемы " + STATE_SCHEMA)
        for k in ("tau_D", "dt_0", "theta_crit", "theta_reset"):
            v = state.get(k)
            if isinstance(v, bool) or not isinstance(v, (int, float)) or float(v) != getattr(obj, k):
                raise InvalidConfigError(f"state: параметр {k} не совпадает с текущими параметрами")
        d, alarm, kh = state.get("d"), state.get("alarm"), state.get("k_high")
        if isinstance(d, bool) or not isinstance(d, (int, float)) or not math.isfinite(d) or not (0.0 <= d <= 1.0):
            raise InvalidStateError("state: D вне [0, 1]")
        if not isinstance(alarm, bool):
            raise InvalidStateError("state: alarm не bool")
        if isinstance(kh, bool) or not isinstance(kh, int) or kh < 0 or (kh == 0) == alarm:
            raise InvalidStateError("state: K_high не согласован с alarm")
        if (not alarm and d >= obj.theta_crit) or (alarm and d < obj.theta_reset):
            raise InvalidStateError("state: A_t противоречит D и порогам")
        lt, le = state.get("last_ts"), state.get("last_event_id")
        if (lt is None) != (le is None):
            raise InvalidStateError("state: last_ts и last_event_id -- оба или ни одного")
        if lt is not None:
            try:
                lt = _ts(lt, "last_ts")
                le = _event_id(le)
            except ValueError as e:
                raise InvalidStateError(f"state: {e}") from None
        obj.d, obj.alarm, obj.k_high = float(d), alarm, kh
        obj.last_ts, obj.last_event_id = lt, le
        return obj


# ---------------------------------------------------------------- 4c. Этап 3c: параметры ядра из файла параметров
PARAMS_USABLE = ("MANUAL", "JOURNAL")       # статусы со значением; NO_DATA / MANUAL_PENDING -> None (ядро даст HALT)
_DOC_KEYS = (("tau_b", "tau_b"), ("tau_D", "tau_D"), ("tau_h", "tau_h"), ("tau_i", "tau_i"),
             ("kappa_space", "kappa_space"), ("eps_cov", "eps_cov"), ("eps_state", "eps_state"),
             ("theta_state", "Theta_state"), ("theta_intent", "Theta_intent"), ("theta_margin", "Theta_margin"),
             ("theta_goal", "Theta_goal"), ("theta_crit", "Theta_crit"), ("theta_reset", "Theta_reset"))


def _doc_num(entry: object) -> Optional[float]:
    if not isinstance(entry, dict) or entry.get("status") not in PARAMS_USABLE:
        return None
    v = entry.get("value")
    return float(v) if isinstance(v, (int, float)) and not isinstance(v, bool) and math.isfinite(v) else None


def params_from_doc(doc: object) -> AbstractionParams:
    """AbstractionParams из документа config/sigma_params.json (этап 3c, решение 18:21 п.3): числа -- как в файле,
    NO_DATA / MANUAL_PENDING -> None; Δt_0 -- раздел `dt_0` (MANUAL, переносит calibrate_sigma.py из ручного
    файла). Нет раздела -> dt_0 = NO_DATA -> интегратор не создаётся (NoDataError -> HALT). Значение вне
    допустимого (≤ 0, Θ вне [0, 1]) -> InvalidConfigError."""
    if not isinstance(doc, dict) or not isinstance(doc.get("params"), dict):
        raise InvalidConfigError("файл параметров: нет объекта params")
    p = doc["params"]
    kw = {name: _doc_num(p.get(key)) for name, key in _DOC_KEYS}
    kw["dt_0"] = _doc_num(doc.get("dt_0"))
    try:
        return AbstractionParams(**kw)
    except ValueError as e:
        raise InvalidConfigError(f"файл параметров: {e}") from None


# ---------------------------------------------------------------- 5. Бюджет R_t
class BudgetExhausted(RuntimeError):
    pass


@dataclass(frozen=True)
class Budget:
    """R_t = N_obs + N_route + N_reflect. Каждый шаг тратит 1 -> R_{t+1} < R_t."""
    n_obs: int
    n_route: int
    n_reflect: int

    def __post_init__(self) -> None:
        for f in fields(self):
            v = getattr(self, f.name)
            if isinstance(v, bool) or not isinstance(v, int) or v < 0:
                raise ValueError(f"{f.name}: целое ≥ 0")

    @property
    def remaining(self) -> int:
        return self.n_obs + self.n_route + self.n_reflect

    def spend(self, kind: str) -> "Budget":
        if kind not in ("obs", "route", "reflect"):
            raise ValueError("kind ∈ {obs, route, reflect}")
        name = "n_" + kind
        if getattr(self, name) <= 0:
            raise BudgetExhausted(f"{name} = 0")
        vals = {f.name: getattr(self, f.name) for f in fields(self)}
        vals[name] -= 1
        return Budget(**vals)


# ---------------------------------------------------------------- 6. Решение
@dataclass(frozen=True)
class DecisionInputs:
    c_state: float
    q_best: float
    q_second: float
    c_goal: float
    d_t: float
    authorization: int
    safety_gate: int
    budget_remaining: int
    dissonance_alarm: bool = False   # защёлка DissonanceMonitor (гистерезис)
    n_hyp: Optional[int] = None      # число гипотез намерения (этап 3b, R-07): < 2 -> CLARIFY; None -- не передано


@dataclass(frozen=True)
class Decision:
    outcome: Outcome
    reasons: Tuple[str, ...]


def _is_one(v: object) -> bool:
    """Строго 1 (int) или True. 0.999, «1», None -> нет."""
    return v is True or (type(v) is int and v == 1)


def _unit_ok(v: object) -> bool:
    """Конечное число в [0, 1] (bool -- не число)."""
    return (not isinstance(v, bool) and isinstance(v, (int, float)) and math.isfinite(v)
            and 0.0 <= v <= 1.0)


def decide(params: AbstractionParams, x: DecisionInputs) -> Decision:
    """EXECUTE только при всех условиях §3 сразу. Иначе (порядок -- от жёсткого к мягкому):
    NO_DATA любого порога (включая Θ_reset) / вход вне [0, 1] или нечисло / R_t = 0 /
    Authorization≠1 / SafetyGate≠1 -> HALT;
    D_t ≥ Θ_crit или защёлка гистерезиса / C_state < Θ_state -> OBSERVE;
    N_hyp < 2 (R-07) / q_best < Θ_intent / зазор < Θ_margin / C_goal < Θ_goal -> CLARIFY.
    n_hyp: None -- не передано (контракт 1d сохранён; router этапа 3d обязан передавать, как и защёлку);
    не целое ≥ 0 -> HALT BAD_INPUT:n_hyp."""
    if not isinstance(params, AbstractionParams) or not isinstance(x, DecisionInputs):
        return Decision(Outcome.HALT, ("BAD_INPUT",))
    missing = tuple(n for n in DECISION_THRESHOLDS if getattr(params, n) is None)
    if missing:
        return Decision(Outcome.HALT, tuple("NO_DATA:" + n for n in missing))
    bad = tuple("BAD_INPUT:" + n for n in DECISION_INPUTS_UNIT if not _unit_ok(getattr(x, n)))
    nh = x.n_hyp
    nh_bad = nh is not None and (isinstance(nh, bool) or not isinstance(nh, int) or nh < 0)
    if bad or not isinstance(x.dissonance_alarm, bool) or nh_bad:
        return Decision(Outcome.HALT, ("BAD_INPUT",) + bad + (() if isinstance(x.dissonance_alarm, bool)
                                                              else ("BAD_INPUT:dissonance_alarm",))
                        + (("BAD_INPUT:n_hyp",) if nh_bad else ()))
    if not isinstance(x.budget_remaining, int) or isinstance(x.budget_remaining, bool) or x.budget_remaining <= 0:
        return Decision(Outcome.HALT, ("BUDGET_EXHAUSTED",))
    hard = []
    if not _is_one(x.authorization):
        hard.append("NO_AUTHORIZATION")
    if not _is_one(x.safety_gate):
        hard.append("SAFETY_GATE")
    if hard:
        return Decision(Outcome.HALT, tuple(hard))
    observe = []
    if x.dissonance_alarm or not (x.d_t < params.theta_crit):
        observe.append("DISSONANCE")
    if not (x.c_state >= params.theta_state):
        observe.append("STATE")
    if observe:
        return Decision(Outcome.OBSERVE, tuple(observe))
    clarify = []
    if nh is not None and nh < 2:
        clarify.append("N_HYP")
    if not (x.q_best >= params.theta_intent):
        clarify.append("INTENT")
    if not ((x.q_best - x.q_second) >= params.theta_margin):
        clarify.append("MARGIN")
    if not (x.c_goal >= params.theta_goal):
        clarify.append("GOAL")
    if clarify:
        return Decision(Outcome.CLARIFY, tuple(clarify))
    return Decision(Outcome.EXECUTE, ())


# ---------------------------------------------------------------- 7. Обёртка: ошибки ядра -> HALT
def guarded_decide(params: AbstractionParams, compute_inputs: Callable[[], DecisionInputs]) -> Decision:
    """Решение с изоляцией ошибок ядра (решение 15.11, п.5). compute_inputs() считает входы решения
    функциями этого модуля (вера, d_Σ, r², u_i, D_t ...). Любое исключение (вырождение, плохой масштаб,
    переполнение, NO_DATA параметра формулы, исчерпанный бюджет, неверный тип) наружу не выходит:
    исход HALT с причиной KERNEL_ERROR:<класс> (NO_DATA -> NO_DATA:kernel). Не EXECUTE никогда.
    Этап 3b: ошибки с меткой (INVALID_TIMESTAMP / INVALID_DISTRIBUTION / INVALID_CONFIG / INVALID_STATE)
    -> HALT с этой меткой как причиной."""
    try:
        x = compute_inputs()
    except NoDataError:
        return Decision(Outcome.HALT, ("NO_DATA:kernel",))
    except _LABELED_ERRORS as e:
        return Decision(Outcome.HALT, (e.label.value,))
    except Exception as e:  # noqa: BLE001 -- контракт: ядро не роняет вызывающего
        return Decision(Outcome.HALT, ("KERNEL_ERROR:" + type(e).__name__,))
    if not isinstance(x, DecisionInputs):
        return Decision(Outcome.HALT, ("KERNEL_ERROR:BAD_RESULT",))
    try:
        return decide(params, x)
    except Exception as e:  # noqa: BLE001
        return Decision(Outcome.HALT, ("KERNEL_ERROR:" + type(e).__name__,))


def guarded_decide_event(params: AbstractionParams, integrator: DissonanceIntegrator, event_id: int, ts: float,
                         errors: Sequence[float], compute_inputs: Callable[[], DecisionInputs]) -> Decision:
    """Этап 3b: событие (исход прошлой команды) -> интегратор λ_0 / A_t -> решение со входами этого автомата.
    Δt < 0 -> HALT INVALID_TIMESTAMP (состояние не меняется, R-08); дубль event_id -> D не меняется (R-09);
    флаг вызывающего тревогу A_t не снимает (защёлка A_t). Любая ошибка -> HALT, не EXECUTE."""
    def build() -> DecisionInputs:
        if not isinstance(integrator, DissonanceIntegrator):
            raise InvalidConfigError("integrator: нужен DissonanceIntegrator")
        integrator.step(event_id, ts, errors)
        return integrator.decision_inputs(compute_inputs())
    return guarded_decide(params, build)
