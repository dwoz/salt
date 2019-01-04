# -*- coding: utf-8 -*-

# Import Python libs
from __future__ import absolute_import, unicode_literals
import io
import inspect
import logging
import os
import subprocess
import socket
import sys
import textwrap
import threading
import time
import traceback
import yaml

# Import Salt Testing libs
from tests.support.case import ModuleCase
from tests.support.helpers import with_system_user
from tests.support.mock import Mock
from tests.support.paths import CODE_DIR
from tests.support.unit import skipIf

# Import Salt libs
from salt.ext import six
import salt.utils.files
import salt.utils.win_runas
import salt.utils.win_service

try:
    import win32service
    import win32serviceutil
    import win32event
    import servicemanager
    import win32api
    CODE_DIR = win32api.GetLongPathName(CODE_DIR)
    HAS_WIN32 = True
except ImportError:
    # Mock win32serviceutil object to avoid
    # a stacktrace in the _ServiceManager class
    win32serviceutil = Mock()
    HAS_WIN32 = False

logger = logging.getLogger(__name__)

PASSWORD = 'P@ssW0rd'
NOPRIV_STDERR = 'ERROR: Logged-on user does not have administrative privilege.\n'
PRIV_STDOUT = (
    '\nINFO: The system global flag \'maintain objects list\' needs\n      '
    'to be enabled to see local opened files.\n      See Openfiles '
    '/? for more information.\n\n\nFiles opened remotely via local share '
    'points:\n---------------------------------------------\n\n'
    'INFO: No shared open files found.\n'
)
RUNAS_PATH = os.path.abspath(os.path.join(CODE_DIR, 'runas.py'))
RUNAS_OUT = os.path.abspath(os.path.join(CODE_DIR, 'runas.out'))

if HAS_WIN32:
    test_service = salt.utils.win_service.service_class_factory('test_service', 'test service')

SERVICE_SOURCE = '''
from __future__ import absolute_import, unicode_literals
import logging
logger = logging.getLogger()
logging.basicConfig(level=logging.DEBUG, format="%(message)s")

from salt.utils.win_service import service_class_factory
import salt.utils.win_runas
import sys
import yaml

OUTPUT = {}
USERNAME = '{}'
PASSWORD = '{}'


def target(service, *args, **kwargs):
    service.log_info("target start")
    if PASSWORD:
        ret = salt.utils.win_runas.runas(
            'cmd.exe /C OPENFILES',
            username=USERNAME,
            password=PASSWORD,
        )
    else:
        ret = salt.utils.win_runas.runas(
            'cmd.exe /C OPENFILES',
            username=USERNAME,
        )

    service.log_info("win_runas returned %s" % ret)
    with open(OUTPUT, 'w') as fp:
        yaml.dump(ret, fp)
    service.log_info("target stop")


# This class will get imported and run as the service
test_service = service_class_factory('test_service', 'test service', target=target)

if __name__ == '__main__':
    try:
        test_service.stop()
    except Exception as exc:
        logger.debug("stop service failed, this is ok.")
    try:
        test_service.remove()
    except Exception as exc:
        logger.debug("remove service failed, this os ok.")
    test_service.install()
    sys.exit(0)
'''


def wait_for_service(name, timeout=200):
    start = time.time()
    while True:
        status = win32serviceutil.QueryServiceStatus(name)
        if status[1] == win32service.SERVICE_STOPPED:
            break
        if time.time() - start > timeout:
            raise TimeoutError("Timeout waiting for service")  # pylint: disable=undefined-variable

        time.sleep(.3)


