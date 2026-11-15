"""One bounded completion test after the Nov 14 instructor review.

The first review used three fully reconciled transports. This invocation has
four remaining slots at USD0.025 each, preserving the combined seven-request,
USD0.175 reservation ceiling. The existing native browser journey owns all
acceptance and cleanup. No context, evaluator, deadline or retry changes.
"""
from live_notebook import LiveNotebook
import nhanes_desktop

_original_init = LiveNotebook.__init__


def bounded_init(self, *args, **kwargs):
    _original_init(self, *args, **kwargs)
    # Setup has not returned to the course/browser runner; no hint can yet be
    # submitted. Narrow the transport guard before any model admission.
    self.guard.request_limit = 4
    self.guard.budget = 0.100
    self.guard._append({'event':'studio_completion_budget', 'prior_complete_requests':3,
                        'remaining_requests':4, 'remaining_reserved_usd':0.100})


if __name__ == '__main__':
    LiveNotebook.__init__ = bounded_init
    raise SystemExit(nhanes_desktop.main())
