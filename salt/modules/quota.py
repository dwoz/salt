"""
Module for managing quotas on POSIX-like systems.
"""

import logging

import salt.utils.path
import salt.utils.platform
from salt.exceptions import CommandExecutionError, SaltInvocationError

log = logging.getLogger(__name__)


# Define a function alias in order not to shadow built-in's
__func_alias__ = {"set_": "set"}


def __virtual__():
    """
    Only work on POSIX-like systems with setquota binary available
    """
    if not salt.utils.platform.is_windows() and salt.utils.path.which("setquota"):
        return "quota"
    return (
        False,
        "The quota execution module cannot be loaded: the module is only "
        "available on POSIX-like systems with the setquota binary available.",
    )


def report(mount):
    """
    Report on quotas for a specific volume

    CLI Example:

    .. code-block:: bash

        salt '*' quota.report /media/data
    """
    ret = {mount: {}}
    ret[mount]["User Quotas"] = _parse_quota(mount, "-u")
    ret[mount]["Group Quotas"] = _parse_quota(mount, "-g")
    return ret


def _parse_quota(mount, opts):
    """
    Parse the output from repquota. Requires that -u -g are passed in
    """
    cmd = f"repquota -vp {opts} {mount}"
    out = __salt__["cmd.run"](cmd, python_shell=False).splitlines()
    mode = "header"

    if "-u" in opts:
        quotatype = "Users"
    elif "-g" in opts:
        quotatype = "Groups"
    ret = {quotatype: {}}

    for line in out:
        if not line:
            continue
        comps = line.split()
        if mode == "header":
            if "Block grace time" in line:
                blockg, inodeg = line.split(";")
                blockgc = blockg.split(": ")
                inodegc = inodeg.split(": ")
                ret["Block Grace Time"] = blockgc[-1:]
                ret["Inode Grace Time"] = inodegc[-1:]
            elif line.startswith("-"):
                mode = "quotas"
        elif mode == "quotas":
            if len(comps) < 8:
                continue
            if not comps[0] in ret[quotatype]:
                ret[quotatype][comps[0]] = {}
            ret[quotatype][comps[0]]["block-used"] = comps[2]
            ret[quotatype][comps[0]]["block-soft-limit"] = comps[3]
            ret[quotatype][comps[0]]["block-hard-limit"] = comps[4]
            ret[quotatype][comps[0]]["block-grace"] = comps[5]
            ret[quotatype][comps[0]]["file-used"] = comps[6]
            ret[quotatype][comps[0]]["file-soft-limit"] = comps[7]
            ret[quotatype][comps[0]]["file-hard-limit"] = comps[8]
            ret[quotatype][comps[0]]["file-grace"] = comps[9]
    return ret


def set_(device, **kwargs):
    """
    Calls out to setquota, for a specific user or group

    CLI Example:

    .. code-block:: bash

        salt '*' quota.set /media/data user=larry block-soft-limit=1048576
        salt '*' quota.set /media/data group=painters file-hard-limit=1000
    """
    empty = {
        "block-soft-limit": 0,
        "block-hard-limit": 0,
        "file-soft-limit": 0,
        "file-hard-limit": 0,
    }

    current = ret = None
    cmd = "setquota"
    if "user" in kwargs:
        cmd += " -u {} ".format(kwargs["user"])
        parsed = _parse_quota(device, "-u")
        if kwargs["user"] in parsed:
            current = parsed["Users"][kwargs["user"]]
        else:
            current = empty
        ret = "User: {}".format(kwargs["user"])

    if "group" in kwargs:
        if "user" in kwargs:
            raise SaltInvocationError("Please specify a user or group, not both.")
        cmd += " -g {} ".format(kwargs["group"])
        parsed = _parse_quota(device, "-g")
        if kwargs["group"] in parsed:
            current = parsed["Groups"][kwargs["group"]]
        else:
            current = empty
        ret = "Group: {}".format(kwargs["group"])

    if not current:
        raise CommandExecutionError("A valid user or group was not found")

    for limit in (
        "block-soft-limit",
        "block-hard-limit",
        "file-soft-limit",
        "file-hard-limit",
    ):
        if limit in kwargs:
            current[limit] = kwargs[limit]

    cmd += "{} {} {} {} {}".format(
        current["block-soft-limit"],
        current["block-hard-limit"],
        current["file-soft-limit"],
        current["file-hard-limit"],
        device,
    )

    result = __salt__["cmd.run_all"](cmd, python_shell=False)
    if result["retcode"] != 0:
        raise CommandExecutionError(
            "Unable to set desired quota. Error follows: \n{}".format(result["stderr"])
        )
    return {ret: current}


def warn():
    """
    Runs the warnquota command, to send warning emails to users who
    are over their quota limit.

    CLI Example:

    .. code-block:: bash

        salt '*' quota.warn
    """
    __salt__["cmd.run"]("quotawarn")


def stats():
    """
    Runs the quotastats command, and returns the parsed output

    CLI Example:

    .. code-block:: bash

        salt '*' quota.stats
    """
    ret = {}
    out = __salt__["cmd.run"]("quotastats").splitlines()
    for line in out:
        if not line:
            continue
        comps = line.split(": ")
        ret[comps[0]] = comps[1]

    return ret


def on(device):
    """
    Turns on the quota system

    CLI Example:

    .. code-block:: bash

        salt '*' quota.on
    """
    cmd = f"quotaon {device}"
    __salt__["cmd.run"](cmd, python_shell=False)
    return True


def off(device):
    """
    Turns off the quota system

    CLI Example:

    .. code-block:: bash

        salt '*' quota.off
    """
    cmd = f"quotaoff {device}"
    __salt__["cmd.run"](cmd, python_shell=False)
    return True


def get_mode(device):
    """
    Report whether the quota system for this device is on or off

    CLI Example:

    .. code-block:: bash

        salt '*' quota.get_mode
    """
    ret = {}
    cmd = f"quotaon -p {device}"
    out = __salt__["cmd.run"](cmd, python_shell=False)
    for line in out.splitlines():
        comps = line.strip().split()
        if comps[3] not in ret:
            if comps[0].startswith("quotaon"):
                if comps[1].startswith("Mountpoint"):
                    ret[comps[4]] = "disabled"
                    continue
                elif comps[1].startswith("Cannot"):
                    ret[device] = "Not found"
                    return ret
                continue
            ret[comps[3]] = {
                "device": comps[4].replace("(", "").replace(")", ""),
            }
        ret[comps[3]][comps[0]] = comps[6]
    return ret
