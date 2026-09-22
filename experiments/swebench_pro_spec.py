#!/usr/bin/env python3
"""Pro TestSpec and log parsers adapted from the validated MetaGPT adapter.

Host-side evaluation only: never imported by model workers.
"""
import argparse
import ast
import json
import re
import sys
from pathlib import Path

IMAGE_PREFIX = "jefzda/sweap-images"

# pytest verbose emits  node::id PASSED [ 42%]  (name first, and parametrized
# ids may contain spaces).  The stock parse_log_pytest expects status-first
# and splits on whitespace, so we emit our own machine-readable lines:
#   __PRO__<STATUS>\t<full node id>
_PY_LINE = re.compile(
    r"^(.*?)\s+(PASSED|FAILED|XFAIL|XPASS|SKIPPED|ERROR)\s*(?:\[\s*\d+%\])?\s*$"
)
_PY_FILTER = (
    "import sys,re\n"
    "pat=re.compile(r'" + _PY_LINE.pattern + "')\n"
    "for line in sys.stdin:\n"
    "    line=line.rstrip('\\n')\n"
    "    m=pat.match(line)\n"
    "    if m:\n"
    "        sys.stdout.write('__PRO__%s\\t%s\\n'%(m.group(2),m.group(1).rstrip()))\n"
    "    sys.stdout.write(line+'\\n')\n"
)

_JS_RUNNER = r'''
import json, os, subprocess

pairs = json.load(open("/tmp/pro_tests.json"))

def find_pkg(app_abs):
    d = os.path.dirname(os.path.join("/app", app_abs))
    while d.startswith("/app"):
        if os.path.exists(os.path.join(d, "package.json")):
            return d
        nd = os.path.dirname(d)
        if nd == d:
            break
        d = nd
    stem = os.path.basename(app_abs).split(".")[0]
    for base in ("packages", "applications"):
        bdir = os.path.join("/app", base)
        if not os.path.isdir(bdir):
            continue
        for dirpath, dirs, files in os.walk(bdir):
            dirs[:] = [x for x in dirs if x != "node_modules"]
            for fn in files:
                if fn.endswith((".test.ts", ".test.tsx")) and fn.split(".")[0] == stem:
                    d = dirpath
                    while d.startswith("/app"):
                        if os.path.exists(os.path.join(d, "package.json")):
                            return d
                        d = os.path.dirname(d)
    return "/app"

seen = set()
for app_abs, pkg_rel in pairs:
    pkgdir = find_pkg(app_abs)
    key = (pkgdir, pkg_rel)
    if key in seen:
        continue
    seen.add(key)
    try:
        r = subprocess.run(
            ["npx", "--no-install", "jest", pkg_rel, "--json", "--runInBand"],
            cwd=pkgdir, capture_output=True, text=True, timeout=600,
        )
        data = json.loads(r.stdout)
    except Exception as e:
        print("[FAILED] %s | HARNESS_ERROR %s" % (pkg_rel, e))
        continue
    for t in data.get("testResults", []):
        test_file = os.path.relpath(t.get("name", ""), pkgdir) if t.get("name") else pkg_rel
        for a in t.get("assertionResults", []):
            st = "PASSED" if a.get("status") == "passed" else "FAILED"
            fn = a.get("fullName", "")
            print("[%s] %s | %s" % (st, test_file, fn))
            title = a.get("title")
            if title and title != fn:
                print("[%s] %s | %s" % (st, test_file, title))
'''


def parse_test_list(raw):
    if not raw:
        return []
    if isinstance(raw, list):
        return raw
    for fn in (json.loads, ast.literal_eval):
        try:
            return fn(raw)
        except Exception:
            pass
    return []


def extract_test_checkout(before_repo_set_cmd):
    lines = []
    for line in (before_repo_set_cmd or "").splitlines():
        s = line.strip()
        if not s:
            continue
        if s.startswith("git reset") or s.startswith("git clean"):
            continue
        if s.startswith("git checkout ") and "--" not in s:
            continue
        lines.append(s)
    return "\n".join(lines)


def _build_js_pairs(instance):
    """Flat list mixes repo-absolute paths (packages/|applications/) and
    package-relative jest patterns; pair by test-file stem (extension may
    differ, e.g. .ts vs .tsx)."""
    raw = parse_test_list(instance.get("selected_test_files_to_run"))
    abs_paths, rel_paths = [], []
    for tf in raw:
        if not isinstance(tf, str):
            continue
        (abs_paths if tf.startswith(("packages/", "applications/")) else rel_paths).append(tf)

    def stem(p):
        return p.rsplit("/", 1)[-1].split(".")[0]

    pairs, used = [], set()
    for ap in abs_paths:
        s = stem(ap)
        idx = next((i for i, rp in enumerate(rel_paths)
                    if i not in used and stem(rp) == s), None)
        if idx is not None:
            used.add(idx)
            pairs.append((ap, rel_paths[idx]))
        else:
            pairs.append((ap, ap.rsplit("/", 1)[-1]))
    for i, rp in enumerate(rel_paths):
        if i not in used:
            pairs.append((rp, rp))
    return pairs


