"""Structured handoffs and conservative local approval checks (not eval verdicts)."""
import json


def parse_handoff(text):
    try:
        value = json.loads(text)
    except (ValueError, TypeError):
        return None
    if not isinstance(value, dict) or not isinstance(value.get('summary'), str):
        return None
    for name in ['facts', 'assumptions', 'acceptance_criteria', 'changes', 'evidence',
                 'open_questions', 'findings', 'check_dispositions']:
        if not isinstance(value.get(name), list):
            return None
    return value


def assess_review(report, exit_status, checks, final_tree, precommitted):
    """A model verdict cannot erase missing, stale or contradictory evidence."""
    errors = []
    if exit_status != 'Submitted':
        errors.append('Reviewer did not complete submission')
    if report is None:
        return 'unverified', errors + ['Missing or invalid structured handoff']
    requested = str(report.get('verdict', 'UNVERIFIED')).upper()
    if requested == 'REQUEST_CHANGES':
        return 'request_changes' if exit_status == 'Submitted' else 'unverified', errors
    if requested != 'APPROVE':
        return 'unverified', errors
    if not precommitted:
        errors.append('Missing initial or inherited acceptance checklist')
    criteria = report.get('acceptance_criteria', [])
    if isinstance(precommitted, list) and any(c not in criteria for c in precommitted):
        errors.append('Initial acceptance criteria were discarded after predecessor disclosure')
    if not criteria or not all(isinstance(c, str) and c.strip() for c in criteria):
        errors.append('Acceptance criteria are missing')
    if report.get('open_questions'):
        errors.append('Open questions remain')
    if any(not isinstance(f, dict) or f.get('severity') != 'nonblocking' for f in report.get('findings', [])):
        errors.append('Blocking or unclassified findings remain')
    by_id = {c['id']: c for c in checks if c.get('id')}

    def passed(check):
        return bool(final_tree and check and check.get('wrapped') and check.get('scope') == 'patched'
                    and check.get('returncode') == 0 and check.get('status') == 'tests_passed'
                    and check.get('tree_before') == check.get('tree_after') == final_tree)

    kinds, covered, behavior_ids, regression_ids = set(), set(), set(), set()
    for item in report.get('evidence', []):
        if not isinstance(item, dict) or not item.get('claim'):
            errors.append('Invalid evidence claim')
            continue
        check = by_id.get(item.get('check_id'))
        if not passed(check):
            if item.get('kind') != 'baseline':
                errors.append(f'Claim lacks a successful current-tree check: {item.get("check_id")}')
            continue
        kinds.add(item.get('kind'))
        if item.get('kind') == 'behavior':
            behavior_ids.add(check['id'])
        if item.get('kind') == 'regression':
            regression_ids.add(check['id'])
        if isinstance(item.get('criterion'), int):
            covered.add(item['criterion'])
    if not {'behavior', 'regression'} <= kinds:
        errors.append('Both independent behavior and regression evidence are required')
    elif not any(a != b for a in behavior_ids for b in regression_ids):
        errors.append('Behavior and regression evidence must be separate checks')
    if not set(range(len(criteria))) <= covered:
        errors.append('Some acceptance criteria have no successful check')
    dispositions = {d.get('check_id'): d for d in report.get('check_dispositions', []) if isinstance(d, dict)}
    for check in checks:
        if check.get('scope') == 'baseline' or check.get('status') not in {
                'failure_observed', 'command_failed', 'invocation_error', 'timeout', 'no_tests'}:
            continue
        d = dispositions.get(check.get('id'), {})
        replacement = by_id.get(d.get('replacement_check_id'))
        baseline = by_id.get(d.get('baseline_check_id'))
        corrected = check.get('status') in {'invocation_error', 'no_tests'} and passed(replacement)
        matched = bool(baseline and baseline.get('wrapped') and baseline.get('scope') == 'baseline'
                       and baseline.get('returncode') == check.get('returncode') != 0
                       and baseline.get('command') == check.get('command')
                       and baseline.get('summary') and baseline.get('summary') == check.get('summary'))
        if not d.get('reason') or not (corrected or matched):
            errors.append(f'Unresolved check: {check.get("id")} ({check.get("status")})')
    return ('unverified' if errors else 'approve'), errors
