"""av_compat: подмена av.open только при несовместимом PyAV; stt.py подключает её (29.10.2026)."""
import ast
import pathlib
import types

import av_compat


class InvalidDataError(Exception):
    pass


def _fake_av(accepts: bool):
    calls = []

    def open_(src, mode="r", **kw):
        if "metadata_errors" in kw and not accepts:
            raise TypeError("open() got an unexpected keyword argument 'metadata_errors'")
        calls.append(kw)
        raise InvalidDataError("empty")

    return types.SimpleNamespace(open=open_, __version__="19.0.2"), calls


def test_incompatible_av_gets_patched_and_kwarg_dropped():
    av, calls = _fake_av(accepts=False)
    assert av_compat.apply(av) is True
    try:
        av.open("x.ogg", mode="r", metadata_errors="ignore")
    except InvalidDataError:
        pass
    assert calls[-1] == {}


def test_compatible_av_untouched():
    av, _ = _fake_av(accepts=True)
    orig = av.open
    assert av_compat.apply(av) is False
    assert av.open is orig


def test_apply_idempotent():
    av, _ = _fake_av(accepts=False)
    av_compat.apply(av)
    first = av.open
    assert av_compat.apply(av) is True
    assert av.open is first


def test_other_typeerror_not_treated_as_incompatibility():
    def open_(*a, **k):
        raise TypeError("something else")
    assert av_compat.rejects_metadata_errors(types.SimpleNamespace(open=open_)) is False


def test_stt_uses_av_compat_not_inline_patch():
    src = pathlib.Path(__file__).with_name("stt.py").read_text(encoding="utf-8-sig")
    tree = ast.parse(src)
    assert "_patched_av_open" not in src
    calls = [ast.unparse(n) for n in tree.body if isinstance(n, ast.Expr)]
    assert "av_compat.apply()" in calls
