import os
import sys
import tempfile

import pytest
from salt.cli.api import SaltAPI
from tests.support.mock import patch
from tests.support.unit import TestCase


class SaltAPITestCase(TestCase):
    @pytest.mark.slow_test
    def test_start_shutdown(self):
        fp, name = tempfile.mkstemp()
        api = SaltAPI()
        try:
            # testing environment will fail if we use default pidfile
            # overwrite sys.argv so salt-api does not use testing args
            with patch.object(
                sys,
                "argv",
                [
                    sys.argv[0],
                    "--pid-file",
                    "salt-api-test.pid",
                    "--log-file",
                    "api.log",
                ],
            ):
                api.start()
                self.assertTrue(os.path.isfile("salt-api-test.pid"))
                os.remove("salt-api-test.pid")
                self.assertTrue(os.path.isfile("api.log"))
                os.remove("api.log")
        finally:
            self.assertRaises(SystemExit, api.shutdown)
