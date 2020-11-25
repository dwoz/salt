import os
import salt.loader

import pytest
from tests.support.helpers import dedent


@pytest.fixture
def loader_dir(tmp_path):
    mod_content = dedent("""
    def __virtual__():
        return True

    def set_context(key, value):
        __context__[key] = value

    def get_context(key):
        return __context__[key]
    """)
    with open(os.path.join(tmp_path, 'mod_a.py'), 'w') as fp:
        fp.write(mod_content)
    with open(os.path.join(tmp_path, 'mod_b.py'), 'w') as fp:
        fp.write(mod_content)
    yield tmp_path


def test_loader_a(loader_dir):
    opts = {
        "optimization_order": [0, 1, 2]
    }
    loader_a = salt.loader.LazyLoader(
        [str(loader_dir)],
        opts,
        tag='test',
    )
    loader_b = salt.loader.LazyLoader(
        [loader_dir],
        opts,
        tag='test',
    )
    loader_a._load_all()
    loader_a['mod_a.set_context']('foo', 'bar')
    assert loader_a.pack['__context__'] == {'foo': 'bar'}
    assert loader_a['mod_b.get_context']('foo') == 'bar'
    with pytest.raises(KeyError):
        loader_b['mod_a.get_context']('foo')
