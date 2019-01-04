import logging
import inspect

import six

try:
    import win32service
    import win32serviceutil
    import win32event
    import servicemanager
    HAS_WIN32 = True
except ImportError:
    # Mock win32serviceutil object to avoid
    # a stacktrace in the _ServiceManager class
    win32serviceutil = Mock()
    HAS_WIN32 = False


log = logging.getLogger(__name__)


def _default_target(service, *args, **kwargs):
    while service.active:
        time.sleep(service.timeout)


class _ServiceManager(win32serviceutil.ServiceFramework):
    '''
    A windows service manager
    '''

    _svc_name_ = "Service Manager"
    _svc_display_name_ = "Service Manager"
    _svc_description_ = "A Service Manager"
    run_in_foreground = False
    target = _default_target

    def __init__(self, args, target=None, timeout=60, active=True):
        win32serviceutil.ServiceFramework.__init__(self, args)
        self.hWaitStop = win32event.CreateEvent(None, 0, 0, None)
        self.timeout = timeout
        self.active = active
        if target is not None:
            self.target = target

    @classmethod
    def log_error(cls, msg):
        if cls.run_in_foreground:
            logger.error(msg)
        servicemanager.LogErrorMsg(msg)

    @classmethod
    def log_info(cls, msg):
        if cls.run_in_foreground:
            logger.info(msg)
        servicemanager.LogInfoMsg(msg)

    @classmethod
    def log_exception(cls, msg):
        if cls.run_in_foreground:
            logger.exception(msg)
        exc_info = sys.exc_info()
        tb = traceback.format_tb(exc_info[2])
        servicemanager.LogErrorMsg("{} {} {}".format(msg, exc_info[1], tb))

    @property
    def timeout_ms(self):
        return self.timeout * 1000

    def SvcStop(self):
        """
        Stop the service by; terminating any subprocess call, notify
        windows internals of the stop event, set the instance's active
        attribute to 'False' so the run loops stop.
        """
        self.ReportServiceStatus(win32service.SERVICE_STOP_PENDING)
        win32event.SetEvent(self.hWaitStop)
        self.active = False

    def SvcDoRun(self):
        """
        Run the monitor in a separete thread so the main thread is
        free to react to events sent to the windows service.
        """
        servicemanager.LogMsg(
            servicemanager.EVENTLOG_INFORMATION_TYPE,
            servicemanager.PYS_SERVICE_STARTED,
            (self._svc_name_, ''),
        )
        self.log_info("Starting Service {}".format(self._svc_name_))
        monitor_thread = threading.Thread(target=self.target_thread)
        monitor_thread.start()
        while self.active:
            rc = win32event.WaitForSingleObject(self.hWaitStop, self.timeout_ms)
            if rc == win32event.WAIT_OBJECT_0:
                # Stop signal encountered
                self.log_info("Stopping Service")
                break
            if not monitor_thread.is_alive():
                self.log_info("Update Thread Died, Stopping Service")
                break

    def target_thread(self, *args, **kwargs):
        """
        Target Thread, handles any exception in the target method and
        logs them.
        """
        self.log_info("Monitor")
        try:
            self.target(self, *args, **kwargs)
        except Exception as exc:
            # TODO: Add traceback info to windows event log objects
            self.log_exception("Exception In Target")

    @classmethod
    def install(cls, username=None, password=None, start_type=None):
        if hasattr(cls, '_svc_reg_class_'):
            svc_class = cls._svc_reg_class_
        else:
            svc_class = win32serviceutil.GetServiceClassString(cls)
        win32serviceutil.InstallService(
            svc_class,
            cls._svc_name_,
            cls._svc_display_name_,
            description=cls._svc_description_,
            userName=username,
            password=password,
            startType=start_type,
        )

    @classmethod
    def remove(cls):
        win32serviceutil.RemoveService(
            cls._svc_name_
        )

    @classmethod
    def start(cls):
        win32serviceutil.StartService(
            cls._svc_name_
        )

    @classmethod
    def restart(cls):
        win32serviceutil.RestartService(
            cls._svc_name_
        )

    @classmethod
    def stop(cls):
        win32serviceutil.StopService(
            cls._svc_name_
        )


def service_class_factory(
        cls_name, name, target=_default_target, display_name='', description='',
        run_in_foreground=False):
    frm = inspect.stack()[1]
    mod = inspect.getmodule(frm[0])
    if six.PY2:
        cls_name = cls_name.encode()
    return type(
        cls_name,
        (_ServiceManager, object),
        {
            '__module__': mod.__name__,
            '_svc_name_': name,
            '_svc_display_name_': display_name or name,
            '_svc_description_':  description,
            'run_in_foreground': run_in_foreground,
            'target': target,
        },
    )
