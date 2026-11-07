import base64
import json
from io import BytesIO
from email.message import Message
from http.client import HTTPMessage
from urllib.error import HTTPError
from urllib.request import Request
from typing import Any

import pytest
from PIL import Image

from codex_images_mcp import _native as engine


def png(alpha=0):
    buf = BytesIO()
    Image.new('RGBA', (16, 16), (1, 2, 3, alpha)).save(buf, format='PNG')
    return buf.getvalue()


class Response(BytesIO):
    pass


def fake(monkeypatch, calls, alpha=0):
    monkeypatch.setattr(engine, 'oauth_headers', lambda _: {'Authorization': 'Bearer fake', 'OpenAI-Beta': 'remove'})
    def opener(request, timeout):
        calls.append((request, timeout))
        return Response(json.dumps({'data': [{'b64_json': base64.b64encode(png(alpha)).decode()}]}).encode())
    monkeypatch.setattr(engine, 'urlopen', opener)


def test_generate_verbatim_and_headers(tmp_path, monkeypatch):
    calls = []
    fake(monkeypatch, calls)
    result = engine.generate({'prompt': '  verbatim {x}\n', 'output_dir': str(tmp_path)})
    assert result['ok']
    request, timeout = calls[0]
    assert request.full_url == engine.BASE + '/generations'
    assert json.loads(request.data) == {'prompt': '  verbatim {x}\n', 'background': 'opaque', 'model': 'gpt-image-2', 'quality': 'auto', 'size': 'auto'}
    assert timeout == 280
    assert 'Openai-beta' not in request.headers
    assert request.headers['User-agent'].startswith('codex_cli_rs/')
    assert result['models'] == {'dispatched': 'gpt-image-2', 'provider': None, 'confirmed': False}


def test_edit_mask_transparency(tmp_path, monkeypatch):
    calls = []
    fake(monkeypatch, calls)
    ref = tmp_path / 'ref.png'
    ref.write_bytes(png())
    result = engine.generate({'prompt': 'edit', 'references': [str(ref)], 'mask': str(ref), 'transparent': True, 'size': '1000x1000', 'output_dir': str(tmp_path)})
    assert result['ok']
    request, _ = calls[0]
    body = json.loads(request.data)
    assert request.full_url.endswith('/edits')
    assert body['images'] == [{'image_url': 'data:image/png;base64,' + base64.b64encode(png()).decode()}]
    assert body['mask'] == body['images'][0]
    assert body['background'] == 'transparent'
    assert body['size'] == '992x992'


def test_dict_and_variants(tmp_path, monkeypatch):
    calls = []
    fake(monkeypatch, calls)
    result = engine.generate({'prompt': {'avoid': 'blur', 'subject': 'cat'}, 'batch_mode': 'variants', 'count': 3, 'output_dir': str(tmp_path)})
    assert len(result['images']) == len(calls) == 3
    assert json.loads(calls[0][0].data)['prompt'] == 'Subject: cat\nAvoid: blur'


@pytest.mark.parametrize('status,html,code,retry,calls_expected', [(401,False,'UPSTREAM_AUTH_FAILED',False,1),(403,False,'UPSTREAM_AUTH_FAILED',False,1),(403,True,'UPSTREAM_GENERATION_FAILED',False,1),(429,False,'UPSTREAM_GENERATION_FAILED',True,2),(503,False,'UPSTREAM_GENERATION_FAILED',True,2)])
def test_errors(tmp_path, monkeypatch, status, html, code, retry, calls_expected):
    calls = []
    fake(monkeypatch, calls)
    def fail(request, timeout):
        calls.append(request)
        headers = Message()
        headers['Content-Type'] = 'text/html' if html else 'application/json'
        raise HTTPError(request.full_url, status, 'secret must not leak', headers, BytesIO(b'<html>Cloudflare</html>' if html else b'{"error":"secret"}'))
    monkeypatch.setattr(engine, 'urlopen', fail)
    result = engine.generate({'prompt': 'a', 'output_dir': str(tmp_path)})
    assert len(calls) == calls_expected
    error = result['errors'][0]
    assert (error['code'], error['retryable']) == (code, retry)
    assert 'secret' not in str(result)


def test_anchor_chain_and_partial(tmp_path, monkeypatch):
    calls = []
    fake(monkeypatch, calls)
    result = engine.generate({'prompt': ['a','b'], 'anchor': 'anchor', 'batch_mode': 'chain', 'output_dir': str(tmp_path)})
    assert len(result['images']) == 2
    bodies = [json.loads(req.data) for req, _ in calls]
    assert [b['prompt'] for b in bodies] == ['anchor','a','b']
    assert len(bodies[1]['images']) == 1
    assert len(bodies[2]['images']) == 2