def make_eval_script(instance):
    # A failed hidden-test checkout must abort evaluation, not silently run
    # the image's older tests. Test commands themselves may fail normally.
    test_setup = "set -e\n" + extract_test_checkout(instance.get("before_repo_set_cmd", "")) + "\nset +e"
    f2p = parse_test_list(instance.get("fail_to_pass"))
    p2p = parse_test_list(instance.get("pass_to_pass"))
    lang = (instance.get("repo_language") or "").lower()
    repo = instance.get("repo", "")

    if lang == "python":
        files, seen = [], set()
        for t in f2p + p2p:
            f = t.split("::")[0] if "::" in t else t
            if f and f not in seen:
                seen.add(f)
                files.append(f)
        env = "PYTHONPATH=/app/lib " if repo == "ansible/ansible" else ""
        qf = "'" + _PY_FILTER.replace("'", "'\"'\"'") + "'"
        test_cmd = f"{env}python -m pytest {' '.join(files)} -v 2>&1 | python3 -c {qf}"
        return (
            f"{test_setup}\n"
            'echo " >>>>> Start Test Output"\n'
            f"{test_cmd}\n"
            'echo " >>>>> End Test Output"\n'
        )

    if lang == "go":
        test_names, pkgs = [], set()
        for t in f2p + p2p:
            test_names.append(t)
        for line in (instance.get("before_repo_set_cmd") or "").splitlines():
            s = line.strip()
            if s.startswith("git checkout ") and "--" in s:
                for part in s.split("--", 1)[1].split():
                    if part.endswith("_test.go"):
                        pkgs.add(str(Path(part).parent))
        for tf in parse_test_list(instance.get("selected_test_files_to_run")):
            if isinstance(tf, str) and tf.endswith("_test.go"):
                pkgs.add(str(Path(tf).parent))
        if not pkgs:
            pkgs.add("./...")
        run_pattern = "|".join(re.escape(n) for n in test_names) if test_names else "."
        pkg_args = " ".join(
            f"./{p}" if not p.startswith("./") else p for p in sorted(pkgs)
        )
        return (
            f"{test_setup}\n"
            'echo " >>>>> Start Test Output"\n'
            f"go test -v -count=1 -run '{run_pattern}' {pkg_args}\n"
            'echo " >>>>> End Test Output"\n'
        )

    if lang in ("js", "javascript", "typescript", "ts"):
        pairs = _build_js_pairs(instance)
        lines = [test_setup, 'echo " >>>>> Start Test Output"']
        lines.append("cat > /tmp/pro_tests.json << 'PRO_JSON'")
        lines.append(json.dumps(pairs))
        lines.append("PRO_JSON")
        lines.append("python3 << 'PRO_PY'")
        lines.append(_JS_RUNNER.strip("\n"))
        lines.append("PRO_PY")
        lines.append('echo " >>>>> End Test Output"')
        return "\n".join(lines) + "\n"

    return f"{test_setup}\n"


def parse_log_pro_pytest(log, test_spec):
    """Read both machine lines and historical raw pytest output.

    Whitespace alignment is presentation, not part of a pytest node ID.
    Parameter IDs may themselves contain spaces, so never split on words.
    """
    status_map = {}
    for line in log.splitlines():
        if line.startswith("__PRO__"):
            status, _, name = line[len("__PRO__"):].partition("\t")
            if name:
                status_map[name.rstrip()] = status
        else:
            match = _PY_LINE.match(line)
            if match and ".py" in match.group(1):
                status_map[match.group(1).rstrip()] = match.group(2)
    return status_map


_GO_LINE = re.compile(r"^\s*--- (PASS|FAIL|SKIP): (\S+)\s")
_GO_STATUS = {"PASS": "PASSED", "FAIL": "FAILED", "SKIP": "SKIPPED"}


def parse_log_pro_gotest(log, test_spec):
    status_map = {}
    for line in log.split("\n"):
        m = _GO_LINE.match(line)
        if m:
            status, name = m.groups()
            status_map[name] = _GO_STATUS[status]
    return status_map


_JS_LINE = re.compile(r"^\[(PASSED|FAILED)\] (.*?) \| (.*)$")


def parse_log_pro_jest(log, test_spec):
    status_map = {}
    for line in log.split("\n"):
        m = _JS_LINE.match(line)
        if m:
            status, test_file, name = m.groups()
            status_map[name] = status
            status_map[f"{test_file} | {name}"] = status
            # Jest reports nested names as ``suite test`` while several Pro
            # FAIL_TO_PASS entries omit only the outer suite. Preserve that
            # intermediate form as well (e.g. ``useCanCheckItem get-started``
            # -> ``get-started``), rather than treating a passing test as
            # absent from the status map.
            _suite, separator, nested = name.partition(" ")
            if separator:
                status_map[nested] = status
                status_map[f"{test_file} | {nested}"] = status
    return status_map


# repo -> (log parser, language)
_PRO_REPOS = {
    "ansible/ansible": (parse_log_pro_pytest, "python"),
    "internetarchive/openlibrary": (parse_log_pro_pytest, "python"),
    "flipt-io/flipt": (parse_log_pro_gotest, "go"),
    "protonmail/webclients": (parse_log_pro_jest, "js"),
}


def register_pro_parser():
    """Register custom log parsers for SWE-bench Pro repos, which are not
    present in stock swebench's PARSER_REGISTRY."""
    from swebench.harness.grading import PARSER_REGISTRY

    for repo, (parser, _lang) in _PRO_REPOS.items():
        PARSER_REGISTRY[repo] = parser


def make_test_spec(instance):
    from swebench.types import TestSpec

    repo = instance.get("repo", "")
    ts = TestSpec(
        instance_id=instance["instance_id"],
        image=f"{IMAGE_PREFIX}:{instance['dockerhub_tag'][:128]}",
        repo=repo,
        version=instance.get("base_commit", ""),
        eval_script_list=[make_eval_script(instance)],
        FAIL_TO_PASS=parse_test_list(instance.get("fail_to_pass")),
        PASS_TO_PASS=parse_test_list(instance.get("pass_to_pass")),
        log_parser=repo,
        eval_type="pass_and_fail",
    )
    return ts
