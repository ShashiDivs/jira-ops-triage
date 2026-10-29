"""Protection for a publicly reachable demo: a shared passcode gate and a cost limiter.

The app has no user accounts — the "Approving as" box is only a claim. That is fine on a laptop and
unsafe on a public URL, where anyone could type `bob` and approve a security ticket, or submit tickets
all day at the owner's expense. Both guards are off unless configured (see config.py), so local
development is unchanged.
"""
import hmac
import threading
import time
from collections import deque
from collections.abc import Callable

from fastapi import HTTPException, Request
from starlette.responses import JSONResponse

PASSCODE_HEADER = "x-demo-passcode"
# Reachable without the passcode: the health probe, and the call the browser makes to learn whether to ask.
OPEN_PATHS = frozenset({"/api/health", "/api/gate"})


class PasscodeGate:
    """ASGI middleware: with a passcode configured, every /api request except OPEN_PATHS must send it.
    Static files stay public — they contain no data — so the page can load and show the passcode prompt."""

    def __init__(self, app, passcode: str):
        self.app = app
        self.passcode = passcode

    async def __call__(self, scope, receive, send):
        if (
            self.passcode
            and scope["type"] == "http"
            and scope["path"].startswith("/api")
            and scope["path"] not in OPEN_PATHS
            and scope["method"] != "OPTIONS"  # CORS preflights carry no custom headers
        ):
            supplied = dict(scope["headers"]).get(PASSCODE_HEADER.encode(), b"")
            if not hmac.compare_digest(supplied, self.passcode.encode()):
                await JSONResponse({"detail": "passcode required"}, status_code=401)(scope, receive, send)
                return
        await self.app(scope, receive, send)


class SubmissionLimiter:
    """Caps ticket submissions, which are what spends model calls: N per minute per client, and a total
    for the life of the process. The total is the real budget guard — a per-client limit can be dodged by
    changing address, a global one cannot. In-memory, so it assumes ONE server process (the demo runs one)."""

    def __init__(self, per_minute: int = 0, total_cap: int = 0, clock: Callable[[], float] = time.monotonic):
        self.per_minute, self.total_cap, self._clock = per_minute, total_cap, clock
        self._recent: dict[str, deque[float]] = {}
        self._accepted = 0
        self._lock = threading.Lock()

    def check(self, client: str) -> None:
        """Raises HTTP 429 if this submission is not allowed; otherwise counts it."""
        with self._lock:
            if self.total_cap and self._accepted >= self.total_cap:
                raise HTTPException(429, "This demo has reached its ticket limit.")
            now = self._clock()
            recent = self._recent.setdefault(client, deque())
            while recent and now - recent[0] >= 60:
                recent.popleft()
            if self.per_minute and len(recent) >= self.per_minute:
                raise HTTPException(429, f"Too many tickets — at most {self.per_minute} per minute. Try again shortly.")
            recent.append(now)
            self._accepted += 1


def client_address(request: Request) -> str:
    """The caller's address. Behind a proxy that appends to X-Forwarded-For, the RIGHTMOST entry is the
    one the proxy itself saw; earlier entries are whatever the client claimed, so they are not trusted."""
    forwarded = request.headers.get("x-forwarded-for", "")
    if forwarded:
        return forwarded.split(",")[-1].strip()
    return request.client.host if request.client else "unknown"
