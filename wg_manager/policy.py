from __future__ import annotations

import ipaddress


MAX_CLIENT_ROUTES = 32


def normalize_client_allowed_ips(value: str) -> str:
    """Return a stable, validated comma-separated IPv4 route list."""
    parts = [part.strip() for part in value.replace("\n", ",").split(",") if part.strip()]
    if not parts:
        raise ValueError("at least one client AllowedIPs route is required")
    if len(parts) > MAX_CLIENT_ROUTES:
        raise ValueError(f"at most {MAX_CLIENT_ROUTES} client routes are allowed")

    networks: set[ipaddress.IPv4Network] = set()
    for part in parts:
        try:
            network = ipaddress.ip_network(part, strict=True)
        except ValueError as error:
            raise ValueError(f"invalid network CIDR: {part}") from error
        if network.version != 4:
            raise ValueError("only IPv4 client routes are supported")
        networks.add(network)

    # Remove routes already covered by an explicitly larger route, but do not
    # merge adjacent routes. In particular, two /1 routes must not silently
    # become /0 because Windows treats an exact full-tunnel route specially.
    normalized = sorted(
        (
            network
            for network in networks
            if not any(network != other and network.subnet_of(other) for other in networks)
        ),
        key=lambda network: (int(network.network_address), network.prefixlen),
    )
    return ", ".join(str(network) for network in normalized)


def split_allowed_ips(value: str) -> tuple[str, ...]:
    return tuple(part.strip() for part in value.split(",") if part.strip())
