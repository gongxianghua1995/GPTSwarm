"""Dependency-free execution evidence helpers; classifications are not verdicts."""
import re
import shlex

# Capture the real command status BEFORE printing bounded output. The command
# supplied to this wrapper should be the actual test, without head/tail pipes.
def capture_check(command):
    return (
        'swe_check_log=$(mktemp /tmp/swe-check.XXXXXXXX)\n'
        'bash -o pipefail -c ' + shlex.quote(command) + ' >"$swe_check_log" 2>&1\n'
        'swe_check_rc=$?\n'
        'printf "[check log: %s]\\n" "$swe_check_log"\n'
        'if [ "$(wc -c < "$swe_check_log")" -le 12000 ]; then cat "$swe_check_log"; '
        'else head -c 5000 "$swe_check_log"; printf "\\n[output truncated]\\n"; tail -c 7000 "$swe_check_log"; fi\n'
        'printf "\\n[check exit code: %s]\\n" "$swe_check_rc"\n'
        'exit "$swe_check_rc"'
    )


def check_evidence(command, result):
    """Recognize explicit test summaries, including errors masked by a pipeline."""
    output = result.get('output', '')
    rc = result.get('returncode', result.get('exit_code'))
    try:
        words = shlex.split(command)
    except ValueError:
        words = []
    test_like = any(w.rsplit('/', 1)[-1] in {'pytest', 'unittest', 'runtests.py', 'swe-check', 'jest'} for w in words)
    test_like |= any(words[i:i+2] in [['go', 'test'], ['npm', 'test'], ['yarn', 'test'], ['django', 'test']]
                     for i in range(len(words)))
    if not test_like and rc == 0:
        return None  # Reading source containing "AssertionError" is not a failed test.
    status = 'unknown'
    if re.search(r'no tests (?:to run|ran|found)|collected 0 items|Ran 0 tests|\[no test files\]', output, re.I):
        status = 'no_tests'
    elif re.search(r'unittest\.loader\._FailedTest|unrecognized arguments:|ERROR: file or directory not found|No module named (?:pytest|test_sqlite)', output):
        status = 'invocation_error'
    elif re.search(r'\b[1-9]\d* failed\b|AssertionError|^FAIL\b|^FAILED\b|unrecognized arguments:', output, re.M):
        status = 'failure_observed'
    elif rc == 124:
        status = 'timeout'
    elif test_like and rc == 0 and re.search(r'\b[1-9]\d* (?:assertions? )?passed\b|^ok\s|^PASS$|^OK(?: \(.*\))?$', output, re.M):
        status = 'tests_passed'
    elif test_like and rc not in (None, 0):
        status = 'command_failed'
    if not test_like and status == 'unknown':
        return None
    lines = [line for line in output.splitlines() if re.search(
        r'failed|passed|error|FAIL|PASS|no tests|no test files|^ok\s|^OK$|check exit', line, re.I)]
    return dict(status=status, returncode=rc, summary='\n'.join(lines[-12:])[-1600:],
                note='Output classification, not a correctness verdict. Relate failures to requirements and the recorded tree version.')


def clip(text, limit=1800):
    return text if len(text) <= limit else text[:limit//3]+'\n[truncated]\n'+text[-limit*2//3:]

CHECK_SCRIPT = '''#!/bin/bash
log=$(mktemp /tmp/swe-check.XXXXXXXX)
"$@" >"$log" 2>&1
rc=$?
python - "$rc" "$log" "$@" <<'SWARM_CHECK_JOURNAL'
import json, os, sys
with open(sys.argv[2], errors='replace') as source:
    output = source.read()
entry = dict(returncode=int(sys.argv[1]), argv=sys.argv[3:], output=output[-12000:])
with open(os.environ.get('SWARM_CHECK_JOURNAL', '/tmp/swarm-checks.jsonl'), 'a') as stream:
    stream.write(json.dumps(entry) + '\\n')
SWARM_CHECK_JOURNAL
printf "[check log: %s]\\n" "$log"
if [ "$(wc -c < "$log")" -le 12000 ]; then
    cat "$log"
else
    head -c 5000 "$log"
    printf "\\n[truncated]\\n"
    tail -c 7000 "$log"
fi
printf "\\n[check exit code: %s]\\n" "$rc"
exit "$rc"
'''
