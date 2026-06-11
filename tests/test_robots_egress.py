"""Egress filtering of the policy's own ``robots.txt`` fetch (network-free).

SSRF regression suite for ``_default_robots_loader``: the robots GET is server-side
``httpx``, not the browser, so the policy's ``request_filter`` cannot reach it as a browser
route — the loader must apply the filter itself, to the initial robots URL *and* to every
redirect hop, before each request is made. A public robots URL that 302-redirects to an
internal endpoint (e.g. the cloud metadata service) must never be fetched.

All tests drive the real loader through its private ``_transport`` seam
(``httpx.MockTransport``), recording every URL actually requested.
"""

from __future__ import annotations

import httpx

from colorsense.net.politeness import (
    _MAX_ROBOTS_BYTES,
    _MAX_ROBOTS_REDIRECTS,
    PolitenessPolicy,
    _default_robots_loader,
)

ROBOTS_URL = "https://public.test/robots.txt"
METADATA_URL = "http://169.254.169.254/latest/meta-data/"
ROBOTS_TEXT = "User-agent: *\nDisallow: /private\n"
UA = "colorsense-tests/1.0"


def _transport_recording(
    responses: dict[str, httpx.Response],
) -> tuple[httpx.MockTransport, list[str]]:
    """A MockTransport serving ``responses`` by URL, recording every requested URL."""
    requested: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requested.append(str(request.url))
        return responses[str(request.url)]

    return httpx.MockTransport(handler), requested


def _block_metadata(url: str) -> bool:
    return not url.startswith("http://169.254.")


async def test_redirect_to_blocked_url_is_not_followed() -> None:
    # The SSRF shape under test: a public robots URL 302-bounces to the metadata
    # endpoint. The blocked URL must never be requested, and the loader must return
    # None (no rules) rather than the redirect target's body.
    transport, requested = _transport_recording(
        {
            ROBOTS_URL: httpx.Response(302, headers={"location": METADATA_URL}),
            METADATA_URL: httpx.Response(200, text="SECRET"),
        }
    )

    text = await _default_robots_loader(ROBOTS_URL, UA, _block_metadata, _transport=transport)

    assert text is None
    assert requested == [ROBOTS_URL]  # the metadata endpoint was never touched


async def test_relative_redirect_location_is_resolved_then_filtered() -> None:
    # Location headers may be relative; the loader must resolve them against the current
    # URL before filtering, so a filter keyed on absolute URLs sees the real destination.
    transport, requested = _transport_recording(
        {
            ROBOTS_URL: httpx.Response(302, headers={"location": "/blocked/robots.txt"}),
            "https://public.test/blocked/robots.txt": httpx.Response(200, text=ROBOTS_TEXT),
        }
    )

    def block_blocked_path(url: str) -> bool:
        return "/blocked/" not in url

    text = await _default_robots_loader(ROBOTS_URL, UA, block_blocked_path, _transport=transport)

    assert text is None
    assert requested == [ROBOTS_URL]


async def test_filter_applies_to_initial_robots_url() -> None:
    # The very first request is vetted too: a robots URL the filter rejects is never
    # fetched at all.
    transport, requested = _transport_recording({ROBOTS_URL: httpx.Response(200, text=ROBOTS_TEXT)})

    text = await _default_robots_loader(ROBOTS_URL, UA, lambda _url: False, _transport=transport)

    assert text is None
    assert requested == []


async def test_async_filter_vets_hops_too() -> None:
    # The seam accepts async predicates (the shipped block_private_networks() is one): the
    # loader must await the verdict per hop, blocking the redirect to the metadata
    # endpoint exactly as a sync filter would.
    transport, requested = _transport_recording(
        {
            ROBOTS_URL: httpx.Response(302, headers={"location": METADATA_URL}),
            METADATA_URL: httpx.Response(200, text="SECRET"),
        }
    )

    async def async_block_metadata(url: str) -> bool:
        return not url.startswith("http://169.254.")

    text = await _default_robots_loader(ROBOTS_URL, UA, async_block_metadata, _transport=transport)

    assert text is None
    assert requested == [ROBOTS_URL]


