"""A hand-rolled stand-in for `aiohttp.ClientSession`.

**Not aioresponses.** As of 0.7.9 it is incompatible with the aiohttp version
Home Assistant pins — `ClientResponse.__init__()` there gained a required
`stream_writer` kwarg that aioresponses does not pass, so every test using it
fails with a `TypeError` that has nothing to do with the code under test. The
client in `api.py` only needs `session.get(...)` and `session.request(...)`, so
faking those two directly is both smaller and more robust than fighting that
coupling.

One wrinkle worth knowing: `api.py` uses `session.get()` **both** ways — as an
async context manager for ordinary requests, and as a plain awaitable in
`async_open_object_stream` (which has to keep the response alive past the
`async with` block in order to stream it). Real aiohttp returns a
`_RequestContextManager` that supports both, so `FakeRequestContext` does too.
"""
from __future__ import annotations

import json as jsonlib
from collections.abc import AsyncIterator
from urllib.parse import parse_qsl, unquote, urlparse


class FakeContent:
    """Stands in for `resp.content`, the streaming body reader."""

    def __init__(
        self, body: bytes, *, fail_after: int | None = None, owner: object = None
    ) -> None:
        self._body = body
        self._fail_after = fail_after
        self._owner = owner

    async def iter_chunked(self, n: int) -> AsyncIterator[bytes]:
        if self._owner is not None and getattr(self._owner, "released", False):
            raise ResponseClosed(
                "iter_chunked() after the response was released — a stream "
                "handed out of an `async with` block is already dead."
            )
        emitted = 0
        for start in range(0, len(self._body), n):
            if self._fail_after is not None and emitted >= self._fail_after:
                raise TimeoutError("connection stalled mid-stream")
            chunk = self._body[start : start + n]
            emitted += 1
            yield chunk


class ResponseClosed(AssertionError):
    """Raised when a test reads a response real aiohttp would have released.

    Not a real aiohttp exception — a deliberately loud harness failure. Real
    aiohttp releases the connection when an `async with session.get(...)` block
    exits, so a body read after that point works in this fake but would not work
    in production. `api.py` relies on the distinction: ordinary calls use the
    context manager, while `async_open_object_stream` awaits `get()` bare
    precisely so the response outlives the call.
    """


class FakeResponse:
    """Stands in for `aiohttp.ClientResponse`."""

    def __init__(
        self,
        *,
        status: int = 200,
        body: bytes = b"",
        json: object = None,
        headers: dict[str, str] | None = None,
        stream_fail_after: int | None = None,
    ) -> None:
        self.status = status
        self.headers = headers or {}
        self._json = json
        self._body = jsonlib.dumps(json).encode() if json is not None else body
        self.released = False
        self.content = FakeContent(
            self._body, fail_after=stream_fail_after, owner=self
        )

    def _check_open(self, what: str) -> None:
        if self.released:
            raise ResponseClosed(
                f"{what} after the response was released. Real aiohttp releases "
                "on `async with` exit, so this would fail in production."
            )

    async def read(self) -> bytes:
        self._check_open("read()")
        return self._body

    async def text(self) -> str:
        self._check_open("text()")
        return self._body.decode("utf-8", "replace")

    async def json(self) -> object:
        self._check_open("json()")
        return jsonlib.loads(self._body)

    def release(self) -> None:
        self.released = True

    async def __aenter__(self) -> FakeResponse:
        return self

    async def __aexit__(self, *exc: object) -> bool:
        # Real aiohttp releases the connection here. Modelling that is what lets
        # a test catch code that opens a response inside `async with` and then
        # hands the body or the stream back to a caller.
        self.release()
        return False


class FakeRequestContext:
    """Awaitable *and* async-context-manager, like aiohttp's own return value."""

    def __init__(self, response: FakeResponse | BaseException) -> None:
        self._response = response
        self._entered: FakeResponse | None = None

    def _resolve(self) -> FakeResponse:
        if isinstance(self._response, BaseException):
            raise self._response
        return self._response

    def __await__(self):
        async def _inner() -> FakeResponse:
            return self._resolve()

        return _inner().__await__()

    async def __aenter__(self) -> FakeResponse:
        self._entered = self._resolve()
        return self._entered

    async def __aexit__(self, *exc: object) -> bool:
        # THIS is the object `async with session.get(...)` operates on — real
        # aiohttp returns a `_RequestContextManager`, and it is that context's
        # exit which releases the response, not the response's own. Putting the
        # release only on FakeResponse.__aexit__ looks equivalent and is not:
        # nothing ever enters the response as a context manager, so the release
        # never fired and the fake happily served a body from a closed
        # response.
        entered = getattr(self, "_entered", None)
        if entered is not None:
            entered.release()
        return False


class Call:
    """One recorded request."""

    def __init__(self, method: str, url: str, kwargs: dict) -> None:
        self.method = method
        self.url = url
        self.kwargs = kwargs
        parsed = urlparse(url)
        # Decoded, so routes are written with readable keys while the assertion
        # in TestSigning can still inspect the raw encoded `url`.
        self.path = unquote(parsed.path)
        # Query params arrive two ways: encoded into the URL (the S3 client
        # signs and builds its own query string) or as a `params=` kwarg (the
        # REST client lets aiohttp encode them). Merge both so assertions do
        # not have to care which.
        self.params = {**dict(parse_qsl(parsed.query, keep_blank_values=True))}
        self.params.update({k: str(v) for k, v in (kwargs.get("params") or {}).items()})

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return f"<Call {self.method} {self.path} {self.params}>"


class FakeSession:
    """Answers by (method, path) from a canned routing table.

    Each route holds a list of responses replayed in order, so pagination and
    retry paths can be expressed. A single-entry list is repeated forever,
    which keeps the common "same answer every time" case terse. A route may
    also be a callable taking the `Call` and returning a response, for the
    cases where the answer depends on a query parameter.
    """

    def __init__(self, routes: dict | None = None) -> None:
        self.routes = dict(routes or {})
        self.calls: list[Call] = []

    def _take(self, method: str, url: str, kwargs: dict) -> FakeRequestContext:
        call = Call(method, url, kwargs)
        self.calls.append(call)
        route = self.routes.get((method, call.path))
        if route is None:
            raise AssertionError(f"unexpected request: {method} {call.path} {call.params}")
        if isinstance(route, BaseException):
            # A bare exception as the route means "the transport itself failed
            # here" — what a timeout or a reset connection looks like.
            return FakeRequestContext(route)
        if callable(route):
            return FakeRequestContext(route(call))
        if len(route) == 1:
            return FakeRequestContext(route[0])
        return FakeRequestContext(route.pop(0))

    def get(self, url: str, **kwargs: object) -> FakeRequestContext:
        return self._take("GET", url, kwargs)

    def request(self, method: str, url: str, **kwargs: object) -> FakeRequestContext:
        return self._take(method, url, kwargs)

    def paths(self, method: str | None = None) -> list[str]:
        return [c.path for c in self.calls if method is None or c.method == method]
