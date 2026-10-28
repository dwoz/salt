import os

import salt.defaults.exitcodes
import salt.utils.parsers
import salt.utils.profile
from salt.exceptions import SaltClientError
from salt.utils.verify import check_user


class SaltRun(salt.utils.parsers.SaltRunOptionParser):
    """
    Used to execute Salt runners
    """

    def run(self):
        """
        Execute salt-run
        """
        import salt.runner

        self.parse_args()

        profiling_enabled = self.options.profiling_enabled

        runner = salt.runner.Runner(self.config)
        if self.options.doc:
            runner.print_docs()
            self.exit(salt.defaults.exitcodes.EX_OK)

        # Run this here so SystemExit isn't raised anywhere else when
        # someone tries to use the runners via the python API
        try:
            # Intelligent auth detection:
            # 1. If client_auth explicitly set, use that
            # 2. If user check passes, allow (traditional local execution)
            # 3. If user check fails but client keys exist, enable client auth automatically
            client_auth_mode = self.config.get("client_auth")

            if client_auth_mode is None:
                # Auto-detect: check if user has permissions first
                if not check_user(self.config["user"]):
                    # User check failed, see if we have client keys
                    import salt.syspaths

                    pki_dir = self.config.get(
                        "pki_dir",
                        os.path.join(salt.syspaths.CONFIG_DIR, "pki", "master"),
                    )
                    client_key = os.path.join(pki_dir, "client.pem")
                    if os.path.exists(client_key):
                        client_auth_mode = True
                else:
                    client_auth_mode = False

            if client_auth_mode or check_user(self.config["user"]):
                pr = salt.utils.profile.activate_profile(profiling_enabled)
                try:
                    ret = runner.run(full_return=True)
                    # In older versions ret['data']['retcode'] was used
                    # for signaling the return code. This has been
                    # changed for the orchestrate runner, but external
                    # runners might still use it. For this reason, we
                    # also check ret['data']['retcode'] if
                    # ret['retcode'] is not available.
                    if (
                        isinstance(ret, dict)
                        and "return" in ret
                        and "retcode" not in ret
                    ):
                        ret = ret["return"]
                    if isinstance(ret, dict) and "retcode" in ret:
                        self.exit(ret["retcode"])
                    elif isinstance(ret, dict) and "retcode" in ret.get("data", {}):
                        self.exit(ret["data"]["retcode"])
                finally:
                    salt.utils.profile.output_profile(
                        pr, stats_path=self.options.profiling_path, stop=True
                    )

        except SaltClientError as exc:
            raise SystemExit(str(exc))
