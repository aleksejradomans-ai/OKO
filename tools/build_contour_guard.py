"""Build contour guard (B33 v2 attempt 4, Operator rule 25.11 10:47: the build contour is ASCII; selfrun #2 root cause).

Static checks, standard library only, nothing is written:

  python tools/build_contour_guard.py --root <repo or block files dir> [--ps <block .ps1/.cmd> ...]

Python part (under --root):
  ascii_cli       a non-docstring string literal with a non-ASCII character in a CLI of the block (CLI_FILES); a data
                  literal (pattern of a Russian document, text of docs/CONTROL_MAP.md) is allowed only with the marker
                  comment "# non-ascii-data" on its line or on the def line of the enclosing function;
  implicit_decode subprocess output decoded without an explicit encoding in src/test_*.py and tools/*.py:
                  subprocess.* / check_output with text=True or universal_newlines=True and no encoding=, or a
                  zero-argument .decode() on .stdout / .stderr / check_output(...) / a git helper call;
  bare_child      in HERMETIC_FILES a subprocess call without env= (or **kwargs carrying it): the child would inherit
                  PYTHONHOME / PYTHONPATH / PYTEST_ADDOPTS / OKO_B33_* of the parent (selfrun / Operator profile).
PowerShell part (--ps, every file; the dot-sourced *_common.ps1 is read together with each script):
  ascii_bytes     any non-ASCII byte (a UTF-8 BOM at the start is allowed);
  top_case        a script-scope variable written in two spellings that differ only by case (PowerShell variable
                  names are case-insensitive: `foreach ($l in ...)` overwrote the script log $L, selfrun #2);
  top_loop        a loop variable at script scope named like a script variable that functions read;
  dyn_shadow      a function-local variable / parameter named like a script variable that a function it calls
                  reads (dynamic scoping: the callee sees the caller's local);
  log_var         the script log $L assigned anywhere except its one definition;
  flat_pair       `@( @(...) )` with a single inner array (PowerShell flattens it; needs `@( ,@(...) )`).
Output: one line per finding `CONTOUR <rule> <file>:<line> <detail>` and `CONTOUR_SUMMARY files=<n> findings=<n>`
(ASCII only). Exit code: 0 -- no finding; 1 -- findings; 2 -- a file does not parse / is missing."""
import argparse
import ast
import re
import sys
from pathlib import Path

CLI_FILES = ("tools/weakening_guard.py", "tools/guard_mutation_smoke.py", "tools/impl_mutation_smoke.py",
             "tools/verify_control_map.py", "tools/consent_lines.py", "tools/push_record.py", "tools/notify.py",
             "src/weakening_guard.py", "tools/build_contour_guard.py")
# block tests and tools whose children must get a hermetic environment (item 3): every subprocess call passes env=
HERMETIC_FILES = ("src/test_net_guard.py", "src/test_verify_control_map.py", "src/test_weakening_guard_b30.py",
                  "src/test_weakening_guard_b33.py", "src/test_consent_b33.py", "src/test_consent_b30.py",
                  "src/test_push_record_b33.py", "src/test_log_no_text.py", "tools/weakening_guard.py",
                  "tools/verify_control_map.py", "tools/impl_mutation_smoke.py")
DATA_MARK = "# non-ascii-data"
GIT_HELPERS = {"check_output", "_git", "git", "gout", "run_git", "_wgit"}
AUTO_VARS = {"_", "args", "true", "false", "null", "matches", "lastexitcode", "erroractionpreference", "psscriptroot",
             "input", "this", "pid", "psversiontable", "error", "home", "host", "pscmdlet", "myinvocation", "psitem",
             "foreach", "pwd", "profile", "ofs", "executioncontext", "psboundparameters", "pscommandpath",
             "verbosepreference", "warningpreference", "progresspreference", "informationpreference"}
LOG_NAME = "l"


def say(s: str) -> None:
    sys.stdout.buffer.write((s + "\n").encode("ascii", "backslashreplace"))
    sys.stdout.buffer.flush()


