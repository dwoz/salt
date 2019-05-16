# -*- coding: utf-8 -*-
'''
Helpers/utils for working with tornado asynchronous stuff
'''

from __future__ import absolute_import, print_function, unicode_literals
import logging
import sys
import os
import traceback

import tornado.ioloop
import tornado.concurrent
import contextlib
from multiprocessing.pool import ThreadPool
from concurrent.futures import ThreadPoolExecutor
import tornado.gen
import functools
from salt.ext.six.moves import queue
import threading
import time

import asyncio
from tornado.platform.asyncio import AsyncIOMainLoop
from salt.ext.six import reraise

AsyncIOMainLoop().install()
loop = asyncio.get_event_loop()

log = logging.getLogger(__name__)

_IOLoop = tornado.ioloop.IOLoop
loop_cache = {}
_workers = ThreadPoolExecutor(10)

class ThreadedSyncRunner(object):
    '''
    A wrapper around tornado.ioloop.IOLoop.run_sync which farms the work of
    `run_sync` out to a thread. This is to facilitate calls to `run_sync` when
    there is already an ioloop running in the current thread.
    '''

    def __init__(self, io_loop=None, lock=None):
       if io_loop is None:
           self.io_loop = tornado.ioloop.IOLoop()
       else:
           self.io_loop = io_loop
       if io_loop is None:
           self.lock = threading.Semaphore()
       else:
           self.lock = lock
       self._run_sync_thread = None
       self._run_sync_result = None
       self._run_sync_exc_info = None

    def _run_sync_target(self, func, timeoout=None):
        try:
            self._run_sync_result = self.io_loop.run_sync(func)
        except Exception as exc:
            # Exception is re-raised in parent thread
            self._run_sync_exc_info = sys.exc_info()

    def run_sync(self, func, timeout=None):
        with self.lock:
            self._run_sync_thread = threading.Thread(
                target=self._run_sync_target,
                args=(func,),
            )
            self._run_sync_thread.start()
            self._run_sync_thread.join()
            if self._run_sync_exc_info is None:
                result = self._run_sync_result
                self._run_sync_result = None
            else:
                reraise(*self._run_sync_exc_info)
                self._run_sync_exc_info = None
            return result


def stack(length=4):
    return '\n'.join(traceback.format_stack()[-(length+1):-1])


class IOLoop(object):
    '''
    A wrapper around an existing IOLoop implimentation.
    '''
    class_local = {'started': False}

    def __init__(self, *args, **kwargs):
        self._ioloop = kwargs.get(
            '_ioloop',
            tornado.ioloop.IOLoop.current()
        )
        self.sync_runner = kwargs.get(
            'sync_runner',
            ThreadedSyncRunner()
        )

    def __getattr__(self, key):
        return getattr(self._ioloop, key)

    def start(self, *args, **kwargs):
        if not self.class_local['started']:
            self.class_local['started'] = True
            self._ioloop.start()
        else:
            log.error("Tried to start event loop again: %s", stack())

    def close(self, *args, **kwargs):
        log.trace("IOLoop.close called %s", stack())
        pass

    def run_sync(self, func, timeout=None):
        asyncio_loop = asyncio.get_event_loop()
        if self.is_running() or (asyncio_loop and asyncio_loop.is_running()):
            #log.trace("run_sync - running %s", stack())
            return self.sync_runner.run_sync(func)
        else:
            #log.trace("run_sync - not running %s", stack())
            return self._ioloop.run_sync(func)

    def is_running(self):
        try:
            return self._ioloop.is_running()
        except AttributeError:
            pass
        return self._ioloop.asyncio_loop.is_running()


@contextlib.contextmanager
def current_ioloop(io_loop):
    '''
    A context manager that will set the current ioloop to io_loop for the context
    '''
    # TODO: Remove current_ioloop calls
    yield
    #orig_loop = tornado.ioloop.IOLoop.current()
    #io_loop.make_current()
    #try:
    #    yield
    #finally:
    #    orig_loop.make_current()

class SyncWrapper(object):

    def __init__(self, cls, args=None, kwargs=None, async_methods=None, stop_methods=None):
        #log.error("WTF %s %s", cls, '\n'.join(traceback.format_stack()))
        if args is None:
            args = []
        if kwargs is None:
            kwargs = {}
        if async_methods is None:
            async_methods = []
        if stop_methods is None:
            stop_methods = []
        self.args = args
        self.kwargs = kwargs
        self.cls = cls
        self.obj = None
        self._async_methods = async_methods
        for name in dir(cls):
            if tornado.gen.is_coroutine_function(getattr(cls, name)):
                self._async_methods.append(name)
        self._stop_methods = stop_methods
        self._req = queue.Queue()
        self._res = queue.Queue()
        self._stop = threading.Event()
        self._thread = threading.Thread(
            target=self._target,
            args=(cls, args, kwargs, self._req, self._res, self._stop, self.stop),
            daemon=True,
        )
        self._current_future = None
        self.start()

    def __repr__(self):
        return '<SyncWrapper(cls={})'.format(self.cls)

    def start(self):
        self._thread.start()
        while self.obj is None:
            time.sleep(.01)

    def stop(self):
        log.error("STOP - begin %r", self)
        for method in self._stop_methods:
            log.error("run close method %s %r", method, self.obj)
            try:
                method = getattr(self, method)
            except AttributeError:
                log.error("No method %s on object %r", method, self.obj)
                continue
            log.error("*** Got method %s", method)
            method()
        log.error("STOP - after methods %r", self.obj)
        if self._current_future:
            self._current_future.cancel()
        log.error("STOP - set event %r", self)
        self._stop.set()
        log.error("STOP - join %r", self)
        self._thread.join()
        log.error("STOP - end %r", self)

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
        io_loop = tornado.ioloop.IOLoop()
        obj = cls(*args, **kwargs)
        for name in dir(obj):
            if tornado.gen.is_coroutine_function(getattr(obj, name)):
                self._async_methods.append(name)
        self.obj = obj
        def callback(future):
            io_loop.stop()
        io_loop.add_future(self.arg_handler(io_loop, stop, requests, responses, obj), callback)
        io_loop.start()

    @tornado.gen.coroutine
    def arg_handler(self, io_loop, stop, requests, responses, obj):
        while not stop.is_set():
            try:
                attr_name, call_args, call_kwargs = requests.get(block=False)
            except queue.Empty:
                yield tornado.gen.sleep(.01)
            else:
                attr = getattr(obj, attr_name)
                ret = attr(*call_args, **call_kwargs)
                def callback(future):
                    self._current_future = None
                    res = future.result()
                    responses.put((True, res,))
                io_loop.add_future(ret, callback)
                self._current_future = ret
                yield
        raise tornado.gen.Return(True)
