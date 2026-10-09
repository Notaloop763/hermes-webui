# Python 3.14 from the same python-build-standalone release the Hermes Agent
# image pins in pm/lock.json (hermes-agent v0.21.6: CPython 3.14.7+20260901), so
# a Mnemosyne side venv created here uses the same interpreter build as the
# agent container. The WebUI project supports Python 3.11-3.14 (pyproject
# requires-python >=3.11; scripts/test.sh probes python3.14 first).
FROM debian:13.4

LABEL maintainer="nesquena"
LABEL description="Hermes Web UI — browser interface for Hermes Agent"

# Install system packages
ENV DEBIAN_FRONTEND=noninteractive

# Make use of apt-cacher-ng if available
RUN if [ "A${BUILD_APT_PROXY:-}" != "A" ]; then \
        echo "Using APT proxy: ${BUILD_APT_PROXY}"; \
        printf 'Acquire::http::Proxy "%s";\n' "$BUILD_APT_PROXY" > /etc/apt/apt.conf.d/01proxy; \
    fi \
    && apt-get update \
    && apt-get install -y --no-install-recommends ca-certificates wget gnupg \
    && rm -rf /var/lib/apt/lists/* \
    && apt-get clean

RUN apt-get update -y --fix-missing --no-install-recommends \
    && apt-get install -y --no-install-recommends \
    apt-utils \
    locales \
    ca-certificates \
    curl \
    rsync \
    openssh-client \
    git \
    xz-utils \
    python3 \
    python3-dev \
    python3-venv \
    && apt-get upgrade -y \
    && apt-get clean \
    && rm -rf /var/lib/apt/lists/*

# ── Python 3.14 (python-build-standalone) ───────────────────────────────────
# Same artifacts and sha256 digests as hermes-agent pm/lock.json at v0.21.6.
# Installed at the Agent's managed-store path (pm store_entry is
# "<name>-<version>-<target>" under HERMES_RUNTIME_DIR=/opt/hermes/tools) so a
# shared Mnemosyne venv records a base interpreter both containers can execute.
# uv canonicalizes symlinked standalone Pythons, so this is the real directory;
# /opt/python is only a convenience link. When the agent pin moves, bump these
# ARGs together from its pm/lock.json.
ARG PYTHON_VERSION=3.14.7
ARG PYTHON_PBS_RELEASE=20260901
ARG PYTHON_SHA256_X64=0ab3305457051cd3e7c031857e005f1bda17c218a1990567dacaaac6dd1d14f0
ARG PYTHON_SHA256_ARM64=30f1cc489be654477d895b441e196bb080738bf0456da82080ad4ab66a22d80f
ARG PYTHON_STORE=/opt/hermes/tools
ARG TARGETARCH
COPY --chmod=444 docker/python/sitecustomize.py /usr/local/share/hermes/sitecustomize.py
RUN case "${TARGETARCH:-$(dpkg --print-architecture)}" in \
        amd64) triple=x86_64-unknown-linux-gnu; pmt=linux-x64; sha="${PYTHON_SHA256_X64}" ;; \
        arm64) triple=aarch64-unknown-linux-gnu; pmt=linux-arm64; sha="${PYTHON_SHA256_ARM64}" ;; \
        *) echo "unsupported architecture: ${TARGETARCH}" >&2; exit 1 ;; \
    esac \
    && home="${PYTHON_STORE}/python-${PYTHON_VERSION}+${PYTHON_PBS_RELEASE}-${pmt}" \
    && curl -fsSL "https://github.com/astral-sh/python-build-standalone/releases/download/${PYTHON_PBS_RELEASE}/cpython-${PYTHON_VERSION}+${PYTHON_PBS_RELEASE}-${triple}-install_only.tar.gz" -o /tmp/python.tar.gz \
    && echo "${sha}  /tmp/python.tar.gz" | sha256sum -c - \
    && mkdir -p "${home}" \
    && tar -xzf /tmp/python.tar.gz -C "${home}" --strip-components=1 \
    && rm -f /tmp/python.tar.gz \
    && ln -s "${home}" /opt/python \
    && install -m 0444 /usr/local/share/hermes/sitecustomize.py "${home}/lib/python3.14/sitecustomize.py" \
    && ln -s "${home}/bin/python3" /usr/local/bin/python3 \
    && ln -s "${home}/bin/python3" /usr/local/bin/python3.14 \
    && ln -s "${home}/bin/python3" /usr/local/bin/python \
    && python3 -c "import sys; assert sys.version_info[:2] == (3, 14), sys.version"

# ── SQLite upgrade ──────────────────────────────────────────────────────────
# The Debian 13 base ships SQLite 3.46.1 (Trixie), which is
# vulnerable to the WAL-reset corruption bug discovered March 2026.
# https://sqlite.org/wal.html#walresetbug
#
# Debian has not backported the fix, so we compile from the amalgamation.
# Installs to /usr/local/lib (registered in ld.so.conf.d for arm64 priority).
# Build tools are purged after compilation to keep the image lean.
# Build args are for forward version bumps only (3.54+, etc.).
# When bumping SQLITE_VERSION, recompute the SHA-256 from the official
# download and update SQLITE_SHA256 accordingly.
ARG SQLITE_VERSION=3530000
ARG SQLITE_YEAR=2026
ARG SQLITE_SHA256=851e9b38192fe2ceaa65e0baa665e7fa06230c3d9bd1a6a9662d02380d73365a
RUN apt-get update \
    && apt-get install -y --no-install-recommends gcc make libc6-dev \
    && cd /tmp \
    && curl -fsSL "https://sqlite.org/${SQLITE_YEAR}/sqlite-autoconf-${SQLITE_VERSION}.tar.gz" \
       -o sqlite.tar.gz \
    && echo "${SQLITE_SHA256}  sqlite.tar.gz" | sha256sum -c - \
    && tar xzf sqlite.tar.gz \
    && cd "sqlite-autoconf-${SQLITE_VERSION}" \
    && CPPFLAGS="-DSQLITE_SECURE_DELETE" ./configure --prefix=/usr/local --disable-static --disable-readline \
       --enable-fts5 --enable-fts4 --enable-rtree \
    && make -j"$(nproc)" \
    && make install \
    && echo "/usr/local/lib" > /etc/ld.so.conf.d/000-usr-local-lib.conf \
    && /sbin/ldconfig \
    && cd / && rm -rf /tmp/sqlite* \
    && apt-get purge -y gcc make libc6-dev \
    && apt-get autoremove -y \
    && apt-get clean && rm -rf /var/lib/apt/lists/* \
    && python3 -c "\
import sqlite3; \
v = sqlite3.sqlite_version; \
assert tuple(int(x) for x in v.split('.')) >= (3, 51, 3), \
    f'SQLite {v} still vulnerable'; \
c = sqlite3.connect(':memory:'); \
assert c.execute('PRAGMA secure_delete').fetchone()[0] == 1, \
    'SQLITE_SECURE_DELETE not compiled in (deleted rows would remain recoverable)'; \
c.execute('CREATE VIRTUAL TABLE _fts5_build_check USING fts5(x)'); \
c.execute('DROP TABLE _fts5_build_check'); \
c.close()" \
    && python3 -I -c "\
import ctypes, sys, _sqlite3, sqlite3; \
lib = ctypes.CDLL('/usr/local/lib/libsqlite3.so.0'); \
lib.sqlite3_compileoption_used.argtypes = [ctypes.c_char_p]; \
assert lib.sqlite3_compileoption_used(b'SECURE_DELETE') == 1, \
    'built libsqlite3 lacks SECURE_DELETE'; \
assert '_sqlite3' in sys.builtin_module_names, \
    'PBS layout changed (_sqlite3 no longer builtin): re-evaluate docker/python/sitecustomize.py'; \
assert _sqlite3.connect(':memory:').execute('PRAGMA secure_delete').fetchone()[0] == 0, \
    'PBS now compiles SECURE_DELETE: sitecustomize shim is redundant'; \
assert sqlite3.connect._hermes_secure_delete, 'sitecustomize shim not active'"

# Optional GPU user-space acceleration libraries for users who pass through
# host GPU devices. The default image remains CPU-only.
ARG INSTALL_GPU_LIBS=0
RUN if [ "$INSTALL_GPU_LIBS" = "1" ]; then \
        apt-get update -y --fix-missing --no-install-recommends \
        && apt-get install -y --no-install-recommends \
            libva2 \
            vainfo \
            mesa-va-drivers \
        && if apt-cache show intel-media-va-driver-non-free >/dev/null 2>&1; then \
            apt-get install -y --no-install-recommends intel-media-va-driver-non-free; \
        else \
            echo "intel-media-va-driver-non-free is not available from the configured Debian repositories; skipping Intel non-free VA-API driver."; \
        fi \
        && apt-get clean \
        && rm -rf /var/lib/apt/lists/*; \
    else \
        echo "Skipping optional GPU user-space acceleration libraries (INSTALL_GPU_LIBS=0)."; \
    fi

# UTF-8
RUN localedef -i en_US -c -f UTF-8 -A /usr/share/locale/locale.alias en_US.UTF-8
ENV LANG=en_US.utf8
ENV LC_ALL=C

# Set environment variables
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PYTHONIOENCODING=utf-8

WORKDIR /apptoo

# Create the unprivileged runtime user. The entrypoint starts as root only for
# UID/GID alignment and filesystem preparation, then execs the server as this user.
RUN groupadd -g 1024 hermeswebui \
    && useradd -u 1024 -d /home/hermeswebui -g hermeswebui -G users -s /bin/bash -m hermeswebui \
    && mkdir -p /app /uv_cache /workspace \
    && chown -R hermeswebui:hermeswebui /home/hermeswebui /app /uv_cache /workspace \
    && chmod 0755 /home/hermeswebui \
    && chmod 1777 /app /uv_cache /workspace

COPY --chmod=555 docker_init.bash /hermeswebui_init.bash

RUN touch /.within_container

# Remove APT proxy configuration and clean up APT downloaded files
RUN rm -rf /var/lib/apt/lists/* /etc/apt/apt.conf.d/01proxy \
    && apt-get clean

USER root

# Pre-install uv system-wide so the container doesn't need internet access at runtime.
# Installing as root places uv in /usr/local/bin, available to all users.
# The init script will skip the download when uv is already on PATH.
RUN curl -LsSf https://astral.sh/uv/install.sh | env UV_INSTALL_DIR=/usr/local/bin sh

# Prove the SQLite secure_delete property and interpreter path inside a real uv
# venv, the shape the app and a shared Mnemosyne venv use.
RUN d="$(mktemp -d)" \
    && uv venv -q --python /usr/local/bin/python3 "$d/v" \
    && grep -qE "^home = /opt/hermes/tools/python-.*/bin$" "$d/v/pyvenv.cfg" \
    && "$d/v/bin/python" -I -c "import sqlite3; assert sqlite3.connect(':memory:').execute('PRAGMA secure_delete').fetchone()[0] == 1" \
    && rm -rf "$d"

COPY --chown=root:root . /apptoo

# Bake the git version tag into the image so the settings badge works even
# when .git is not present (it is excluded by .dockerignore).
# CI passes: --build-arg HERMES_VERSION=$(git describe --tags --always)
# Local builds that omit the arg get "unknown" as the fallback.
ARG HERMES_VERSION=unknown
RUN echo "__version__ = '${HERMES_VERSION}'" > /apptoo/api/_version.py

# Default to binding all interfaces (required for container networking)
ENV HERMES_WEBUI_HOST=0.0.0.0
ENV HERMES_WEBUI_PORT=8787

EXPOSE 8787

HEALTHCHECK --interval=30s --timeout=8s --start-period=10s --retries=3 \
  CMD bash /apptoo/scripts/lib/health_probe.sh localhost 8787 /health 2 >/dev/null || exit 1

# docker_init.bash performs root-only bind-mount setup, then drops to hermeswebui
# before starting the WebUI server. The production image does not ship sudo.
USER root
CMD ["/hermeswebui_init.bash"]