# ------------------------------------------------------------------ python
def _docstring_ids(tree) -> set:
    ids = set()
    for n in ast.walk(tree):
        if isinstance(n, (ast.Module, ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)) and n.body:
            b = n.body[0]
            if isinstance(b, ast.Expr) and isinstance(b.value, ast.Constant) and isinstance(b.value.value, str):
                ids.add(id(b.value))
    return ids


def ascii_cli(rel: str, src: str) -> list:
    tree = ast.parse(src)
    lines = src.splitlines()
    ds = _docstring_ids(tree)
    marked_fn = []
    for n in ast.walk(tree):
        if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)) and DATA_MARK in lines[n.lineno - 1]:
            marked_fn.append((n.lineno, getattr(n, "end_lineno", n.lineno)))
    out = []
    for n in ast.walk(tree):
        if not (isinstance(n, ast.Constant) and isinstance(n.value, str)) or id(n) in ds:
            continue
        if all(ord(c) < 128 for c in n.value):
            continue
        ln = n.lineno
        end = getattr(n, "end_lineno", ln) or ln
        if any(DATA_MARK in lines[i - 1] for i in range(ln, end + 1)) or any(a <= ln <= b for a, b in marked_fn):
            continue
        out.append(("ascii_cli", rel, ln, "non-ASCII string literal in a block CLI (output must be ASCII)"))
    return out


def _call_name(f) -> str:
    if isinstance(f, ast.Attribute):
        return f.attr
    if isinstance(f, ast.Name):
        return f.id
    return ""


def _is_subprocess_call(c) -> bool:
    f = c.func
    if isinstance(f, ast.Attribute) and isinstance(f.value, ast.Name) and f.value.id == "subprocess":
        return f.attr in ("run", "Popen", "check_output", "call", "check_call")
    return isinstance(f, ast.Name) and f.id in ("check_output", "Popen")


def _receiver_is_process_output(r) -> bool:
    while True:
        if isinstance(r, ast.Attribute):
            if r.attr in ("stdout", "stderr"):
                return True
            r = r.value
        elif isinstance(r, ast.Call):
            if _is_subprocess_call(r) or _call_name(r.func) in GIT_HELPERS:
                return True
            r = r.func
        elif isinstance(r, ast.Subscript):
            r = r.value
        else:
            return False


def implicit_decode(rel: str, src: str) -> list:
    out = []
    for n in ast.walk(ast.parse(src)):
        if not isinstance(n, ast.Call):
            continue
        if _is_subprocess_call(n):
            kw = {k.arg: k.value for k in n.keywords if k.arg}
            txt = any(isinstance(kw.get(k), ast.Constant) and kw[k].value is True for k in ("text", "universal_newlines"))
            if txt and "encoding" not in kw:
                out.append(("implicit_decode", rel, n.lineno, "text=True without encoding= (locale code page on Windows)"))
        f = n.func
        if isinstance(f, ast.Attribute) and f.attr == "decode" and not n.args and not n.keywords \
                and _receiver_is_process_output(f.value):
            out.append(("implicit_decode", rel, n.lineno, "zero-argument .decode() of process output"))
    return out


def bare_child(rel: str, src: str) -> list:
    out = []
    for n in ast.walk(ast.parse(src)):
        if isinstance(n, ast.Call) and _is_subprocess_call(n):
            if not any(k.arg == "env" or k.arg is None for k in n.keywords):
                out.append(("bare_child", rel, n.lineno, "child process without env= (hermetic environment needed)"))
    return out


