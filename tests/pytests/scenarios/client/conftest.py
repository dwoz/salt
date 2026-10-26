import pytest


@pytest.fixture(scope="package")
def salt_master(salt_master_factory):
    """
    A running salt-master fixture for client scenario tests
    """
    with salt_master_factory.started():
        yield salt_master_factory
