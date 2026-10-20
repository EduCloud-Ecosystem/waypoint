"""Fixed public requirements observations; retrieval never grants readiness."""
from datetime import datetime, timezone
import hashlib
import json
import subprocess
import sys
import urllib.error
import urllib.request

from . import assessment as a
from .bounded_process import collect

ACCESS_PROFILES = ("access_explore", "access_discover", "access_accelerate")
URLS = {
    "access_explore": "https://allocations.access-ci.org/prepare-requests",
    "access_accelerate": "https://allocations.access-ci.org/prepare-requests",
    "access_discover": "https://allocations.access-ci.org/prepare-requests",
    "nairr_research_draft": "https://nairrpilot.org/nairr-pilot-proposal-instructions",
    "nairr_classroom_draft": "https://nairrpilot.org/opportunities/education-call",
}
LIMIT = 524288


def observation(profile, *, refresh=False):
    if profile not in URLS:
        a.reject()
    result = {"url": URLS[profile], "format_review_date": "2026-10-19" if profile in ACCESS_PROFILES else None,
              "status": "snapshot_only" if profile in ACCESS_PROFILES else "robots_blocked_not_verified",
              "retrieved_at": None, "content_sha256": None, "current_requirements_ready": False}
    if not refresh or profile not in ACCESS_PROFILES:
        return result
    # Only the fixed public page is requested; no assessment data crosses this boundary.
    try:
        process = subprocess.Popen([sys.executable, "-m", "research_workspace.allocation_sources"],
                                   stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        out, _ = collect(process, timeout=12)
        data = a.strict_document(out)
        a.fields(data, ("status", "retrieved_at", "content_sha256"))
        a.choice(data["status"], ("retrieved_unreviewed", "unavailable"))
        a.timestamp(data["retrieved_at"])
        if data["status"] == "retrieved_unreviewed":
            if type(data["content_sha256"]) is not str or len(data["content_sha256"]) != 64 or any(c not in "0123456789abcdef" for c in data["content_sha256"]):
                a.reject()
        elif data["content_sha256"] is not None:
            a.reject()
        if process.returncode != 0:
            a.reject()
        result.update(data)
    except (OSError, ValueError, TimeoutError):
        result.update(status="unavailable", retrieved_at=None, content_sha256=None)
    return result


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def main():
    result = {"status": "unavailable", "retrieved_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"), "content_sha256": None}
    try:
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), NoRedirect())
        request = urllib.request.Request(URLS["access_discover"], headers={"User-Agent": "Waypoint-requirements-review/1"})
        with opener.open(request, timeout=5) as response:
            body = response.read(LIMIT + 1)
            if response.status == 200 and 0 < len(body) <= LIMIT and response.headers.get_content_type() == "text/html":
                result.update(status="retrieved_unreviewed", content_sha256=hashlib.sha256(body).hexdigest())
    except (OSError, ValueError, urllib.error.URLError):
        pass
    print(json.dumps(result, sort_keys=True))


if __name__ == "__main__":
    main()