def scan_python(root: Path) -> tuple:
    found, bad = [], []
    for rel in CLI_FILES:
        p = root / rel
        if p.is_file():
            try:
                found += ascii_cli(rel, p.read_text(encoding="utf-8-sig"))
            except (SyntaxError, UnicodeDecodeError) as e:
                bad.append(f"{rel}: {type(e).__name__}")
    files = sorted(set((root / "src").glob("test_*.py")) | set((root / "tools").glob("*.py")))
    for p in files:
        rel = p.relative_to(root).as_posix()
        try:
            src = p.read_text(encoding="utf-8-sig")
            found += implicit_decode(rel, src)
            if rel in HERMETIC_FILES:
                found += bare_child(rel, src)
        except (SyntaxError, UnicodeDecodeError) as e:
            bad.append(f"{rel}: {type(e).__name__}")
    return found, bad, len(files)


# ------------------------------------------------------------------ powershell tokenizer
_VAR_RX = re.compile(r"\$(?:\{([^}]+)\}|((?:script|global|local|private|env|using):)?([A-Za-z_][A-Za-z0-9_]*))")
_WORD_RX = re.compile(r"[A-Za-z_][A-Za-z0-9_\-]*")


class Tok:
    __slots__ = ("kind", "text", "scope", "line", "pos", "in_str")

    def __init__(self, kind, text, line, pos, scope="", in_str=False):
        self.kind, self.text, self.line, self.pos, self.scope, self.in_str = kind, text, line, pos, scope, in_str


def ps_tokens(src: str) -> list:
    """Tokens of PowerShell source: var / word / punct; strings and comments are skipped except the variables and
    $( ) subexpressions inside double-quoted strings (marked in_str)."""
    toks, i, n, line = [], 0, len(src), 1

    def var_at(j, in_str):
        m = _VAR_RX.match(src, j)
        if not m:
            return None
        if m.group(1):
            return Tok("var", m.group(1), line, j, "", in_str), m.end()
        return Tok("var", m.group(3), line, j, (m.group(2) or "").rstrip(":").lower(), in_str), m.end()

    def sub_end(j):
        """j at '(' after '$' inside a string -> index after the matching ')' (strings inside skipped)."""
        depth, k = 0, j
        while k < n:
            c = src[k]
            if c == "(":
                depth += 1
            elif c == ")":
                depth -= 1
                if depth == 0:
                    return k + 1
            elif c == "'":
                k = src.find("'", k + 1)
                if k < 0:
                    return n
            k += 1
        return n

    while i < n:
        c = src[i]
        if c == "\n":
            line += 1
            i += 1
        elif c == "#":
            e = src.find("\n", i)
            i = n if e < 0 else e
        elif src.startswith("<#", i):
            e = src.find("#>", i + 2)
            e = n if e < 0 else e + 2
            line += src.count("\n", i, e)
            i = e
        elif src.startswith("@'", i) and src[i + 2:i + 3] in ("\r", "\n"):
            m = re.compile(r"\n'@").search(src, i)
            e = n if not m else m.end()
            line += src.count("\n", i, e)
            i = e
        elif src.startswith('@"', i) and src[i + 2:i + 3] in ("\r", "\n"):
            m = re.compile(r'\n"@').search(src, i)
            e = n if not m else m.end()
            body_toks = _string_vars(src, i + 2, e, line)
            toks += body_toks
            line += src.count("\n", i, e)
            i = e
        elif c == "'":
            k = i + 1
            while k < n:
                if src[k] == "'" and src[k + 1:k + 2] == "'":
                    k += 2
                    continue
                if src[k] == "'":
                    break
                k += 1
            line += src.count("\n", i, k)
            i = k + 1
        elif c == '"':
            k = i + 1
            while k < n:
                if src[k] == "`":
                    k += 2
                    continue
                if src[k] == "$" and src[k + 1:k + 2] == "(":
                    k = sub_end(k + 1)
                    continue
                if src[k] == '"':
                    break
                k += 1
            toks += _string_vars(src, i + 1, k, line)
            line += src.count("\n", i, k)
            i = k + 1
        elif c == "$":
            r = var_at(i, False)
            if r:
                toks.append(r[0])
                i = r[1]
            else:
                toks.append(Tok("punct", c, line, i))
                i += 1
        elif c.isalpha() or c == "_":
            m = _WORD_RX.match(src, i)
            toks.append(Tok("word", m.group(0), line, i))
            i = m.end()
        elif c in " \t\r":
            i += 1
        else:
            if c == "@" and src[i + 1:i + 2] == "(":
                toks.append(Tok("punct", "@(", line, i))
                i += 2
                continue
            two = src[i:i + 2]
            if two in ("+=", "-=", "*=", "/=", "%=", "==", "++", "--"):
                toks.append(Tok("punct", two, line, i))
                i += 2
                continue
            toks.append(Tok("punct", c, line, i))
            i += 1
    return toks


