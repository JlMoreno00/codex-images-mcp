"""Resolve remote image URLs only to globally routable addresses."""
from __future__ import annotations

import ipaddress
import socket
from urllib.parse import urlparse


class RemoteURLValidationError(Exception):
    code = 'REMOTE_DOWNLOAD_FAILED'

    def __init__(self, message: str, retryable: bool = False):
        super().__init__(message)
        self.retryable = retryable


def validate_public_https(url: str) -> tuple[str, ...]:
    try:
        parsed = urlparse(url)
        if parsed.scheme != 'https' or not parsed.hostname or parsed.username or parsed.password:
            raise RemoteURLValidationError('Remote inputs must use an HTTPS URL without embedded credentials.')
        port = parsed.port or 443
    except ValueError as exc:
        raise RemoteURLValidationError('Remote input URL is malformed.') from exc
    try:
        addresses = {str(item[4][0]) for item in socket.getaddrinfo(parsed.hostname, port, type=socket.SOCK_STREAM)}
    except socket.gaierror as exc:
        raise RemoteURLValidationError('Remote input hostname could not be resolved.', True) from exc
    if not addresses:
        raise RemoteURLValidationError('Remote input hostname resolved to no addresses.')
    for address in addresses:
        try:
            if not ipaddress.ip_address(address).is_global:
                raise RemoteURLValidationError('Remote input resolves to a private or reserved network.')
        except ValueError as exc:
            raise RemoteURLValidationError('Remote input resolved to an invalid address.') from exc
    return tuple(sorted(addresses))
