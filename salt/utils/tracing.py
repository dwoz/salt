from __future__ import absolute_import
import os
import collections
import functools
import logging
try:
    import opentracing
    import jaeger_client
    import jaeger_client.config
    HAS_TRACING = True
except ImportError:
    HAS_TRACING = False


_process_tracers = {}
_tracer_config = {}
_span_map = {}
log = logging.getLogger(__name__)

TracerConf = collections.namedtuple('TracerConf', 'config, tracer')

Format = opentracing.Format


class TracingError(RuntimeError):
    '''
    General salt.utils.tracing exception
    '''


def _reload_module(module):
    '''
    Reload a python module
    '''
    import importlib
    if hasattr(importlib, 'reload'):
        importlib.reload(module)
    else:
        reload(module)


def _noop_check():
    return not _tracer_config or not HAS_TRACING


class TracerWrapper(object):

    def start_span(self, operation_name, references=None, child_of=None,
            tags=None, start_time=None, missing_parent='warn'):
        '''
        '''
        if _noop_check():
            return SpanWrapper()
        parent_span = None
        if child_of:
            if child_of in _span_map:
                parent_span = _span_map[child_of]
            elif missing_parent == 'warn':
                log.warn('Trace missing parent span: %s', child_of)
            elif missing_parent == 'raise':
                raise RuntimeError('Tracer missing parent span: {}'.format(child_of))
        if isinstance(parent_span, SpanWrapper):
            # make sure we're using the current process tracer, this is a
            # work around for when a span is passed to a
            # multiprocess.Process target.
            parent_span.span._tracer = jaeger_tracer()
            parent_span = parent_span.context
        if operation_name in _span_map:
            raise TracingError('Span with \'{}\' already exists'.format(name))
        print(references)
        span = SpanWrapper(jaeger_tracer().start_span(
            operation_name, references=references, child_of=parent_span,
            tags=tags, start_time=start_time
        ))
        _span_map[span.span.operation_name] = span
        return span

    def extract(self, *args, **kwargs):
        if _noop_check():
            return
        return jaeger_tracer().extract(*args, **kwargs)

    def inject(self, *args, **kwargs):
        if _noop_check():
            return
        return jaeger_tracer().inject(*args, **kwargs)

    def configure(self, *args, **kwargs):
        configure_tracing(*args, **kwargs)

    def close(self):
        if _noop_check():
            return
        jaeger_tracer().close()


tracer = tracer_wrapper = TracerWrapper()


def _wrap_attr(attr_name, instance):
    return getattr(instance.span, attr_name)


def wrap_attr(attr_name, instance):
    return functools.partial(_wrap_attr, attr_name)


class SpanWrapper(object):

    def __init__(self, span=None):
        self.span = span

    @property
    def tracer(self):
        return tracer_wrapper

    @property
    def context(self):
        if self.span:
            return self.span.context

    def finish(self):
        if self.span:
            try:
                return _span_map.pop(self.span.operation_name).span.finish()
            except KeyError:
                raise TracingError("Span not in map")

    def log_kv(self, *args, **kwargs):
        if self.span:
            return self.span.log_kv(*args, **kwargs)


def configure_tracing(*args, **kwargs):
    '''
    Configure tracing for this process and any sub-processes
    '''
    if not HAS_TRACING:
        raise TracingError(
            'Tracer dependencies not installed. Install jeager-client'
        )
    if _tracer_config:
        raise TracingError('Tracer configured')
    _tracer_config['args'] = args
    _tracer_config['kwargs'] = kwargs
    return tracer


def jaeger_tracer():
    '''
    Return a jeager tracer instance for this process
    '''
    pid = os.getpid()
    if pid in _process_tracers:
        return _process_tracers[pid].tracer
    elif _tracer_config:
        _reload_module(opentracing)
        _reload_module(jaeger_client)
        _reload_module(jaeger_client.config)
        jaeger_config = jaeger_client.Config(
            *_tracer_config['args'],
            **_tracer_config['kwargs']
        )
        _process_tracers[pid] = TracerConf(
            jaeger_config,
            jaeger_config.initialize_tracer(),
        )
        return _process_tracers[pid].tracer
    raise TracingError('No tracer configured')


def extract(carrier, fmt=Format.TEXT_MAP, tracer=tracer):
    return tracer.extract(fmt, carrier)


def inject(context, carrier, fmt=Format.TEXT_MAP, tracer=tracer):
    return tracer.inject(context, fmt, carrier)


def start_span(*args, **kwargs):
    return tracer.start_span(*args, **kwargs)


def trace(func, name, tags=None, child_of=None, as_keyword=None, missing_parent='warn'):
    @functools.wraps(func)
    def wrapper(*args, **kwargs):
        with tracer.start_span(name, tags=tags, child_of=child_of, missing_parent=missing_parent) as span:
            if as_keyword:
                if as_keyword in kwargs:
                    raise TracingError(
                        'Name \'{}\' already in kwargs'.format(as_keyword)
                    )
                kwargs[as_keyword] = span
            return func(*args, **kwargs)
    return wrapper
