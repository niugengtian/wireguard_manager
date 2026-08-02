ARG PYTHON_IMAGE=python:3.13.14-slim-bookworm
FROM ${PYTHON_IMAGE}

ARG WG_MANAGER_VERSION=0.3.0
ARG SOURCE_REVISION=local

LABEL org.opencontainers.image.title="WireGuard Manager" \
      org.opencontainers.image.description="Non-root Web/CLI and least-privilege WireGuard reconciler roles" \
      org.opencontainers.image.version="${WG_MANAGER_VERSION}" \
      org.opencontainers.image.revision="${SOURCE_REVISION}" \
      org.opencontainers.image.source="https://github.com/niugengtian/wireguard_manager"

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    WG_MANAGER_UID=10001 \
    WG_MANAGER_GID=10001 \
    WG_MANAGER_DATA_DIR=/var/lib/wireguard-manager \
    WG_RECONCILE_STATE_DIR=/var/lib/wireguard-manager-reconciler \
    WG_RECONCILE_RUNTIME_DIR=/run/wireguard-manager \
    WG_RECONCILE_SOCKET=/run/wireguard-manager/reconcile.sock

RUN apt-get update \
    && apt-get install -y --no-install-recommends ca-certificates wireguard-tools \
    && rm -rf /var/lib/apt/lists/* \
    && groupadd --gid 10001 wireguard-manager \
    && useradd --uid 10001 --gid 10001 --home-dir /var/lib/wireguard-manager \
       --no-create-home --shell /usr/sbin/nologin wireguard-manager \
    && install -d -o 10001 -g 10001 -m 0700 /var/lib/wireguard-manager \
    && install -d -o root -g 10001 -m 0750 \
       /var/lib/wireguard-manager-reconciler /run/wireguard-manager

WORKDIR /opt/wireguard-manager
COPY pyproject.toml README.md ./
COPY wg_manager ./wg_manager
COPY docker/constraints.txt /tmp/wg-manager-constraints.txt

RUN python -m pip install --no-cache-dir pip==26.2 setuptools==80.9.0 \
    && python -m pip install --no-cache-dir --no-build-isolation \
       --constraint /tmp/wg-manager-constraints.txt . \
    && test "$(wg-manager --version)" = "wg-manager ${WG_MANAGER_VERSION}" \
    && rm -f /tmp/wg-manager-constraints.txt

COPY --chmod=0555 docker/entrypoint.py /usr/local/bin/wg-manager-container

EXPOSE 8081/tcp
STOPSIGNAL SIGTERM
USER 10001:10001
ENTRYPOINT ["/usr/local/bin/wg-manager-container"]
CMD ["manager"]