async def test_async_filter_permits_the_fetch_when_it_allows() -> None:
    # Control: an async filter returning True lets the robots fetch proceed normally.
    transport, requested = _transport_recording({ROBOTS_URL: httpx.Response(200, text=ROBOTS_TEXT)})

    async def allow_all(_url: str) -> bool:
        return True

    text = await _default_robots_loader(ROBOTS_URL, UA, allow_all, _transport=transport)

    assert text == ROBOTS_TEXT
    assert requested == [ROBOTS_URL]


async def test_raising_async_filter_fails_closed() -> None:
    # An async predicate that raises during its await must block too: no request goes out.
    transport, requested = _transport_recording({ROBOTS_URL: httpx.Response(200, text=ROBOTS_TEXT)})

    async def broken(_url: str) -> bool:
        raise RuntimeError("async predicate boom")

    text = await _default_robots_loader(ROBOTS_URL, UA, broken, _transport=transport)

    assert text is None
    assert requested == []


async def test_no_filter_follows_redirects_unchanged() -> None:
    # Without a filter, behavior is the pre-existing one: redirects are followed (up to
    # the cap) and the terminal body is returned.
    hop = "https://public.test/elsewhere/robots.txt"
    transport, requested = _transport_recording(
        {
            ROBOTS_URL: httpx.Response(301, headers={"location": hop}),
            hop: httpx.Response(200, text=ROBOTS_TEXT),
        }
    )

    text = await _default_robots_loader(ROBOTS_URL, UA, None, _transport=transport)

    assert text == ROBOTS_TEXT
    assert requested == [ROBOTS_URL, hop]


async def test_raising_filter_fails_closed() -> None:
    # A buggy predicate must block, not permit: no request goes out, loader returns None.
    transport, requested = _transport_recording({ROBOTS_URL: httpx.Response(200, text=ROBOTS_TEXT)})

    def broken(_url: str) -> bool:
        raise RuntimeError("predicate boom")

    text = await _default_robots_loader(ROBOTS_URL, UA, broken, _transport=transport)

    assert text is None
    assert requested == []


async def test_redirect_cap_is_enforced() -> None:
    # An endless redirect chain terminates at the cap with None — never an unbounded loop.
    requested: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requested.append(str(request.url))
        n = int(request.url.path.rsplit("/", 1)[-1])
        return httpx.Response(302, headers={"location": f"https://public.test/hop/{n + 1}"})

    transport = httpx.MockTransport(handler)

    text = await _default_robots_loader("https://public.test/hop/0", UA, None, _transport=transport)

    assert text is None
    # The initial URL plus the redirect allowance — and not one request more.
    assert len(requested) == _MAX_ROBOTS_REDIRECTS + 1


async def test_redirect_chain_within_cap_succeeds() -> None:
    # Sanity check on the cap's off-by-one: exactly _MAX_ROBOTS_REDIRECTS hops still land.
    responses: dict[str, httpx.Response] = {}
    for n in range(_MAX_ROBOTS_REDIRECTS):
        responses[f"https://public.test/hop/{n}"] = httpx.Response(
            302, headers={"location": f"https://public.test/hop/{n + 1}"}
        )
    responses[f"https://public.test/hop/{_MAX_ROBOTS_REDIRECTS}"] = httpx.Response(
        200, text=ROBOTS_TEXT
    )
    transport, requested = _transport_recording(responses)

    text = await _default_robots_loader("https://public.test/hop/0", UA, None, _transport=transport)

    assert text == ROBOTS_TEXT
    assert len(requested) == _MAX_ROBOTS_REDIRECTS + 1


async def test_redirect_without_location_returns_none() -> None:
    # A redirect status with no Location header is malformed: fail to "no rules".
    transport, requested = _transport_recording({ROBOTS_URL: httpx.Response(302)})

    text = await _default_robots_loader(ROBOTS_URL, UA, None, _transport=transport)

    assert text is None
    assert requested == [ROBOTS_URL]