def _string_vars(src: str, a: int, b: int, line: int) -> list:
    """Variables and $( ) code inside a double-quoted string body src[a:b] (reads only)."""
    out, k = [], a
    while k < b:
        if src[k] == "`":
            k += 2
            continue
        if src[k] == "\n":
            line += 1
        if src[k] == "$":
            if src[k + 1:k + 2] == "(":
                depth, j = 0, k + 1
                while j < b:
                    if src[j] == "(":
                        depth += 1
                    elif src[j] == ")":
                        depth -= 1
                        if depth == 0:
                            break
                    j += 1
                for t in ps_tokens(src[k + 2:j]):
                    t.line += line - 1
                    t.in_str = True
                    out.append(t)
                line += src.count("\n", k, j)
                k = j + 1
                continue
            m = _VAR_RX.match(src, k)
            if m:
                if m.group(1):
                    out.append(Tok("var", m.group(1), line, k, "", True))
                else:
                    out.append(Tok("var", m.group(3), line, k, (m.group(2) or "").rstrip(":").lower(), True))
                k = m.end()
                continue
        k += 1
    return out


class Func:
    def __init__(self, name, line, file):
        self.name, self.line, self.file = name, line, file
        self.params, self.assigns, self.loops, self.reads, self.calls = [], [], [], [], set()


def ps_structure(src: str, file: str) -> tuple:
    """-> (functions {lower name: Func}, top-level writes [(kind, name, line)], flat-pair lines)."""
    toks = ps_tokens(src)
    funcs, top, flat = {}, [], []
    stack = []                                         # brace stack: Func or None
    pending = None                                     # Func waiting for its body brace
    paren_params = None                                # Func whose (params) are being read
    pdepth = 0
    i = 0
    while i < len(toks):
        t = toks[i]
        cur = next((f for f in reversed(stack) if f is not None), None)
        if t.kind == "word" and t.text.lower() == "function" and not t.in_str and i + 1 < len(toks) and toks[i + 1].kind == "word":
            f = Func(toks[i + 1].text, t.line, file)
            funcs[f.name.lower()] = f
            pending = f
            i += 2
            if i < len(toks) and toks[i].text == "(":
                paren_params, pdepth = f, 1
                i += 1
            continue
        if paren_params is not None:
            if t.text == "(":
                pdepth += 1
            elif t.text == ")":
                pdepth -= 1
                if pdepth == 0:
                    paren_params = None
            elif t.kind == "var" and pdepth == 1 and _prev_is_param_start(toks, i):
                paren_params.params.append((t.text, t.line))
            i += 1
            continue
        if t.text == "{" and t.kind == "punct":
            stack.append(pending)
            pending = None
            i += 1
            continue
        if t.text == "}" and t.kind == "punct":
            if stack:
                stack.pop()
            i += 1
            continue
        if t.kind == "word" and t.text.lower() == "param" and cur is not None and i + 1 < len(toks) and toks[i + 1].text == "(":
            depth, j = 0, i + 1
            while j < len(toks):
                if toks[j].text == "(":
                    depth += 1
                elif toks[j].text == ")":
                    depth -= 1
                    if depth == 0:
                        break
                elif toks[j].kind == "var" and depth == 1 and _prev_is_param_start(toks, j):
                    cur.params.append((toks[j].text, toks[j].line))
                j += 1
            i = j + 1
            continue
        if t.kind == "punct" and t.text == "@(" and i + 1 < len(toks) and toks[i + 1].text == "@(":
            depth, j = 1, i + 2
            while j < len(toks) and depth:
                if toks[j].text in ("(", "@("):
                    depth += 1
                elif toks[j].text == ")":
                    depth -= 1
                j += 1
            if j < len(toks) and toks[j].text == ")":
                flat.append(t.line)
        if t.kind == "word" and t.text.lower() == "foreach" and i + 3 < len(toks) and toks[i + 1].text == "(" \
                and toks[i + 2].kind == "var" and toks[i + 3].kind == "word" and toks[i + 3].text.lower() == "in":
            v = toks[i + 2]
            if cur is None:
                top.append(("loop", v.text, v.line, v.scope))
            elif not v.scope:
                cur.loops.append((v.text, v.line))
            i += 3
            continue
        if t.kind == "var" and not t.in_str:
            nx = toks[i + 1] if i + 1 < len(toks) else None
            pv = toks[i - 1] if i else None
            is_asg = nx is not None and nx.kind == "punct" and nx.text in ("=", "+=", "-=", "*=", "/=", "%=") \
                and not (pv is not None and pv.kind == "punct" and pv.text in (".", "::"))
            if is_asg:
                if cur is None:
                    top.append(("assign", t.text, t.line, t.scope))
                elif not t.scope:
                    cur.assigns.append((t.text, t.line))
                elif t.scope in ("script", "global") and t.text.lower() == LOG_NAME:
                    cur.assigns.append(("script:" + t.text, t.line))
            elif cur is not None and t.scope in ("", "local", "private"):
                cur.reads.append((t.text, t.line))
        elif t.kind == "var" and cur is not None and t.scope in ("", "local", "private"):
            cur.reads.append((t.text, t.line))
        if t.kind == "word" and cur is not None:
            cur.calls.add(t.text.lower())
        i += 1
    return funcs, top, flat


