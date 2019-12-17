# -*- coding: utf-8 -*-
from __future__ import absolute_import, print_function, unicode_literals
from unittest import TestCase

import salt.utils.tracing


class TestTracingNullSpan(TestCase):

    def test_null_span_finish(self):
        span = salt.utils.tracing.SpanWrapper()
        assert span.span == None
        try:
            span.finish()
        except Exception:
            self.fail("SpanWrapper.finish() raised Exception unexpectedly!")

    def test_null_span_log(self):
        span = salt.utils.tracing.SpanWrapper()
        assert span.span == None
        try:
            span.log_kv({'event': 'meh 3'})
        except Exception:
            self.fail("SpanWrapper.log_kv() raised Exception unexpectedly!")

    def test_null_span_context(self):
        span = salt.utils.tracing.SpanWrapper()
        assert span.span == None
        try:
            assert span.context == None
        except Exception:
            self.fail("SpanWrapper.context raised Exception unexpectedly!")

    def test_null_span_tracer(self):
        span = salt.utils.tracing.SpanWrapper()
        assert span.span == None
        assert isinstance(span.tracer, salt.utils.tracing.TracerWrapper)

class TestTracingNullTracer(TestCase):

    def test_null_tracer_close(self):
        tracer = salt.utils.tracing.tracer
        try:
            tracer.close()
        except Exception:
            self.fail("TracerWrapper.close raised Exception unexpectedly!")

