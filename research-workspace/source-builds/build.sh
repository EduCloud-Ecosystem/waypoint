#!/bin/sh
set -eu
mkdir /build-source
tar -xJf /build-inputs/source.tar.xz -C /build-source --strip-components=1
cd /build-source
case "$1" in
  python)
    ./configure --prefix=/usr/local --without-ensurepip > /usr/local/share/waypoint-build/configure.log 2>&1
    make -j2
    make install
    ln -s python3 /usr/local/bin/python
    python -c 'import csv,json,sys; print(sys.version)'
    ;;
  r)
    ./configure --prefix=/usr/local --without-x --without-recommended-packages --with-readline=no --with-tcltk=no --with-blas=no --with-lapack=no > /usr/local/share/waypoint-build/configure.log 2>&1
    make -j2
    make install
    Rscript --vanilla -e 'stopifnot(requireNamespace("base"),requireNamespace("utils")); cat(as.character(getRversion()),"\n")'
    ;;
  *) exit 2 ;;
esac
cp /build-inputs/inputs.json /usr/local/share/waypoint-build/inputs.json
cp config.log /usr/local/share/waypoint-build/config.log
cd /
rm -rf /build-source /build-inputs