@skipIf(not HAS_WIN32, 'This test runs only on windows.')
class RunAsTest(ModuleCase):

    @classmethod
    def setUpClass(cls):
        super(RunAsTest, cls).setUpClass()
        cls.hostname = socket.gethostname()

    @with_system_user('test-runas', on_existing='delete', delete=True,
                      password=PASSWORD)
    def test_runas(self, username):
        ret = salt.utils.win_runas.runas('cmd.exe /C OPENFILES', username, PASSWORD)
        self.assertEqual(ret['stdout'], '')
        self.assertEqual(ret['stderr'], NOPRIV_STDERR)
        self.assertEqual(ret['retcode'], 1)

    @with_system_user('test-runas', on_existing='delete', delete=True,
                      password=PASSWORD)
    def test_runas_no_pass(self, username):
        ret = salt.utils.win_runas.runas('cmd.exe /C OPENFILES', username)
        self.assertEqual(ret['stdout'], '')
        self.assertEqual(ret['stderr'], NOPRIV_STDERR)
        self.assertEqual(ret['retcode'], 1)

    @with_system_user('test-runas-admin', on_existing='delete', delete=True,
                      password=PASSWORD, groups=['Administrators'])
    def test_runas_admin(self, username):
        ret = salt.utils.win_runas.runas('cmd.exe /C OPENFILES', username, PASSWORD)
        self.assertEqual(ret['stdout'], PRIV_STDOUT)
        self.assertEqual(ret['stderr'], '')
        self.assertEqual(ret['retcode'], 0)

    @with_system_user('test-runas-admin', on_existing='delete', delete=True,
                      password=PASSWORD, groups=['Administrators'])
    def test_runas_admin_no_pass(self, username):
        ret = salt.utils.win_runas.runas('cmd.exe /C OPENFILES', username)
        self.assertEqual(ret['stdout'], PRIV_STDOUT)
        self.assertEqual(ret['stderr'], '')
        self.assertEqual(ret['retcode'], 0)

    def test_runas_system_user(self):
        ret = salt.utils.win_runas.runas('cmd.exe /C OPENFILES', 'SYSTEM')
        self.assertEqual(ret['stdout'], PRIV_STDOUT)
        self.assertEqual(ret['stderr'], '')
        self.assertEqual(ret['retcode'], 0)

    def test_runas_network_service(self):
        ret = salt.utils.win_runas.runas('cmd.exe /C OPENFILES', 'NETWORK SERVICE')
        self.assertEqual(ret['stdout'], '')
        self.assertEqual(ret['stderr'], NOPRIV_STDERR)
        self.assertEqual(ret['retcode'], 1)

    def test_runas_local_service(self):
        ret = salt.utils.win_runas.runas('cmd.exe /C OPENFILES', 'LOCAL SERVICE')
        self.assertEqual(ret['stdout'], '')
        self.assertEqual(ret['stderr'], NOPRIV_STDERR)
        self.assertEqual(ret['retcode'], 1)

    @with_system_user('test-runas', on_existing='delete', delete=True,
                      password=PASSWORD)
    def test_runas_winrs(self, username):
        runaspy = textwrap.dedent('''
        import sys
        import salt.utils.win_runas
        username = '{}'
        password = '{}'
        sys.exit(salt.utils.win_runas.runas('cmd.exe /C OPENFILES', username, password)['retcode'])
        '''.format(username, PASSWORD))
        with salt.utils.files.fopen(RUNAS_PATH, 'w') as fp:
            fp.write(runaspy)
        ret = subprocess.call("cmd.exe /C winrs /r:{} python {}".format(
            self.hostname, RUNAS_PATH), shell=True)
        self.assertEqual(ret, 1)

    @with_system_user('test-runas', on_existing='delete', delete=True,
                      password=PASSWORD)
    def test_runas_winrs_no_pass(self, username):
        runaspy = textwrap.dedent('''
        import sys
        import salt.utils.win_runas
        username = '{}'
        sys.exit(salt.utils.win_runas.runas('cmd.exe /C OPENFILES', username)['retcode'])
        '''.format(username))
        with salt.utils.files.fopen(RUNAS_PATH, 'w') as fp:
            fp.write(runaspy)
        ret = subprocess.call("cmd.exe /C winrs /r:{} python {}".format(
            self.hostname, RUNAS_PATH), shell=True)
        self.assertEqual(ret, 1)

    @with_system_user('test-runas-admin', on_existing='delete', delete=True,
                      password=PASSWORD, groups=['Administrators'])
    def test_runas_winrs_admin(self, username):
        runaspy = textwrap.dedent('''
        import sys
        import salt.utils.win_runas
        username = '{}'
        password = '{}'
        sys.exit(salt.utils.win_runas.runas('cmd.exe /C OPENFILES', username, password)['retcode'])
        '''.format(username, PASSWORD))
        with salt.utils.files.fopen(RUNAS_PATH, 'w') as fp:
            fp.write(runaspy)
        ret = subprocess.call("cmd.exe /C winrs /r:{} python {}".format(
            self.hostname, RUNAS_PATH), shell=True)
        self.assertEqual(ret, 0)

    @with_system_user('test-runas-admin', on_existing='delete', delete=True,
                      password=PASSWORD, groups=['Administrators'])
    def test_runas_winrs_admin_no_pass(self, username):
        runaspy = textwrap.dedent('''
        import sys
        import salt.utils.win_runas
        username = '{}'
        sys.exit(salt.utils.win_runas.runas('cmd.exe /C OPENFILES', username)['retcode'])
        '''.format(username))
        with salt.utils.files.fopen(RUNAS_PATH, 'w') as fp:
            fp.write(runaspy)
        ret = subprocess.call("cmd.exe /C winrs /r:{} python {}".format(
            self.hostname, RUNAS_PATH), shell=True)
        self.assertEqual(ret, 0)

    def test_runas_winrs_system_user(self):
        runaspy = textwrap.dedent('''
        import sys
        import salt.utils.win_runas
        sys.exit(salt.utils.win_runas.runas('cmd.exe /C OPENFILES', 'SYSTEM')['retcode'])
        ''')
        with salt.utils.files.fopen(RUNAS_PATH, 'w') as fp:
            fp.write(runaspy)
        ret = subprocess.call("cmd.exe /C winrs /r:{} python {}".format(
            self.hostname, RUNAS_PATH), shell=True)
        self.assertEqual(ret, 0)

    def test_runas_winrs_network_service_user(self):
        runaspy = textwrap.dedent('''
        import sys
        import salt.utils.win_runas
        sys.exit(salt.utils.win_runas.runas('cmd.exe /C OPENFILES', 'NETWORK SERVICE')['retcode'])
        ''')
        with salt.utils.files.fopen(RUNAS_PATH, 'w') as fp:
            fp.write(runaspy)
        ret = subprocess.call("cmd.exe /C winrs /r:{} python {}".format(
            self.hostname, RUNAS_PATH), shell=True)
        self.assertEqual(ret, 1)

    def test_runas_winrs_local_service_user(self):
        runaspy = textwrap.dedent('''
        import sys
        import salt.utils.win_runas
        sys.exit(salt.utils.win_runas.runas('cmd.exe /C OPENFILES', 'LOCAL SERVICE')['retcode'])
        ''')
        with salt.utils.files.fopen(RUNAS_PATH, 'w') as fp:
            fp.write(runaspy)
        ret = subprocess.call("cmd.exe /C winrs /r:{} python {}".format(
            self.hostname, RUNAS_PATH), shell=True)
        self.assertEqual(ret, 1)

    @with_system_user('test-runas', on_existing='delete', delete=True,
                      password=PASSWORD)
    def test_runas_powershell_remoting(self, username):
        psrp_wrap = (
            'powershell Invoke-Command -ComputerName {} -ScriptBlock {{ {} }}'
        )
        runaspy = textwrap.dedent('''
        import sys
        import salt.utils.win_runas
        username = '{}'
        password = '{}'
        sys.exit(salt.utils.win_runas.runas('cmd.exe /C OPENFILES', username, password)['retcode'])
        '''.format(username, PASSWORD))
        with salt.utils.files.fopen(RUNAS_PATH, 'w') as fp:
            fp.write(runaspy)
        cmd = 'python.exe {}'.format(RUNAS_PATH)
        ret = subprocess.call(
            psrp_wrap.format(self.hostname, cmd),
            shell=True
        )
        self.assertEqual(ret, 1)

    @with_system_user('test-runas', on_existing='delete', delete=True,
                      password=PASSWORD)
    def test_runas_powershell_remoting_no_pass(self, username):
        psrp_wrap = (
            'powershell Invoke-Command -ComputerName {} -ScriptBlock {{ {} }}'
        )
        runaspy = textwrap.dedent('''
        import sys
        import salt.utils.win_runas
        username = '{}'
        sys.exit(salt.utils.win_runas.runas('cmd.exe /C OPENFILES', username)['retcode'])
        '''.format(username))
        with salt.utils.files.fopen(RUNAS_PATH, 'w') as fp:
            fp.write(runaspy)
        cmd = 'python.exe {}'.format(RUNAS_PATH)
        ret = subprocess.call(
            psrp_wrap.format(self.hostname, cmd),
            shell=True
        )
        self.assertEqual(ret, 1)

    @with_system_user('test-runas-admin', on_existing='delete', delete=True,
                      password=PASSWORD, groups=['Administrators'])
    def test_runas_powershell_remoting_admin(self, username):
        psrp_wrap = (
            'powershell Invoke-Command -ComputerName {} -ScriptBlock {{ {} }}; exit $LASTEXITCODE'
        )
        runaspy = textwrap.dedent('''
        import sys
        import salt.utils.win_runas
        username = '{}'
        password = '{}'
        ret = salt.utils.win_runas.runas('cmd.exe /C OPENFILES', username, password)
        sys.exit(ret['retcode'])
        '''.format(username, PASSWORD))
        with salt.utils.files.fopen(RUNAS_PATH, 'w') as fp:
            fp.write(runaspy)
        cmd = 'python.exe {}; exit $LASTEXITCODE'.format(RUNAS_PATH)
        ret = subprocess.call(
            psrp_wrap.format(self.hostname, cmd),
            shell=True
        )
        self.assertEqual(ret, 0)

    @with_system_user('test-runas-admin', on_existing='delete', delete=True,
                      password=PASSWORD, groups=['Administrators'])
    def test_runas_powershell_remoting_admin_no_pass(self, username):
        psrp_wrap = (
            'powershell Invoke-Command -ComputerName {} -ScriptBlock {{ {} }}; exit $LASTEXITCODE'
        )
        runaspy = textwrap.dedent('''
        import sys
        import salt.utils.win_runas
        username = '{}'
        sys.exit(salt.utils.win_runas.runas('cmd.exe /C OPENFILES', username)['retcode'])
        '''.format(username))
        with salt.utils.files.fopen(RUNAS_PATH, 'w') as fp:
            fp.write(runaspy)
        cmd = 'python.exe {}; exit $LASTEXITCODE'.format(RUNAS_PATH)
        ret = subprocess.call(
            psrp_wrap.format(self.hostname, cmd),
            shell=True
        )
        self.assertEqual(ret, 0)

    @with_system_user('test-runas', on_existing='delete', delete=True,
                      password=PASSWORD)
    def test_runas_service(self, username, timeout=200):
        if os.path.exists(RUNAS_OUT):
            os.remove(RUNAS_OUT)
        assert not os.path.exists(RUNAS_OUT)
        runaspy = SERVICE_SOURCE.format(repr(RUNAS_OUT), username, PASSWORD)
        with io.open(RUNAS_PATH, 'w', encoding='utf-8') as fp:
            fp.write(runaspy)
        cmd = 'python.exe {}'.format(RUNAS_PATH)
        ret = subprocess.call(
            cmd,
            shell=True
        )
        self.assertEqual(ret, 0)
        win32serviceutil.StartService('test service')
        wait_for_service('test service')
        with salt.utils.files.fopen(RUNAS_OUT, 'r') as fp:
            ret = yaml.load(fp)
        assert ret['retcode'] == 1, ret

    @with_system_user('test-runas', on_existing='delete', delete=True,
                      password=PASSWORD)
    def test_runas_service_no_pass(self, username, timeout=200):
        if os.path.exists(RUNAS_OUT):
            os.remove(RUNAS_OUT)
        assert not os.path.exists(RUNAS_OUT)
        runaspy = SERVICE_SOURCE.format(repr(RUNAS_OUT), username, '')
        with io.open(RUNAS_PATH, 'w', encoding='utf-8') as fp:
            fp.write(runaspy)
        cmd = 'python.exe {}'.format(RUNAS_PATH)
        ret = subprocess.call(
            cmd,
            shell=True
        )
        self.assertEqual(ret, 0)
        win32serviceutil.StartService('test service')
        wait_for_service('test service')
        with salt.utils.files.fopen(RUNAS_OUT, 'r') as fp:
            ret = yaml.load(fp)
        assert ret['retcode'] == 1, ret

    @with_system_user('test-runas-admin', on_existing='delete', delete=True,
                      password=PASSWORD, groups=['Administrators'])
    def test_runas_service_admin(self, username, timeout=200):
        if os.path.exists(RUNAS_OUT):
            os.remove(RUNAS_OUT)
        assert not os.path.exists(RUNAS_OUT)
        runaspy = SERVICE_SOURCE.format(repr(RUNAS_OUT), username, PASSWORD)
        with io.open(RUNAS_PATH, 'w', encoding='utf-8') as fp:
            fp.write(runaspy)
        cmd = 'python.exe {}'.format(RUNAS_PATH)
        ret = subprocess.call(
            cmd,
            shell=True
        )
        self.assertEqual(ret, 0)
        win32serviceutil.StartService('test service')
        wait_for_service('test service')
        with salt.utils.files.fopen(RUNAS_OUT, 'r') as fp:
            ret = yaml.load(fp)
        assert ret['retcode'] == 0, ret

    @with_system_user('test-runas-admin', on_existing='delete', delete=True,
                      password=PASSWORD, groups=['Administrators'])
    def test_runas_service_admin_no_pass(self, username, timeout=200):
        if os.path.exists(RUNAS_OUT):
            os.remove(RUNAS_OUT)
        assert not os.path.exists(RUNAS_OUT)
        runaspy = SERVICE_SOURCE.format(repr(RUNAS_OUT), username, '')
        with io.open(RUNAS_PATH, 'w', encoding='utf-8') as fp:
            fp.write(runaspy)
        cmd = 'python.exe {}'.format(RUNAS_PATH)
        ret = subprocess.call(
            cmd,
            shell=True
        )
        self.assertEqual(ret, 0)
        win32serviceutil.StartService('test service')
        wait_for_service('test service')
        with salt.utils.files.fopen(RUNAS_OUT, 'r') as fp:
            ret = yaml.load(fp)
        assert ret['retcode'] == 0, ret

    def test_runas_service_system_user(self):
        if os.path.exists(RUNAS_OUT):
            os.remove(RUNAS_OUT)
        assert not os.path.exists(RUNAS_OUT)
        runaspy = SERVICE_SOURCE.format(repr(RUNAS_OUT), 'SYSTEM', '')
        with io.open(RUNAS_PATH, 'w', encoding='utf-8') as fp:
            fp.write(runaspy)
        cmd = 'python.exe {}'.format(RUNAS_PATH)
        ret = subprocess.call(
            cmd,
            shell=True
        )
        self.assertEqual(ret, 0)
        win32serviceutil.StartService('test service')
        wait_for_service('test service')
        with salt.utils.files.fopen(RUNAS_OUT, 'r') as fp:
            ret = yaml.load(fp)
        assert ret['retcode'] == 0, ret
