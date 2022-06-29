#!/bin/sh
#
#
# Script for making a onedir build.
#
# Usage:
#
# docker run --name onedir centos:7 /bin/bash -c "$(cat onedir.sh)"
# docker cp onedir:salt/salt.tar.xz .
#
#


set -e
yum install epel-release -y
yum --disablerepo="*" --enablerepo="epel" list available
yum install yum-utils -y
yum repolist
yum makecache
yum groupinstall "Development Tools" -y
yum-builddep python3 -y
yum install -y zlib-devel
yum install patchelf -y
git clone -b example --depth=1 https://github.com/dwoz/salt.git
cd salt
make salt.tar.xz
