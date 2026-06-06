"""
Tests for salt.modules.kernelpkg_linux_apt

Regression tests covering Debian/Ubuntu kernel-release formats whose
flavor-suffix parsing previously raised ``AttributeError``.
"""

import pytest

import salt.modules.kernelpkg_linux_apt as kernelpkg
from tests.support.mock import MagicMock, patch


@pytest.fixture
def configure_loader_modules():
    return {
        kernelpkg: {
            "__grains__": {"kernelrelease": "4.4.0-70-generic"},
            "__salt__": {
                "pkg.install": MagicMock(return_value={}),
                "pkg.latest_version": MagicMock(return_value=""),
                "pkg.list_pkgs": MagicMock(return_value={}),
                "pkg.purge": MagicMock(return_value=None),
                "system.reboot": MagicMock(return_value=None),
            },
        }
    }


@pytest.mark.parametrize(
    "kernelrelease,expected_type",
    [
        # Ubuntu (existing behavior)
        ("4.4.0-70-generic", "generic"),
        ("6.8.0-64-generic", "generic"),
        # Debian Bullseye (existing behavior, see issues #56219, #68204)
        ("6.1.140-1-amd64", "amd64"),
        # Debian Trixie (#69131 -- `+deb13` in the version broke the regex)
        ("6.12.86+deb13-amd64", "amd64"),
        # Debian Trixie cloud kernel -- flavor itself contains a hyphen
        ("6.12.86+deb13-cloud-amd64", "cloud-amd64"),
        # Pre-trixie Debian cloud kernel (existing behavior)
        ("4.19.0-6-cloud-amd64", "cloud-amd64"),
    ],
)
def test_kernel_type_parses_release(kernelrelease, expected_type):
    """
    Regression test for issue #69131.

    ``_kernel_type`` must extract the flavor suffix from the running
    kernel release across Ubuntu and Debian (including trixie's
    ``+debNN`` version suffix) without raising ``AttributeError``.
    """
    with patch.dict(kernelpkg.__grains__, {"kernelrelease": kernelrelease}):
        assert kernelpkg._kernel_type() == expected_type


def test_latest_available_falls_back_on_trixie_when_no_update():
    """
    Regression test for issue #69131.

    On Debian 13 (trixie) the ``kernelrelease`` grain looks like
    ``6.12.86+deb13-amd64``.  Prior to the fix, ``_kernel_type`` raised
    ``AttributeError: 'NoneType' object has no attribute 'group'`` which
    bubbled up through ``latest_available`` and ``upgrade``.
    """
    grains_patch = {"kernelrelease": "6.12.86+deb13-amd64"}
    # Empty -> latest_available falls back to latest_installed, exercising
    # _kernel_type without depending on the separate `latest_available`
    # regex (#68204).
    salt_patch = {
        "pkg.latest_version": MagicMock(return_value=""),
        "pkg.list_pkgs": MagicMock(
            return_value={"linux-image-6.12.86+deb13-amd64": "1"}
        ),
    }
    with patch.dict(kernelpkg.__grains__, grains_patch), patch.dict(
        kernelpkg.__salt__, salt_patch
    ):
        # Should not raise.
        kernelpkg.latest_available()
