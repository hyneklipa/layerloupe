from collections.abc import Callable

import httpx
import pytest

from layerloupe.registry import (
    RegistryClient,
    RegistryHTTPError,
    parse_next_link,
)

# -- parse_next_link ------------------------------------------------------


def test_parse_next_link_relative_path() -> None:
    header = '</v2/_catalog?n=100&last=foo>; rel="next"'
    assert parse_next_link(header) == "/v2/_catalog?n=100&last=foo"


def test_parse_next_link_absolute_url() -> None:
    header = '<https://registry.example.com/v2/_catalog?n=100&last=foo>; rel="next"'
    assert parse_next_link(header) == "https://registry.example.com/v2/_catalog?n=100&last=foo"


def test_parse_next_link_unquoted_rel() -> None:
    header = "</v2/_catalog?n=100&last=bar>; rel=next"
    assert parse_next_link(header) == "/v2/_catalog?n=100&last=bar"


def test_parse_next_link_picks_next_among_multiple() -> None:
    header = '</prev>; rel="prev", </next>; rel="next", </first>; rel="first"'
    assert parse_next_link(header) == "/next"


def test_parse_next_link_empty_returns_none() -> None:
    assert parse_next_link(None) is None
    assert parse_next_link("") is None


def test_parse_next_link_no_next_returns_none() -> None:
    header = '</prev>; rel="prev"'
    assert parse_next_link(header) is None


# -- iter_repositories ----------------------------------------------------


def _build_paginated_handler(
    pages: list[dict[str, object]],
    *,
    base_path: str,
    items_key: str,
    use_link_header: bool = True,
) -> Callable[[httpx.Request], httpx.Response]:
    """Return a MockTransport handler that serves ``pages`` in order.

    Each page goes out with a ``Link: rel="next"`` header if there's another
    page to come (when ``use_link_header=True``); otherwise the test is in
    fallback-cursor mode and we omit the header.
    """
    state = {"index": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        page = pages[state["index"]]
        is_last = state["index"] == len(pages) - 1
        state["index"] += 1
        headers: dict[str, str] = {}
        if use_link_header and not is_last:
            next_cursor = page.get(items_key, [])
            assert isinstance(next_cursor, list)
            last_item = next_cursor[-1] if next_cursor else ""
            headers["link"] = f'<{base_path}?n=10&last={last_item}>; rel="next"'
        return httpx.Response(200, json=page, headers=headers)

    return handler


async def test_iter_repositories_single_page() -> None:
    handler = _build_paginated_handler(
        [{"repositories": ["foo", "bar", "baz"]}],
        base_path="/v2/_catalog",
        items_key="repositories",
    )
    async with RegistryClient(
        "https://registry.example.com",
        transport=httpx.MockTransport(handler),
    ) as client:
        results = [r async for r in client.iter_repositories()]
    assert results == ["foo", "bar", "baz"]


async def test_iter_repositories_three_pages_via_link_header() -> None:
    handler = _build_paginated_handler(
        [
            {"repositories": ["a", "b", "c"]},
            {"repositories": ["d", "e", "f"]},
            {"repositories": ["g", "h"]},
        ],
        base_path="/v2/_catalog",
        items_key="repositories",
    )
    async with RegistryClient(
        "https://registry.example.com",
        transport=httpx.MockTransport(handler),
    ) as client:
        results = [r async for r in client.iter_repositories(page_size=3)]
    assert results == ["a", "b", "c", "d", "e", "f", "g", "h"]


async def test_iter_repositories_three_pages_via_cursor_fallback() -> None:
    """Registry doesn't send Link - client falls back to ?last=<last item>."""
    state = {"calls": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        state["calls"] += 1
        params = dict(request.url.params)
        last = params.get("last")
        if last is None:
            return httpx.Response(200, json={"repositories": ["a", "b", "c"]})
        if last == "c":
            return httpx.Response(200, json={"repositories": ["d", "e", "f"]})
        if last == "f":
            # Short page - signals the end without Link header.
            return httpx.Response(200, json={"repositories": ["g"]})
        return httpx.Response(200, json={"repositories": []})

    async with RegistryClient(
        "https://registry.example.com",
        transport=httpx.MockTransport(handler),
    ) as client:
        results = [r async for r in client.iter_repositories(page_size=3)]

    assert results == ["a", "b", "c", "d", "e", "f", "g"]
    assert state["calls"] == 3


async def test_iter_repositories_empty_catalog() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"repositories": []})

    async with RegistryClient(
        "https://registry.example.com",
        transport=httpx.MockTransport(handler),
    ) as client:
        results = [r async for r in client.iter_repositories()]
    assert results == []


async def test_iter_repositories_filter_substring_case_insensitive() -> None:
    handler = _build_paginated_handler(
        [{"repositories": ["FooBar", "library/ubuntu", "alpine", "BUSYBOX", "node"]}],
        base_path="/v2/_catalog",
        items_key="repositories",
    )
    async with RegistryClient(
        "https://registry.example.com",
        transport=httpx.MockTransport(handler),
    ) as client:
        # Case-insensitive substring: "BU" matches "BUSYBOX" and "ubuntu".
        results = [r async for r in client.iter_repositories(query="BU")]
    assert sorted(results) == ["BUSYBOX", "library/ubuntu"]


async def test_iter_repositories_filter_empty_string_matches_all() -> None:
    handler = _build_paginated_handler(
        [{"repositories": ["a", "b", "c"]}],
        base_path="/v2/_catalog",
        items_key="repositories",
    )
    async with RegistryClient(
        "https://registry.example.com",
        transport=httpx.MockTransport(handler),
    ) as client:
        results = [r async for r in client.iter_repositories(query="")]
    assert results == ["a", "b", "c"]


