import logging
import os
import re
import shutil
import subprocess
import sys
import tarfile

import requests

import salt.utils.files
import salt.utils.hashutils
import salt.utils.http
import salt.utils.thin

log = logging.getLogger(__name__)


def gen_relenv_from_current(cachedir, kernel, os_arch):
    """
    Generate a dev-mode relenv tarball from the current Salt installation.
    This is used as a fallback when the real relenv tarball cannot be downloaded
    (e.g., in test environments or air-gapped systems).

    The tarball will contain:
    - Current Salt installation
    - salt-call script that wraps python3 -m salt.scripts.salt_call

    :param cachedir: The cache directory where the tarball will be stored.
    :param kernel: The detected OS (e.g., 'linux', 'darwin', 'windows').
    :param os_arch: The detected architecture (e.g., 'amd64', 'x86_64', 'arm64').
    :return: The path to the tarball file.
    """
    log.info("Generating dev-mode relenv tarball from current Salt installation")

    # Set up directories
    relenv_dir = os.path.join(cachedir, "relenv", kernel, os_arch)
    if not os.path.isdir(relenv_dir):
        os.makedirs(relenv_dir)

    tarball_path = os.path.join(relenv_dir, "salt-relenv.tar.xz")

    # If tarball already exists, return it
    if os.path.exists(tarball_path):
        log.info("Dev-mode relenv tarball already exists: %s", tarball_path)
        return tarball_path

    # Find salt module location
    import salt
    salt_path = os.path.dirname(os.path.dirname(salt.__file__))

    # Create a temporary directory to build the relenv structure
    import tempfile
    with tempfile.TemporaryDirectory() as tmpdir:
        # Create directory structure similar to relenv onedir
        relenv_root = os.path.join(tmpdir, "salt")
        os.makedirs(relenv_root)

        # Copy Salt installation
        log.info("Copying Salt from %s to %s", salt_path, relenv_root)
        for item in os.listdir(salt_path):
            src = os.path.join(salt_path, item)
            dst = os.path.join(relenv_root, item)
            if os.path.isdir(src):
                shutil.copytree(src, dst, symlinks=True, ignore_dangling_symlinks=True)
            else:
                shutil.copy2(src, dst)

        # Create salt-call wrapper script
        salt_call_wrapper = os.path.join(relenv_root, "salt-call")
        with open(salt_call_wrapper, "w") as f:
            f.write(f"""#!/bin/bash
# Dev-mode relenv salt-call wrapper
# This wraps the system Python to execute salt-call

exec {sys.executable} -m salt.scripts.salt_call "$@"
""")
        os.chmod(salt_call_wrapper, 0o755)

        # Create tarball - add files from inside the directory with no parent prefix
        log.info("Creating dev-mode relenv tarball: %s", tarball_path)
        with tarfile.open(tarball_path, "w:xz") as tar:
            # Change to the relenv_root directory and add files from there
            # This ensures files extract directly without a parent directory
            original_dir = os.getcwd()
            try:
                os.chdir(relenv_root)
                for item in os.listdir("."):
                    tar.add(item)
            finally:
                os.chdir(original_dir)

        log.info("Dev-mode relenv tarball created successfully: %s", tarball_path)

    return tarball_path