def test_alpha_warning_and_reference_limit(tmp_path, monkeypatch):
    calls = []
    fake(monkeypatch, calls, alpha=255)
    result = engine.generate({'prompt': 'a', 'transparent': True, 'output_dir': str(tmp_path)})
    assert any('opaque' in w for w in result['warnings'])
    calls.clear()
    result = engine.generate({'prompt': 'a', 'references': ['unused'] * 6, 'output_dir': str(tmp_path)})
    assert result['errors'][0]['code'] == 'TOO_MANY_REFERENCES'
    assert not calls


@pytest.fixture(autouse=True)
def no_live_network(monkeypatch):
    def forbidden(*args, **kwargs):
        raise AssertionError('Tests must never call a live endpoint')
    monkeypatch.setattr(engine, 'urlopen', forbidden)
    monkeypatch.setattr(engine, 'oauth_headers', forbidden)


def test_timeout_retry_then_success(tmp_path, monkeypatch):
    calls = []
    fake(monkeypatch, calls)
    success = engine.urlopen
    attempts = []
    def flaky(request, timeout):
        attempts.append(1)
        if len(attempts) == 1:
            raise TimeoutError()
        return success(request, timeout)
    monkeypatch.setattr(engine, 'urlopen', flaky)
    assert engine.generate({'prompt': 'a', 'output_dir': str(tmp_path)})['ok']
    assert len(attempts) == 2


def test_remote_reference_verified_and_bounded(monkeypatch):
    validated, reads = [], []
    def validate(url):
        validated.append(url)
        return ('93.184.216.34',)
    monkeypatch.setattr(engine, 'validate_public_https', validate)
    class Download(Response):
        status = 200
        def read(self, limit: int | None = -1):
            reads.append(limit)
            return super().read(limit)
    class Connection:
        def __init__(self, host, port, address, timeout):
            assert (host, port, address, timeout) == ('example.com', 443, '93.184.216.34', 30)
        def request(self, method, target, headers):
            assert method == 'GET' and headers['Host'] == 'example.com'
        def getresponse(self): return Download(png())
        def close(self): pass
    monkeypatch.setattr(engine, 'PinnedHTTPSConnection', Connection)
    assert engine.data_url('https://example.com/ref')['image_url'].startswith('data:image/png;base64,')
    assert validated == ['https://example.com/ref']
    assert reads == [engine.MAX_INPUT_BYTES + 1]
    monkeypatch.setattr(engine, 'MAX_INPUT_BYTES', 1)
    with pytest.raises(engine.EngineError) as error:
        engine.data_url('https://example.com/ref')
    assert error.value.code == 'INPUT_MATERIALIZATION_FAILED'


def test_parallel_is_bounded_and_branded_anchor_first(tmp_path, monkeypatch):
    from threading import Barrier, Lock
    calls = []
    fake(monkeypatch, calls)
    success = engine.urlopen
    barrier, lock = Barrier(4), Lock()
    active = 0
    peak = 0
    def concurrent(request, timeout):
        nonlocal active, peak
        body = json.loads(request.data)
        if body['prompt'] == 'anchor':
            assert not calls
            return success(request, timeout)
        assert body['images']
        with lock:
            active += 1
            peak = max(peak, active)
        barrier.wait(timeout=5)
        result = success(request, timeout)
        with lock:
            active -= 1
        return result
    monkeypatch.setattr(engine, 'urlopen', concurrent)
    result = engine.generate({'prompt': ['a'] * 8, 'anchor': 'anchor', 'batch_mode': 'branded-parallel', 'output_dir': str(tmp_path)})
    assert result['ok']
    assert len(result['images']) == 8
    assert peak == 4


def test_reference_mime_invalid_and_redirect_rejected(tmp_path):
    path = tmp_path / 'bad.png'
    path.write_bytes(b'not an image')
    with pytest.raises(engine.EngineError) as error:
        engine.data_url(str(path))
    assert error.value.code == 'UNSUPPORTED_MIME_TYPE'
    with pytest.raises(engine.EngineError) as error:
        engine.NoRedirect().redirect_request(Request('https://example.com/ref'), BytesIO(), 302, '', HTTPMessage(), 'https://127.0.0.1/ref')
    assert error.value.code == 'REMOTE_DOWNLOAD_FAILED'

"""Model reporting now describes dispatch, never an inferred provider engine."""
from codex_images_mcp._native import render_prompt, size_hint

@pytest.mark.parametrize('size', ['8192x8192','8192x1','1x8192','1024x1024','3840x2160','1000x333'])
def test_size_hint_limits(size):
    width, height = map(int, size_hint(size).split('x'))
    assert width % 16 == height % 16 == 0
    assert width <= 3840 and height <= 2160
    assert 1/3 <= width/height <= 3

def test_dict_unknown_keys_are_sorted_and_nested_values_stable():
    assert render_prompt({'z': {'b': 2, 'a': 1}, 'subject': 'cat', 'a': 'x'}) == 'Subject: cat\na: x\nz: {"a":1,"b":2}'
