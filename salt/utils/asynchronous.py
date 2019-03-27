# -*- coding: utf-8 -*-
'''
Helpers/utils for working with tornado asynchronous stuff
'''

from __future__ import absolute_import, print_function, unicode_literals

import tornado.ioloop
import tornado.concurrent
import tornado.gen
import contextlib
import threading
try:
    import queue
except ImportError:
    import Queue as queue

from salt.utils import zeromq
import logging

log = logging.getLogger(__name__)

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

        self.io_loop = zeromq.ZMQDefaultLoop()
        kwargs['io_loop'] = self.io_loop

        with current_ioloop(self.io_loop):
            self.asynchronous = method(*args, **kwargs)

    def __getattribute__(self, key):
        try:
            return object.__getattribute__(self, key)
        except AttributeError as ex:
            if key == 'asynchronous':
                raise ex
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

        else:
            return attr

    def _block_future(self, future):
        self.io_loop.add_future(future, lambda future: self.io_loop.stop())
        self.io_loop.start()
        return future.result()

    def __del__(self):
        '''
        On deletion of the asynchronous wrapper, make sure to clean up the asynchronous stuff
        '''
        if hasattr(self, 'asynchronous'):
            if hasattr(self.asynchronous, 'close'):
                # Certain things such as streams should be closed before
                # their associated io_loop is closed to allow for proper
                # cleanup.
                self.asynchronous.close()
            del self.asynchronous
            self.io_loop.close()
            del self.io_loop
        elif hasattr(self, 'io_loop'):
            self.io_loop.close()
            del self.io_loop


class SyncClassMultiWrapper(object):

    def __init__(self, cls, args=None, kwargs=None):
        if args is None:
            args = []
        if kwargs is None:
            kwargs = {}
        self.args = args
        self.kwargs = kwargs
        self.cls = cls
        self._async_methods = []
        for name in dir(cls):
            if tornado.gen.is_coroutine_function(getattr(cls, name)):
                self._async_methods.append(name)
        self._req = queue.Queue()
        self._res = queue.Queue()
        self._stop = threading.Event()
        self._thread = threading.Thread(
            target=self._target,
            args=(cls, args, kwargs, self._req, self._res, self._stop, self.stop),
        )
        self.start()

    def __repr__(self):
        return '<SyncWrapper(cls={})'.format(self.cls)

    def start(self):
        self._thread.start()

    def stop(self):
        self._stop.set()
        self._thread.join()

    def __getattribute__(self, key):
        ex = None
        try:
            return object.__getattribute__(self, key)
        except AttributeError as ex:
            if key == 'obj':
                raise ex
        if key in self._async_methods:
            def wrap(*args, **kwargs):
                self._req.put((key, args, kwargs,))
                success, result = self._res.get()
                if not success:
                    raise result
                return result
            return wrap
        return getattr(self.obj, key)

    def _target(self, cls, args, kwargs, requests, responses, stop, stop_class):
        from salt.utils.zeromq import zmq, ZMQDefaultLoop, install_zmq
        tornado.ioloop.IOLoop.clear_current()
        io_loop = tornado.ioloop.IOLoop()
        io_loop.make_current()
        #install_zmq()
        #kwargs['io_loop'] = io_loop
        self.obj = obj = cls(*args, **kwargs)
        def callback(future):
            io_loop.stop()
        io_loop.add_future(self.arg_handler(io_loop, stop, requests, responses, obj), callback)
        io_loop.start()

    @staticmethod
    @tornado.gen.coroutine
    def arg_handler(io_loop, stop, requests, responses, obj):
        while not stop.is_set():
            try:
                attr_name, call_args, call_kwargs = requests.get(block=False)
            except queue.Empty:
                yield
                continue
            else:
                attr = getattr(obj, attr_name)
                ret = attr(*call_args, **call_kwargs)
                def callback(future):
                    res = future.result()
                    responses.put((True, res,))
                io_loop.add_future(ret, callback)
                yield
        raise tornado.gen.Return(True)

SyncWrapper = SyncClassMultiWrapper