def gen_relenv(
    cachedir,
    kernel,
    os_arch,
    overwrite=False,
):
    """
    Deploy salt-relenv.
    :param cachedir: The cache directory where the downloaded tarball will be stored.
    :param kernel: The detected OS (e.g., 'linux', 'darwin', 'windows').
    :param os_arch: The detected architecture (e.g., 'amd64', 'x86_64', 'arm64').
    :param overwrite: Whether to overwrite the existing cached tarball.
    :return: The path to the tarball file, or False if both download and fallback fail.
    """
    # Set up directories
    relenv_dir = os.path.join(cachedir, "relenv", kernel, os_arch)
    if not os.path.isdir(relenv_dir):
        os.makedirs(relenv_dir)

    tarball_path = os.path.join(relenv_dir, "salt-relenv.tar.xz")

    # If tarball already exists and we're not overwriting, return it
    if not overwrite and os.path.exists(tarball_path):
        log.info("Using existing relenv tarball: %s", tarball_path)
        return tarball_path

    # Check for shared test cache (used by pytest session-scoped fixture)
    shared_cache = os.environ.get('SALT_SSH_TEST_RELENV_CACHE')
    if shared_cache:
        shared_tarball = os.path.join(shared_cache, "relenv", kernel, os_arch, "salt-relenv.tar.xz")
        if os.path.exists(shared_tarball):
            log.info("Copying tarball from shared test cache: %s -> %s", shared_tarball, tarball_path)
            try:
                import shutil
                shutil.copy2(shared_tarball, tarball_path)
                log.info("Successfully copied tarball from shared cache")
                return tarball_path
            except Exception as e:
                log.warning("Failed to copy from shared cache: %s", e)
                # Fall through to download

    # Download the official relenv tarball from packages.broadcom.com
    log.info("Downloading relenv tarball for %s/%s", kernel, os_arch)
    relenv_url = get_tarball(kernel, os_arch)
    log.info("Download URL: %s", relenv_url)
    log.info("Destination: %s", tarball_path)

    if not download(cachedir, relenv_url, tarball_path):
        log.error("Failed to download relenv tarball from %s", relenv_url)
        raise RuntimeError(f"Failed to download relenv tarball from {relenv_url}")

    log.info("Successfully downloaded relenv tarball: %s", tarball_path)
    return tarball_path


def get_tarball(kernel, arch):
    """
    Get the latest Salt onedir tarball URL for the specified kernel and architecture.
    :param kernel: The detected OS (e.g., 'linux', 'darwin', 'windows').
    :param arch: The detected architecture (e.g., 'amd64', 'x86_64', 'arm64').
    :return: The URL of the latest tarball.
    """
    base_url = "https://packages.broadcom.com/artifactory/saltproject-generic/onedir"
    try:
        # Request the page listing
        response = requests.get(base_url, timeout=60)
        response.raise_for_status()
    except requests.RequestException as e:
        log.error(f"Failed to retrieve tarball listing: {e}")
        raise ValueError("Unable to fetch tarball list from repository")

    latest = sorted(re.findall(r"3\d\d\d\.\d", response.text))[-1]

    try:
        # Request the page listing
        response = requests.get(f"{base_url}/{latest}", timeout=60)
        response.raise_for_status()
    except requests.RequestException as e:
        log.error(f"Failed to retrieve tarball listing: {e}")
        raise ValueError("Unable to fetch tarball list from repository")

    # Search for tarball filenames that match the kernel and arch
    pattern = re.compile(rf'href="(salt-.*-onedir-{kernel}-{arch}\.tar\.xz)"')
    matches = pattern.findall(response.text)
    if not matches:
        log.error(f"No tarballs found for {kernel} and {arch}")
        raise ValueError(f"No tarball found for {kernel} {arch}")

    # Return the latest tarball URL
    matches.sort()
    latest_tarball = matches[-1]
    return f"{base_url}/{latest}/{latest_tarball}"


def download(cachedir, url, destination):
    """
    Download the salt artifact from the given destination to the cache.
    """
    if not os.path.exists(destination):
        log.info(f"Downloading from {url} to {destination}")
        with salt.utils.files.fopen(destination, "wb+") as dest_file:
            result = salt.utils.http.query(
                url=url,
                method="GET",
                stream=True,
                streaming_callback=dest_file.write,
                raise_error=True,
                decode=False,
            )
            # When stream=True, the result dict doesn't include 'status'
            # Instead, raise_error=True will raise an exception on HTTP errors
            # So if we get here without exception, the download succeeded

        # Verify the file was actually written
        if not os.path.exists(destination) or os.path.getsize(destination) == 0:
            log.error(f"Failed to download file from {url}: file not created or empty")
            return False
    return True
