"""A finite human review window; it grants no course or provider authority."""
import time


def validate(seconds, *, live_portage=None, env_file=None, execute_approved=False):
    if type(seconds) is not int or not 0 <= seconds <= 120:
        raise ValueError('operator review must be between 0 and 120 seconds')
    if seconds and (live_portage is not None or env_file is not None or execute_approved):
        raise ValueError('operator review supports only the local tutor stub, without provider credentials')


def wait_for_review(browser, page, seconds, *, monotonic=time.monotonic):
    validate(seconds)
    started = monotonic(); deadline = started + seconds
    reason = 'deadline'
    while monotonic() < deadline:
        if not browser.is_connected() or page.is_closed():
            reason = 'operator_closed'; break
        try:
            page.wait_for_timeout(min(250, max(0, deadline - monotonic()) * 1000))
        except Exception:
            if not browser.is_connected() or page.is_closed():
                reason = 'operator_closed'; break
            raise
    return {'requested_seconds': seconds, 'elapsed_seconds': round(monotonic() - started, 3),
            'ended_by': reason, 'human_acceptance_recorded': False}
