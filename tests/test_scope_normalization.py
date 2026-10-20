import pytest

from agentcoord.core import CoordinationError, normalize_paths


def test_folder_separator_is_advisory_scope_syntax():
    assert normalize_paths(["src/", "docs/plans/", "src/"]) == ("docs/plans", "src")


@pytest.mark.parametrize("path", ["/", "/src/", "../src/", "a//b/", ".git/", "a/../b/"])
def test_folder_normalization_retains_security_checks(path):
    with pytest.raises(CoordinationError):
        normalize_paths([path], allow_root=True)