async def test_invalid_url_from_httpx_fails_open() -> None:
    # ``httpx.InvalidURL`` subclasses neither HTTPError nor ValueError. The current httpx
    # parses leniently, so it is not reachable via a crafted Location today — the
    # transport seam raises it directly to pin that a stricter future httpx still fails
    # open as "no rules" instead of propagating out of the loader.
    def handler(_request: httpx.Request) -> httpx.Response:
        raise httpx.InvalidURL("simulated URL parse failure")

    text = await _default_robots_loader(
        ROBOTS_URL, UA, None, _transport=httpx.MockTransport(handler)
    )

    assert text is None


class _ChunkStream(httpx.AsyncByteStream):
    """A response body served as raw chunks, with no Content-Length header."""

    def __init__(self, chunks: list[bytes]) -> None:
        self._chunks = chunks

    async def __aiter__(self):
        for chunk in self._chunks:
            yield chunk


async def test_declared_oversized_body_is_rejected_before_reading() -> None:
    # A Content-Length over the cap aborts the fetch from the headers alone: no body
    # bytes are consumed, and the loader fails open as "no rules".
    consumed: list[bytes] = []

    class _RecordingStream(_ChunkStream):
        async def __aiter__(self):
            async for chunk in super().__aiter__():
                consumed.append(chunk)
                yield chunk

    transport, requested = _transport_recording(
        {
            ROBOTS_URL: httpx.Response(
                200,
                headers={"content-length": str(_MAX_ROBOTS_BYTES + 1)},
                stream=_RecordingStream([b"x" * 1024]),
            )
        }
    )

    text = await _default_robots_loader(ROBOTS_URL, UA, None, _transport=transport)

    assert text is None
    assert requested == [ROBOTS_URL]
    assert consumed == []  # rejected on the header, before any body chunk was read


async def test_streamed_oversized_body_is_rejected_at_the_cap() -> None:
    # No Content-Length (chunked/streaming server): the loader must stop accumulating
    # the moment the body exceeds the cap, not materialize an unbounded stream.
    chunk = b"x" * 64 * 1024
    transport, _ = _transport_recording(
        {
            ROBOTS_URL: httpx.Response(
                200, stream=_ChunkStream([chunk] * (_MAX_ROBOTS_BYTES // len(chunk) + 2))
            )
        }
    )

    text = await _default_robots_loader(ROBOTS_URL, UA, None, _transport=transport)

    assert text is None


async def test_body_at_exactly_the_cap_is_accepted() -> None:
    # Off-by-one guard: a body of exactly _MAX_ROBOTS_BYTES is within the limit.
    body = b"#" * _MAX_ROBOTS_BYTES
    transport, _ = _transport_recording({ROBOTS_URL: httpx.Response(200, content=body)})

    text = await _default_robots_loader(ROBOTS_URL, UA, None, _transport=transport)

    assert text == body.decode()


async def test_non_redirect_failures_still_return_none() -> None:
    # Pre-existing failure semantics are preserved: HTTP errors => None (fails open as
    # "no rules"; the navigation itself remains gated browser-side).
    transport, _ = _transport_recording({ROBOTS_URL: httpx.Response(500)})

    text = await _default_robots_loader(ROBOTS_URL, UA, None, _transport=transport)

    assert text is None


async def test_policy_passes_its_request_filter_to_the_robots_loader() -> None:
    # End-to-end through the policy seam: _robots_parser must hand the policy's own
    # request_filter to the loader (the gap under test — the filter previously only ever
    # reached the browser-side harvester).
    seen: list[object] = []

    async def recording_loader(
        _url: str, _user_agent: str, request_filter: object = None
    ) -> str | None:
        seen.append(request_filter)
        return None

    def some_filter(_url: str) -> bool:
        return True

    policy = PolitenessPolicy(request_filter=some_filter, robots_loader=recording_loader)
    assert await policy.can_fetch("https://public.test/page") is True
    assert seen == [some_filter]
