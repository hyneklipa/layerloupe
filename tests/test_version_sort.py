"""Tests for :func:`layerloupe.utils.sort_tags`."""

from __future__ import annotations

import pytest

from layerloupe.utils import sort_tags

# -- Acceptance test from the design doc ----------------------------------


def test_acceptance_example_from_design() -> None:
    """The exact ordering required by the smart-sort spec."""
    assert sort_tags(["1.0", "1.10", "1.2", "latest", "edge"]) == [
        "latest",
        "1.10",
        "1.2",
        "1.0",
        "edge",
    ]


# -- Bucket: latest -------------------------------------------------------


def test_latest_pinned_to_top() -> None:
    assert sort_tags(["edge", "1.0", "latest"])[0] == "latest"


def test_no_latest_means_no_latest_in_output() -> None:
    out = sort_tags(["1.0", "1.1"])
    assert "latest" not in out


def test_only_latest() -> None:
    assert sort_tags(["latest"]) == ["latest"]


# -- Bucket: versions -----------------------------------------------------


def test_numeric_components_compared_numerically_not_lexically() -> None:
    """``1.10`` must come before ``1.2`` — the whole point of semver-aware sorting."""
    assert sort_tags(["1.2", "1.10", "1.9"]) == ["1.10", "1.9", "1.2"]


def test_v_prefix_is_optional() -> None:
    assert sort_tags(["v1.0", "v2.0"]) == ["v2.0", "v1.0"]


def test_v_prefix_and_bare_versions_mix() -> None:
    """Mixed ``v1.0`` and ``2.0`` — both parse, descending order wins."""
    out = sort_tags(["v1.0", "2.0", "v3.0"])
    assert out == ["v3.0", "2.0", "v1.0"]


def test_release_outranks_prerelease() -> None:
    assert sort_tags(["1.0.0-rc1", "1.0.0"]) == ["1.0.0", "1.0.0-rc1"]


def test_higher_prerelease_outranks_lower() -> None:
    assert sort_tags(["1.0.0-rc1", "1.0.0-rc2"]) == ["1.0.0-rc2", "1.0.0-rc1"]


def test_more_components_outranks_fewer_for_same_prefix() -> None:
    """``1.0.0`` > ``1.0`` because tuple comparison: ``(1, 0, 0) > (1, 0)``."""
    assert sort_tags(["1.0", "1.0.0"]) == ["1.0.0", "1.0"]


def test_large_numeric_components() -> None:
    assert sort_tags(["1.10", "1.9", "1.100"]) == ["1.100", "1.10", "1.9"]


def test_plus_suffix_treated_as_prerelease_marker() -> None:
    """``1.0.0+build.1`` parses; release ``1.0.0`` still wins."""
    assert sort_tags(["1.0.0+build.1", "1.0.0"]) == ["1.0.0", "1.0.0+build.1"]


# -- Bucket: other --------------------------------------------------------


def test_non_versioned_tags_alphabetical_ascending() -> None:
    assert sort_tags(["slim", "bookworm", "alpine"]) == ["alpine", "bookworm", "slim"]


def test_codename_tags_below_versions() -> None:
    out = sort_tags(["1.0", "alpine", "2.0", "bookworm"])
    assert out == ["2.0", "1.0", "alpine", "bookworm"]


def test_alpha_tag_with_letters_is_non_versioned() -> None:
    """``1.0a1`` doesn't match our regex (no PEP 440 alpha shorthand)."""
    out = sort_tags(["1.0a1", "1.0", "2.0"])
    assert out == ["2.0", "1.0", "1.0a1"]


# -- Edge cases -----------------------------------------------------------


def test_empty_input() -> None:
    assert sort_tags([]) == []


def test_single_tag() -> None:
    assert sort_tags(["foo"]) == ["foo"]
    assert sort_tags(["1.0"]) == ["1.0"]


def test_no_mutation_of_input() -> None:
    """The caller's list shouldn't be reordered in place."""
    original = ["1.0", "2.0", "edge"]
    snapshot = list(original)
    sort_tags(original)
    assert original == snapshot


def test_full_realistic_mix() -> None:
    """Sanity: a realistic Docker registry tag list comes out sensibly."""
    tags = [
        "latest",
        "edge",
        "stable",
        "v1.0.0",
        "v1.0.0-rc1",
        "v1.0.0-rc2",
        "v0.9.0",
        "alpine",
        "bookworm",
        "1.0",
    ]
    out = sort_tags(tags)
    # latest pinned at top
    assert out[0] == "latest"
    # version bucket: 5 entries, descending
    assert out[1:6] == [
        "v1.0.0",
        "v1.0.0-rc2",
        "v1.0.0-rc1",
        "1.0",
        "v0.9.0",
    ]
    # tail: codenames alphabetical (alpine < bookworm < edge < stable)
    assert out[6:] == ["alpine", "bookworm", "edge", "stable"]


# -- _parse_version internal sanity --------------------------------------


@pytest.mark.parametrize(
    ("tag", "expected"),
    [
        ("1.0", ((1, 0), "")),
        ("1.2.3", ((1, 2, 3), "")),
        ("v1.2.3", ((1, 2, 3), "")),
        ("V2.0", ((2, 0), "")),
        ("1.0.0-rc1", ((1, 0, 0), "rc1")),
        ("1.0.0+build.1", ((1, 0, 0), "build.1")),
        ("latest", None),
        ("alpine", None),
        ("1.0a1", None),
        ("", None),
    ],
)
def test_parse_version_internals(tag: str, expected: tuple[tuple[int, ...], str] | None) -> None:
    from layerloupe.utils.version_sort import _parse_version

    assert _parse_version(tag) == expected