async def test_iter_repositories_max_pages_caps_iteration() -> None:
    """Pathological registry: full pages forever, no Link header."""
    state = {"calls": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        state["calls"] += 1
        # Return distinct items each call so the cursor genuinely advances
        # - otherwise the stagnation guard short-circuits before max_pages.
        i = state["calls"]
        return httpx.Response(200, json={"repositories": [f"r{i}-a", f"r{i}-b", f"r{i}-c"]})

    async with RegistryClient(
        "https://registry.example.com",
        transport=httpx.MockTransport(handler),
    ) as client:
        results = [r async for r in client.iter_repositories(page_size=3, max_pages=5)]

    assert state["calls"] == 5
    # 5 pages * 3 items each = 15
    assert len(results) == 15


async def test_iter_repositories_stops_when_cursor_stagnates() -> None:
    """Registry ignores ?last= and replays the same page - must stop, not loop."""
    state = {"calls": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        state["calls"] += 1
        # Same page every time, regardless of ?last=. Without a stagnation
        # guard this loops until max_pages (None by default → forever).
        return httpx.Response(200, json={"repositories": ["a", "b", "c"]})

    async with RegistryClient(
        "https://registry.example.com",
        transport=httpx.MockTransport(handler),
    ) as client:
        results = [r async for r in client.iter_repositories(page_size=3)]

    # Page 1 (no cursor) yields a/b/c. Page 2 sent ?last=c, registry replayed
    # the same page, so the guard fires *before* yielding - no duplicates.
    assert state["calls"] == 2
    assert results == ["a", "b", "c"]


# -- iter_tags ------------------------------------------------------------


async def test_iter_tags_single_page() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/v2/library/ubuntu/tags/list"
        return httpx.Response(200, json={"name": "library/ubuntu", "tags": ["latest", "22.04"]})

    async with RegistryClient(
        "https://registry.example.com",
        transport=httpx.MockTransport(handler),
    ) as client:
        results = [t async for t in client.iter_tags("library/ubuntu")]
    assert results == ["latest", "22.04"]


async def test_iter_tags_three_pages_via_link_header() -> None:
    handler = _build_paginated_handler(
        [
            {"name": "foo", "tags": ["v1.0", "v1.1", "v1.2"]},
            {"name": "foo", "tags": ["v2.0", "v2.1", "v2.2"]},
            {"name": "foo", "tags": ["latest"]},
        ],
        base_path="/v2/foo/tags/list",
        items_key="tags",
    )
    async with RegistryClient(
        "https://registry.example.com",
        transport=httpx.MockTransport(handler),
    ) as client:
        results = [t async for t in client.iter_tags("foo", page_size=3)]
    assert results == ["v1.0", "v1.1", "v1.2", "v2.0", "v2.1", "v2.2", "latest"]


async def test_iter_tags_null_tags_field_treated_as_empty() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"name": "foo", "tags": None})

    async with RegistryClient(
        "https://registry.example.com",
        transport=httpx.MockTransport(handler),
    ) as client:
        results = [t async for t in client.iter_tags("foo")]
    assert results == []


async def test_iter_tags_filter() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={"name": "foo", "tags": ["latest", "v1.0", "v1.1", "v2.0", "edge"]},
        )

    async with RegistryClient(
        "https://registry.example.com",
        transport=httpx.MockTransport(handler),
    ) as client:
        results = [t async for t in client.iter_tags("foo", query="V1")]
    assert sorted(results) == ["v1.0", "v1.1"]


async def test_iter_tags_404_propagates_as_http_error() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(404, json={"errors": [{"code": "NAME_UNKNOWN"}]})

    async with RegistryClient(
        "https://registry.example.com",
        transport=httpx.MockTransport(handler),
    ) as client:
        with pytest.raises(RegistryHTTPError) as exc_info:
            _ = [t async for t in client.iter_tags("missing-repo")]
    assert exc_info.value.status_code == 404


# -- Defensive parsing ----------------------------------------------------


async def test_iter_repositories_rejects_non_dict_response() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=["not", "an", "object"])

    async with RegistryClient(
        "https://registry.example.com",
        transport=httpx.MockTransport(handler),
    ) as client:
        with pytest.raises(RegistryHTTPError, match="non-object"):
            _ = [r async for r in client.iter_repositories()]


async def test_iter_repositories_rejects_repositories_not_a_list() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"repositories": "should-be-list"})

    async with RegistryClient(
        "https://registry.example.com",
        transport=httpx.MockTransport(handler),
    ) as client:
        with pytest.raises(RegistryHTTPError, match="to be a list"):
            _ = [r async for r in client.iter_repositories()]


async def test_iter_repositories_skips_non_string_items() -> None:
    """Defensive: weird registry returns mixed types - yield only strings."""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"repositories": ["good", 42, None, "also-good"]})

    async with RegistryClient(
        "https://registry.example.com",
        transport=httpx.MockTransport(handler),
    ) as client:
        results = [r async for r in client.iter_repositories()]
    assert results == ["good", "also-good"]


# -- Page size honored ----------------------------------------------------


async def test_iter_repositories_sends_page_size_param() -> None:
    seen_params: list[dict[str, str]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen_params.append(dict(request.url.params))
        return httpx.Response(200, json={"repositories": []})

    async with RegistryClient(
        "https://registry.example.com",
        transport=httpx.MockTransport(handler),
    ) as client:
        _ = [r async for r in client.iter_repositories(page_size=42)]

    assert seen_params == [{"n": "42"}]
