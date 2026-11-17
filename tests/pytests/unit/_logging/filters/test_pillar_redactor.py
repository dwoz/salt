"""
Unit tests for :mod:`salt._logging.filters.pillar`.

Covers the three redaction strategies, the disabled fast-path, and
the handler-level install helper.
"""

import logging

import pytest

from salt._logging.filters.pillar import (
    REDACT_PLACEHOLDER,
    PillarRedactingFilter,
    install_on_handler,
)


def _make_record(msg, args=None, name="salt.states.pkg"):
    # LogRecord unwraps a single-dict tuple to a plain dict (so that
    # ``log.info("%(x)s", {"x": 1})`` works); bypass that by assigning
    # ``args`` after construction so our tests can pass either a tuple
    # or a mapping and get back exactly what they passed.
    record = logging.LogRecord(
        name=name,
        level=logging.INFO,
        pathname=__file__,
        lineno=1,
        msg=msg,
        args=None,
        exc_info=None,
    )
    record.args = args
    return record


class TestDisabledFastPath:
    def test_passthrough_when_disabled(self):
        flt = PillarRedactingFilter({"pillar_mask_output": False})
        record = _make_record("db password is %s", args=({"root_password": "hunter2"},))
        assert flt.filter(record) is True
        # args untouched
        assert record.args == ({"root_password": "hunter2"},)
        assert record.msg == "db password is %s"

    def test_passthrough_when_no_opts(self):
        flt = PillarRedactingFilter({})
        record = _make_record("x %s", args=({"password": "s3cret"},))
        flt.filter(record)
        assert record.args == ({"password": "s3cret"},)


class TestKeyHeuristic:
    def test_dict_in_tuple_args(self):
        flt = PillarRedactingFilter(
            {
                "pillar_mask_output": True,
                "pillar_mask_log_keys": ["password", "token"],
            }
        )
        record = _make_record(
            "state ran %s", args=({"user": "alice", "password": "hunter2"},)
        )
        flt.filter(record)
        scrubbed = record.args[0]
        assert scrubbed["password"] == REDACT_PLACEHOLDER
        # Non-sensitive key untouched
        assert scrubbed["user"] == "alice"

    def test_nested_dict(self):
        flt = PillarRedactingFilter(
            {
                "pillar_mask_output": True,
                "pillar_mask_log_keys": ["password"],
            }
        )
        record = _make_record("ran %s", args=({"db": {"root_password": "hunter2"}},))
        flt.filter(record)
        assert record.args[0]["db"]["root_password"] == REDACT_PLACEHOLDER

    def test_case_insensitive_substring(self):
        flt = PillarRedactingFilter(
            {"pillar_mask_output": True, "pillar_mask_log_keys": ["secret"]}
        )
        record = _make_record("%s", args=({"MySecretKey": "boo"},))
        flt.filter(record)
        assert record.args[0]["MySecretKey"] == REDACT_PLACEHOLDER

    def test_dict_as_args(self):
        """record.args may be a mapping when msg uses named formatting."""
        flt = PillarRedactingFilter(
            {"pillar_mask_output": True, "pillar_mask_log_keys": ["password"]}
        )
        record = _make_record("%(user)s", args={"user": "a", "password": "p"})
        flt.filter(record)
        assert record.args["password"] == REDACT_PLACEHOLDER
        assert record.args["user"] == "a"


class TestPathDenylist:
    def test_regex_sub_in_msg(self):
        opts = {
            "pillar_mask_output": True,
            "pillar_mask_log_paths": ["database:root_password"],
            "pillar": {"database": {"root_password": "hunter2"}},
        }
        flt = PillarRedactingFilter(opts)
        record = _make_record("the password is hunter2 today")
        flt.filter(record)
        assert "hunter2" not in record.msg
        assert REDACT_PLACEHOLDER in record.msg

    def test_regex_sub_in_args_values(self):
        opts = {
            "pillar_mask_output": True,
            "pillar_mask_log_paths": ["api_key"],
            "pillar": {"api_key": "sk-abcdef12345"},
        }
        flt = PillarRedactingFilter(opts)
        record = _make_record(
            "sent %s", args=({"headers": {"X-Auth": "Bearer sk-abcdef12345"}},)
        )
        flt.filter(record)
        assert "sk-abcdef12345" not in str(record.args)

    def test_missing_path_is_noop(self):
        opts = {
            "pillar_mask_output": True,
            "pillar_mask_log_paths": ["does:not:exist"],
            "pillar": {},
        }
        flt = PillarRedactingFilter(opts)
        record = _make_record("plain message with hunter2")
        flt.filter(record)
        assert record.msg == "plain message with hunter2"


