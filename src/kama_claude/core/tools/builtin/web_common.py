from __future__ import annotations

import asyncio
import ipaddress
import socket
from collections.abc import Awaitable, Callable
from urllib.parse import SplitResult, urlsplit

Resolver = Callable[[str, int], Awaitable[list[str]]]


class UnsafeUrlError(ValueError):
    """Raised when a model-provided URL could reach a non-public network target."""


def _is_public_ip(value: str) -> bool:
    address = ipaddress.ip_address(value)
    return not (
        address.is_private
        or address.is_loopback
        or address.is_link_local
        or address.is_multicast
        or address.is_reserved
        or address.is_unspecified
    )


async def resolve_host(host: str, port: int) -> list[str]:
    loop = asyncio.get_running_loop()
    records = await loop.getaddrinfo(host, port, type=socket.SOCK_STREAM)
    return sorted({str(record[4][0]) for record in records})


async def validate_public_url(url: str, resolver: Resolver = resolve_host) -> SplitResult:
    """Validate scheme, credentials and every resolved address before an HTTP request."""
    try:
        parsed = urlsplit(url)
        port = parsed.port
    except ValueError as exc:
        raise UnsafeUrlError(f"Invalid URL: {exc}") from exc

    if parsed.scheme not in {"http", "https"}:
        raise UnsafeUrlError("Only http:// and https:// URLs are allowed")
    if not parsed.hostname:
        raise UnsafeUrlError("URL must include a hostname")
    if parsed.username is not None or parsed.password is not None:
        raise UnsafeUrlError("URLs containing credentials are not allowed")

    host = parsed.hostname.rstrip(".").lower()
    if host == "localhost" or host.endswith((".localhost", ".local")):
        raise UnsafeUrlError(f"Local network host is not allowed: {host}")

    try:
        literal = ipaddress.ip_address(host)
    except ValueError:
        addresses = await resolver(host, port or (443 if parsed.scheme == "https" else 80))
        if not addresses:
            raise UnsafeUrlError(f"Hostname did not resolve: {host}")
    else:
        addresses = [str(literal)]

    blocked = [address for address in addresses if not _is_public_ip(address)]
    if blocked:
        raise UnsafeUrlError(
            f"URL resolves to a non-public address and was blocked: {', '.join(blocked)}"
        )
    return parsed
