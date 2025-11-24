import pytest

from tests.support.helpers import system_python_version

pytestmark = [
    pytest.mark.skip_on_windows(reason="salt-ssh not available on Windows"),
    pytest.mark.slow_test,
    pytest.mark.skipif(
        system_python_version() < (3, 10),
        reason="System python too old for these tests",
    ),
]


def test_state_with_import(state_tree, salt_ssh_cli_parameterized):
    """
    verify salt-ssh can use imported map files in states
    """
    ret = salt_ssh_cli_parameterized.run("state.sls", "test")
    assert ret.returncode == 0
    assert ret.data


def test_state_with_import_from_dir(nested_state_tree, salt_ssh_cli_parameterized):
    """
    verify salt-ssh can use imported map files in states
    """
    ret = salt_ssh_cli_parameterized.run(
        "--extra-filerefs=salt://foo/map.jinja", "state.apply", "foo"
    )
    assert ret.returncode == 0
    assert ret.data


def test_state_low(salt_ssh_cli_parameterized):
    """
    test state.low with salt-ssh
    """
    ret = salt_ssh_cli_parameterized.run(
        "state.low", '{"state": "cmd", "fun": "run", "name": "echo blah"}'
    )
    assert ret.data["cmd_|-echo blah_|-echo blah_|-run"]["changes"]["stdout"] == "blah"


def test_state_high(salt_ssh_cli_parameterized):
    """
    test state.high with salt-ssh
    """
    ret = salt_ssh_cli_parameterized.run("state.high", '{"echo blah": {"cmd": ["run"]}}')
    assert ret.data["cmd_|-echo blah_|-echo blah_|-run"]["changes"]["stdout"] == "blah"


def test_state_test(state_tree, salt_ssh_cli_parameterized):
    ret = salt_ssh_cli_parameterized.run("state.test", "test")
    assert ret.returncode == 0
    assert ret.data
    assert (
        ret.data["test_|-Ok with def_|-Ok with def_|-succeed_with_changes"]["result"]
        is None
    )
