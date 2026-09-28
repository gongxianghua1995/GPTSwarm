"""Phase budgeting and review continuity, independent of model/provider code."""


REPORT_GUIDANCE = (
    'Wrap-up period: stop broad exploration. Save the handoff now, preserving unresolved '
    'questions. You may complete a focused behavior/regression check needed for the verdict '
    'if it fits the remaining time; then update and submit the report. If verification is '
    'incomplete, submit UNVERIFIED. Do not spend the remaining time debating the schedule.'
)


def request_budget(remaining, api_timeout, report_seconds, soft_remaining=None):
    # Only the shared task deadline bounds a request. Role targets are advisory.
    available = max(0, remaining - 5)  # leave time for action/result recovery
    return dict(reporting=(remaining <= report_seconds or
                           (soft_remaining is not None and soft_remaining <= report_seconds)),
                timeout=min(api_timeout, available), can_request=available > 0)


def inherited_review_criteria(messages):
    """Only inherit the first reviewer's recorded criteria, never engineer claims."""
    for message in reversed(messages):
        if message.get('phase') == 'review' and message.get('role') == 'Reviewer':
            criteria = message.get('initial_acceptance', [])
            if criteria and all(isinstance(c, str) and c.strip() for c in criteria):
                return list(criteria)
    return []
