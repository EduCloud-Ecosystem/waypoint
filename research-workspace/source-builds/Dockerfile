ARG BASE_IMAGE
FROM ${BASE_IMAGE}
ARG SNAPSHOT
ARG RUNTIME
ENV LANG=C.UTF-8 LC_ALL=C.UTF-8 TZ=UTC
# The immutable Debian snapshot pins the complete binary build-tool dependency
# graph. APT still checks signatures; only archive expiry is disabled.
RUN rm -f /etc/apt/sources.list.d/debian.sources \
    && printf 'deb [check-valid-until=no] http://snapshot.debian.org/archive/debian/%s/ bookworm main\n' "$SNAPSHOT" > /etc/apt/sources.list \
    && printf 'deb [check-valid-until=no] http://snapshot.debian.org/archive/debian-security/%s/ bookworm-security main\n' "$SNAPSHOT" >> /etc/apt/sources.list \
    && apt-get -o Acquire::Retries=2 -o Acquire::http::Timeout=30 update \
    && apt-get -o Acquire::Retries=2 -o Acquire::http::Timeout=30 install -y --no-install-recommends \
       ca-certificates gcc g++ gfortran make libc6-dev pkg-config xz-utils \
       zlib1g-dev libbz2-dev liblzma-dev libpcre2-dev libcurl4-openssl-dev \
       libreadline-dev libssl-dev libffi-dev libsqlite3-dev libncurses-dev \
       libdeflate-dev libzstd-dev \
    && mkdir -p /usr/local/share/waypoint-build \
    && dpkg-query -W -f='${binary:Package}\t${Version}\t${Architecture}\n' | LC_ALL=C sort > /usr/local/share/waypoint-build/packages.tsv \
    && rm -rf /var/lib/apt/lists/*
COPY source.tar.xz build.sh inputs.json /build-inputs/
# Compilation cannot fetch hidden dependencies. No previous build layer is used
# by the acceptance runner (--no-cache); only the pinned OS base is reused.
RUN --network=none sh /build-inputs/build.sh "$RUNTIME"
WORKDIR /tmp
CMD ["/bin/sh"]