def _prev_is_param_start(toks, j) -> bool:
    """A variable in a parameter list names a parameter when it follows '(' / ',' / a [type] / an [attribute]."""
    k = j - 1
    while k >= 0 and toks[k].text == "]":
        depth = 0
        while k >= 0:
            if toks[k].text == "]":
                depth += 1
            elif toks[k].text == "[":
                depth -= 1
                if depth == 0:
                    break
            k -= 1
        k -= 1
    return k >= 0 and toks[k].text in ("(", ",")


def ps_check(files: list, common_names: tuple = ("_common.ps1",)) -> tuple:
    found, bad = [], []
    texts = {}
    for p in files:
        try:
            b = Path(p).read_bytes()
        except OSError as e:
            bad.append(f"{p}: {type(e).__name__}")
            continue
        body = b[3:] if b.startswith(b"\xef\xbb\xbf") else b
        for ln, row in enumerate(body.split(b"\n"), 1):
            if any(x > 127 for x in row):
                found.append(("ascii_bytes", Path(p).name, ln, "non-ASCII byte in a block script"))
        texts[Path(p).name] = body.decode("ascii", "replace").replace("\r\n", "\n")
    commons = [n for n in texts if n.endswith(common_names)]
    for name, text in texts.items():
        if not name.lower().endswith(".ps1") or name in commons:
            continue
        units = [(c, texts[c]) for c in commons if ("\\" + c) in text or ('"' + c + '"') in text] + [(name, text)]
        found += _ps_rules(units)
    for c in commons:
        found += _ps_rules([(c, texts[c])])
    seen, uniq = set(), []
    for f in found:
        if f not in seen:
            seen.add(f)
            uniq.append(f)
    return uniq, bad


