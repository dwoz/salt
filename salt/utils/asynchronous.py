# -*- coding: utf-8 -*-
'''
Helpers/utils for working with tornado asynchronous stuff
'''

from __future__ import absolute_import, print_function, unicode_literals

import sys

import tornado.ioloop
import tornado.concurrent
import contextlib
from salt.ext import six
import salt.utils.zeromq


salt.utils.zeromq.install_zmq()


@contextlib.contextmanager
def current_ioloop(io_loop):
    '''
    A context manager that will set the current ioloop to io_loop for the context
    '''
    orig_loop = tornado.ioloop.IOLoop.current()
    io_loop.make_current()
    try:
        yield
    finally:
        orig_loop.make_current()


class IOLoop(object):
    '''
    A wrapper around an existing IOLoop implimentation.
    '''
    seed_loop = None

    @classmethod
    def _current(cls):
        return tornado.ioloop.IOLoop.current()

    @classmethod
    def _is_closed(cls, loop):
        if hasattr(loop, '_salt_close_called'):
            if loop._salt_close_called is True:
                return True
        if hasattr(loop, '_closing'):
            if loop._closing is True:
                return True
        if hasattr(loop, 'asyncio_loop'):
            if loop.asyncio_loop.is_closed() is True:
                return True
        return False

    def __init__(self, *args, **kwargs):
        self._ioloop = kwargs.get(
            '_ioloop',
            self._current()
        )
        self.sync_runner = None

    def __getattr__(self, key):
        return getattr(self._ioloop, key)

    def add_handler(self, *args, **kwargs):
        try:
            self._ioloop.add_handler(*args, **kwargs)
        except Exception:  # pylint: disable=broad-except
            six.reraise(*sys.exc_info())

    def start(self, *args, **kwargs):
        self._ioloop.start()

    def stop(self, *args, **kwargs):
        self._ioloop._salt_started_called = False
        self._ioloop.stop()

    def close(self, *args, **kwargs):
        self._ioloop.close()

    def run_sync(self, func, timeout=None):
        self._ioloop.run_sync(func, timeout=timeout)

    def is_running(self):
        return self._io_loop.is_running()


class SyncWrapper(object):
    '''
    A wrapper to make Async classes synchronous

    This is uses as a simple wrapper, for example:

    asynchronous = AsyncClass()
    # this method would reguarly return a future
    future = asynchronous.async_method()

    sync = SyncWrapper(async_factory_method, (arg1, arg2), {'kwarg1': 'val'})
    # the sync wrapper will automatically wait on the future
    ret = sync.async_method()
    '''
    def __init__(self, method, args=tuple(), kwargs=None):
        if kwargs is None:
            kwargs = {}

        self.io_loop = salt.utils.zeromq.ZMQDefaultLoop()
        kwargs['io_loop'] = self.io_loop

        with current_ioloop(self.io_loop):
            self.asynchronous = method(*args, **kwargs)

    def __getattribute__(self, key):
        try:
            return object.__getattribute__(self, key)
        except AttributeError:
            if key == 'asynchronous':
                six.reraise(*sys.exc_info())
        attr = getattr(self.asynchronous, key)
        if hasattr(attr, '__call__'):
            def wrap(*args, **kwargs):
                # Overload the ioloop for the func call-- since it might call .current()
                with current_ioloop(self.io_loop):
                    ret = attr(*args, **kwargs)
                    if isinstance(ret, tornado.concurrent.Future):
                        ret = self._block_future(ret)
                    return ret
            return wrap
        return attr

    def _block_future(self, future):
        self.io_loop.add_future(future, lambda future: self.io_loop.stop())
        self.io_loop.start()
        return future.result()

    def close(self):
        if hasattr(self, 'asynchronous'):
            if hasattr(self.asynchronous, 'close'):
                # Certain things such as streams should be closed before
                # their associated io_loop is closed to allow for proper
                # cleanup.
                self.asynchronous.close()
            elif hasattr(self.asynchronous, 'destroy'):
                # Certain things such as streams should be closed before
                # their associated io_loop is closed to allow for proper
                # cleanup.
                self.asynchronous.destroy()
            del self.asynchronous
            self.io_loop.close()
            del self.io_loop
        elif hasattr(self, 'io_loop'):
            self.io_loop.close()
            del self.io_loop

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.close()

    # pylint: disable=W1701
    def __del__(self):
        '''
        On deletion of the asynchronous wrapper, make sure to clean up the asynchronous stuff
        '''
        self.close()
    # pylint: enable=W1701