class TestSourceScoping:
    def test_logger_prefix_scope(self):
        flt = PillarRedactingFilter(
            {
                "pillar_mask_output": True,
                "pillar_mask_log_keys": ["password"],
                "pillar_mask_log_sources": ["salt.states"],
            }
        )
        # Matching scope
        record_in = _make_record(
            "%s", args=({"password": "p"},), name="salt.states.pkg"
        )
        flt.filter(record_in)
        assert record_in.args[0]["password"] == REDACT_PLACEHOLDER

        # Out of scope -> untouched
        record_out = _make_record(
            "%s", args=({"password": "p"},), name="urllib3.connectionpool"
        )
        flt.filter(record_out)
        assert record_out.args[0]["password"] == "p"

    def test_empty_prefix_matches_all(self):
        flt = PillarRedactingFilter(
            {
                "pillar_mask_output": True,
                "pillar_mask_log_keys": ["password"],
                "pillar_mask_log_sources": [""],
            }
        )
        record = _make_record(
            "%s", args=({"password": "p"},), name="urllib3.connectionpool"
        )
        flt.filter(record)
        assert record.args[0]["password"] == REDACT_PLACEHOLDER


class TestInstallOnHandler:
    def test_attaches_filter(self):
        handler = logging.StreamHandler()
        opts = {
            "pillar_mask_output": True,
            "pillar_mask_log_keys": ["password"],
        }
        flt = install_on_handler(handler, opts)
        assert isinstance(flt, PillarRedactingFilter)
        assert flt in handler.filters

    def test_idempotent(self):
        handler = logging.StreamHandler()
        opts = {"pillar_mask_output": True}
        flt1 = install_on_handler(handler, opts)
        flt2 = install_on_handler(handler, opts)
        assert flt1 is flt2
        assert sum(isinstance(f, PillarRedactingFilter) for f in handler.filters) == 1

    def test_handler_pipeline_scrubs(self, caplog):
        """
        Install on root's propagation point (caplog's handler) and
        verify the record seen by caplog has scrubbed args.
        """
        opts = {
            "pillar_mask_output": True,
            "pillar_mask_log_keys": ["password"],
        }
        # caplog.handler is attached to root and picks up propagated records
        install_on_handler(caplog.handler, opts)
        try:
            log = logging.getLogger("salt.states.test_boundary")
            log.setLevel(logging.DEBUG)
            with caplog.at_level(logging.DEBUG, logger="salt.states.test_boundary"):
                log.info("payload %s", {"password": "hunter2"})
            # The record's args were mutated before caplog formatted it
            assert all("hunter2" not in str(r.args) for r in caplog.records)
        finally:
            caplog.handler.filters = [
                f
                for f in caplog.handler.filters
                if not isinstance(f, PillarRedactingFilter)
            ]


class TestFilterSafety:
    def test_filter_never_raises(self):
        """A bug in the scrubber must not silence logging."""
        flt = PillarRedactingFilter(
            {
                "pillar_mask_output": True,
                "pillar_mask_log_keys": ["password"],
            }
        )

        class _Weird:
            def __iter__(self):
                raise RuntimeError("explode")

        record = _make_record("x", args=(_Weird(),))
        # No assertion on scrubbing -- just that filter() returned True
        assert flt.filter(record) is True


if __name__ == "__main__":
    pytest.main([__file__, "-xvs"])