def _ps_rules(units: list) -> list:
    out = []
    funcs, top, flats = {}, [], []
    for fname, text in units:
        fu, tp, fl = ps_structure(text, fname)
        funcs.update(fu)
        top += [(k, n, ln, sc, fname) for k, n, ln, sc in tp]
        flats += [(fname, ln) for ln in fl]
    for fname, ln in flats:
        out.append(("flat_pair", fname, ln, "@( @(...) ) with one inner array flattens; use @( ,@(...) )"))
    spell = {}
    for k, n, ln, sc, fname in top:
        if sc in ("", "script", "global"):
            spell.setdefault(n.lower(), {}).setdefault(n, (fname, ln))
    topnames = set(spell)
    for low, sp in spell.items():
        if len(sp) > 1:
            first = min(sp.values(), key=lambda x: (x[0] != units[0][0], x[1]))
            for n, (fname, ln) in sp.items():
                if (fname, ln) != first:
                    out.append(("top_case", fname, ln, f"${n} differs only by case from ${[m for m in sp if sp[m] == first][0]}"))
    free = {}
    for f in funcs.values():
        local = {x[0].lower() for x in f.params + f.assigns + f.loops if not x[0].startswith("script:")}
        free[f.name.lower()] = {r[0].lower() for r in f.reads if r[0].lower() not in local} - AUTO_VARS
    closure = {}

    def reach(fn, seen):
        if fn in closure:
            return closure[fn]
        if fn in seen:
            return set()
        seen.add(fn)
        acc = set(free.get(fn, set()))
        for c in funcs[fn].calls:
            if c in funcs and c != fn:
                acc |= reach(c, seen)
        closure[fn] = acc
        return acc

    for fn in funcs:
        reach(fn, set())
    read_by_funcs = set().union(*free.values()) if free else set()
    for k, n, ln, sc, fname in top:
        if k == "loop" and n.lower() in topnames and n.lower() in read_by_funcs:
            out.append(("top_loop", fname, ln, f"loop variable ${n} at script scope is a script variable read by functions"))
    for k, n, ln, sc, fname in top:
        if n.lower() == LOG_NAME:
            is_def = (k == "assign" and n == "L" and sc == "" and fname.endswith("_common.ps1"))
            if not is_def:
                out.append(("log_var", fname, ln, f"${n} assigned at script scope (the script log $L)"))
    log_defs = [x for x in top if x[1].lower() == LOG_NAME and x[0] == "assign" and x[1] == "L" and x[4].endswith("_common.ps1")]
    if len(log_defs) > 1:
        for x in log_defs[1:]:
            out.append(("log_var", x[4], x[2], "$L defined more than once"))
    for f in funcs.values():
        callees = set()
        for c in f.calls:
            if c in funcs and c != f.name.lower():
                callees |= closure.get(c, set())
        for kind, items in (("param", f.params), ("assign", f.assigns), ("loop", f.loops)):
            for n, ln in items:
                low = n.lower().replace("script:", "")
                if low == LOG_NAME:
                    out.append(("log_var", f.file, ln, f"{kind} ${n} in {f.name} shadows / rewrites the script log $L"))
                elif low in topnames and low in callees:
                    out.append(("dyn_shadow", f.file, ln, f"{kind} ${n} in {f.name} shadows the script variable read by a callee"))
    return out


def main(argv=None) -> int:
    for s in (sys.stdout, sys.stderr):
        try:
            s.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError):
            pass
    ap = argparse.ArgumentParser(prog="build_contour_guard")
    ap.add_argument("--root", default="")
    ap.add_argument("--ps", nargs="*", default=[])
    a = ap.parse_args(argv)
    found, bad, nfiles = [], [], 0
    if a.root:
        f, b, n = scan_python(Path(a.root))
        found, bad, nfiles = found + f, bad + b, nfiles + n
    if a.ps:
        f, b = ps_check(a.ps)
        found, bad, nfiles = found + f, bad + b, nfiles + len(a.ps)
    for rule, rel, ln, why in found:
        say(f"CONTOUR {rule} {rel}:{ln} {why}")
    for x in bad:
        say(f"CONTOUR_ERROR {x}")
    say(f"CONTOUR_SUMMARY files={nfiles} findings={len(found)} errors={len(bad)}")
    return 2 if bad else (1 if found else 0)


if __name__ == "__main__":
    sys.exit(main())
