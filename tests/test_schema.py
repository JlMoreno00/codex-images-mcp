import socket

import pytest
from jsonschema import Draft202012Validator

from codex_images_mcp import _native, _schema, _urls


def test_schema_accepts_deprecated_fields_and_describes_renamed_contract():
    Draft202012Validator.check_schema(_schema.TOOL_INPUT_SCHEMA)
    validator = Draft202012Validator(_schema.TOOL_INPUT_SCHEMA)
    assert not list(validator.iter_errors({'prompt': 'hello', **{field: 'ignored' for field in _native.DEPRECATED}}))
    assert list(validator.iter_errors({'prompt': 'hello', 'unknown': True}))
    assert 'codex-images.result/v2' in _schema.TOOL_DESCRIPTION
    assert 'codex_images_artifact' in _schema.TOOL_DESCRIPTION
    assert 'mcp://codex-images/' in _schema.TOOL_DESCRIPTION


@pytest.mark.parametrize('url', ['http://example.com/', 'https://user@example.com/', 'https://example.com:bad/'])
def test_rejects_invalid_remote_urls(url):
    with pytest.raises(_urls.RemoteURLValidationError):
        _urls.validate_public_https(url)


def test_rejects_mixed_public_and_private_dns(monkeypatch):
    monkeypatch.setattr(socket, 'getaddrinfo', lambda *a, **k: [(None, None, None, None, ('93.184.216.34', 443)), (None, None, None, None, ('127.0.0.1', 443))])
    with pytest.raises(_urls.RemoteURLValidationError):
        _urls.validate_public_https('https://example.com/')


def test_public_dns_returns_sorted_pinned_addresses(monkeypatch):
    monkeypatch.setattr(socket, 'getaddrinfo', lambda *a, **k: [(None, None, None, None, ('93.184.216.34', 443))])
    assert _urls.validate_public_https('https://example.com/') == ('93.184.216.34',)
