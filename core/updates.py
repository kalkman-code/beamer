"""Whether a newer Beamer has been released, for both apps.

One HTTPS request to GitHub's public releases API when the app starts and once a day after, sending
nothing but the request itself, and only while the `check_updates` setting is on. A newer release
is shown quietly in the window and the menu; nothing downloads or installs, and nothing pops up.
"""

from __future__ import annotations

import json
import logging
import re
import sys
import threading

LATEST_URL = "https://api.github.com/repos/kalkman-code/beamer/releases/latest"
RELEASES_PAGE = "https://github.com/kalkman-code/beamer/releases/latest"
CHECK_EVERY_SECONDS = 24 * 60 * 60
TIMEOUT_SECONDS = 10.0
MAX_BYTES = 256 * 1024

LOGGER = logging.getLogger(__name__)
_VERSION = re.compile(r"^v?(\d+)\.(\d+)(?:\.(\d+))?(?:-beta\.(\d+))?$")


def _parse(text):
    """((major, minor, patch), is_beta) or None."""
    match = _VERSION.match(str(text or "").strip())
    if not match:
        return None
    return tuple(int(part or 0) for part in match.groups()[:3]), match.group(4) is not None


def parse_version(text):
    """(major, minor, patch) for "1.4", "1.4.0", "v1.4.0" or a beta such as "1.5.0-beta.1" (which
    reads as its release), else None: a local "dev" build or a tag in any other shape never reads as
    older or newer than anything."""
    parsed = _parse(text)
    return parsed[0] if parsed else None


# In a release's notes, where GitHub renders nothing, it offers a patch release as an update too.
ANNOUNCE = "<!-- announce -->"


def newer_release(current, payload):
    """(version text, page url) when `payload`, the API's JSON, names a release worth offering over
    `current`, else None. A new minor or major version always is; a patch release (1.4.0 to 1.4.1)
    only when its notes carry ANNOUNCE, so frequent small releases do not nag. Drafts and
    prereleases never count, nor does a tag that names a beta. A beta install is offered its own
    release (1.5.0-beta.1 to 1.5.0) and anything newer without the announce, never an older one."""
    if not isinstance(payload, dict) or payload.get("draft") or payload.get("prerelease"):
        return None
    tag = payload.get("tag_name")
    theirs, ours = _parse(tag), _parse(current)
    if theirs is None or ours is None or theirs[1]:
        return None
    theirs, (ours, beta) = theirs[0], ours
    if theirs < ours or (theirs == ours and not beta):
        return None
    body = payload.get("body")
    if not beta and theirs[:2] == ours[:2] and not (isinstance(body, str) and ANNOUNCE in body):
        return None
    url = payload.get("html_url")
    if not isinstance(url, str) or not url.startswith("https://github.com/kalkman-code/beamer/"):
        url = RELEASES_PAGE
    return (str(tag).lstrip("v"), url)


def fetch(url=LATEST_URL):
    """The response body as bytes. The Mac goes through Foundation, which trusts the system's
    certificates; a bundled Python has no certificate store of its own there."""
    if sys.platform == "darwin":
        import Foundation

        request = Foundation.NSMutableURLRequest.requestWithURL_cachePolicy_timeoutInterval_(
            Foundation.NSURL.URLWithString_(url), Foundation.NSURLRequestReloadIgnoringLocalCacheData, TIMEOUT_SECONDS)
        request.setValue_forHTTPHeaderField_("application/vnd.github+json", "Accept")
        data, response, error = Foundation.NSURLConnection.sendSynchronousRequest_returningResponse_error_(
            request, None, None)
        if data is None or error is not None or response is None or response.statusCode() != 200:
            raise OSError(f"update check failed: {error or (response.statusCode() if response else 'no response')}")
        return bytes(data)[:MAX_BYTES]
    import urllib.request

    request = urllib.request.Request(url, headers={"Accept": "application/vnd.github+json"})
    with urllib.request.urlopen(request, timeout=TIMEOUT_SECONDS) as response:
        return response.read(MAX_BYTES)


class Checker:
    """Checks now and every CHECK_EVERY_SECONDS on its own thread while `enabled()` says so, and
    calls `on_result((version, url) or None)` on that thread after each check that got an answer;
    the owner marshals it. A failed check keeps the last answer and logs once."""

    def __init__(self, current, enabled, on_result, fetcher=fetch, logger=None):
        self.current = current
        self.enabled = enabled
        self.on_result = on_result
        self.fetcher = fetcher
        self.logger = logger or LOGGER
        self.latest = None
        self._wake = threading.Event()
        self._stop = threading.Event()
        self._thread = None
        self._failed = False

    def start(self):
        if self._thread is None:
            self._thread = threading.Thread(target=self._run, name="Beamer-updates", daemon=True)
            self._thread.start()

    def check_now(self):
        """After the setting is switched on, rather than waiting a day."""
        self._wake.set()

    def stop(self):
        self._stop.set()
        self._wake.set()

    def check_once(self):
        if parse_version(self.current) is None or not self.enabled():
            return
        try:
            payload = json.loads(self.fetcher().decode("utf-8"))
        except Exception as exc:
            if not self._failed:
                self.logger.info("could not check for a newer Beamer: %s", exc)
            self._failed = True
            return
        self._failed = False
        self.latest = newer_release(self.current, payload)
        if self.latest:
            self.logger.info("Beamer %s is available", self.latest[0])
        try:
            self.on_result(self.latest)
        except Exception:
            self.logger.exception("update result callback failed")

    def _run(self):
        while not self._stop.is_set():
            self.check_once()
            self._wake.wait(CHECK_EVERY_SECONDS)
            self._wake.clear()
