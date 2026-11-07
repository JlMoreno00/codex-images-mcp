"""Native subscription image engine. No prompt rewriting or chat-model calls."""
from __future__ import annotations

import base64
import json
import os
import re
import socket
import ssl
from dataclasses import dataclass
from http.client import HTTPSConnection, HTTPException
from urllib.parse import urlsplit
from concurrent.futures import ThreadPoolExecutor
from io import BytesIO
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.request import HTTPRedirectHandler, ProxyHandler, Request, build_opener

from PIL import Image
from ._auth import oauth_headers
from ._urls import validate_public_https

BASE = 'https://chatgpt.com/backend-api/codex/images'
API_BASE = 'https://api.openai.com/v1/images'
DEFAULT_API_MODEL = 'gpt-image-2.6-flare'
QUALITIES = ('auto', 'low', 'medium', 'high', 'xhigh', 'max')
COMPLEXITIES = ('low', 'medium', 'high')
API_OPTIONS = ('model', 'quality', 'complexity')
MODEL_RE = re.compile(r'gpt-image-[a-z0-9][a-z0-9.-]{0,63}')
MAX_INPUT_BYTES = int(os.environ.get('CODEX_IMAGES_MAX_INLINE_INPUT_BYTES', str(8 * 1024 * 1024)))
DEPRECATED = ('mode', 'skills', 'instruction_files', 'extra_instructions', 'chroma_key', 'chroma_tolerance', 'image_model', 'output_dir', 'chroma_despill', 'chroma_despill_mode', 'chroma_edge_erode_px', 'chroma_auto_key', 'chroma_transparent_threshold', 'chroma_opaque_threshold', 'enhance_prompt', 'vars', 'parallel', 'wall_clock_timeout', 'advanced')
FIELDS = ('use_case', 'asset_type', 'primary_request', 'input_images', 'scene', 'subject', 'style_medium', 'composition', 'lighting_mood', 'palette', 'materials', 'text_verbatim', 'constraints', 'avoid')
LABELS = ('Use case', 'Asset type', 'Primary request', 'Input images', 'Scene/backdrop', 'Subject', 'Style/medium', 'Composition/framing', 'Lighting/mood', 'Color palette', 'Materials/textures', 'Text (verbatim)', 'Constraints', 'Avoid')


class EngineError(Exception):
    def __init__(self, code, message, retryable=False):
        super().__init__(message)
        self.code, self.retryable = code, retryable

    def record(self, index):
        return {'code': self.code, 'message': str(self), 'layer': 'provider', 'retryable': self.retryable, 'input_index': index}


def render_prompt(prompt):
    if isinstance(prompt, str):
        return prompt
    if not isinstance(prompt, dict):
        raise EngineError('INVALID_ARGUMENT', 'Prompt must be a string or labeled-spec object.')
    def value(item):
        return item if isinstance(item, str) else json.dumps(item, ensure_ascii=False, sort_keys=True, separators=(',', ':'))
    labels: dict[str, str] = dict(zip(FIELDS, LABELS))
    keys = [key for key in FIELDS if key in prompt] + sorted(set(prompt) - set(FIELDS))
    return '\n'.join(f'{labels.get(key, key)}: {value(prompt[key])}' for key in keys)


def size_hint(size):
    try:
        width, height = map(int, size.split('x'))
        if min(width, height) <= 0:
            return 'auto'
        ratio = max(1 / 3, min(3, width / height))
        width = min(width, height * ratio)
        height = width / ratio
        scale = min(1, 3840 / width, 2160 / height)
        width = max(16, int(width * scale // 16) * 16)
        height = max(16, int(height * scale // 16) * 16)
        # Rounding must not move an extreme ratio outside Codex's bounds.
        width = min(width, height * 3)
        height = min(height, width * 3)
        return f'{width}x{height}'
    except (ValueError, AttributeError, TypeError):
        return 'auto'


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise EngineError('REMOTE_DOWNLOAD_FAILED', 'Reference redirects are not allowed.')


@dataclass(frozen=True)
class RemoteImage:
    url: str
    address: str


class PinnedHTTPSConnection(HTTPSConnection):
    """Connect only to the checked numeric IP; authenticate the original host."""
    def __init__(self, host, port, address, *, timeout=30):
        self.tls_context = ssl.create_default_context()
        super().__init__(host, port, timeout=timeout, context=self.tls_context)
        self.address = address

    def connect(self):
        sock = socket.create_connection((self.address, self.port), self.timeout)
        try:
            self.sock = self.tls_context.wrap_socket(sock, server_hostname=self.host)
        except Exception:
            sock.close()
            raise


def download_reference(source: RemoteImage):
    parsed = urlsplit(source.url)
    connection = PinnedHTTPSConnection(parsed.hostname, parsed.port or 443, source.address, timeout=30)
    host = parsed.hostname or ''
    if ':' in host:
        host = f'[{host}]'
    if parsed.port and parsed.port != 443:
        host += f':{parsed.port}'
    target = parsed.path or '/'
    if parsed.query:
        target += '?' + parsed.query
    try:
        # http.client does not read environment proxies or follow redirects.
        connection.request('GET', target, headers={'Host': host, 'Connection': 'close'})
        with connection.getresponse() as response:
            if not 200 <= response.status < 300:
                raise EngineError('REMOTE_DOWNLOAD_FAILED', 'Reference download returned a non-success status.')
            return response.read(MAX_INPUT_BYTES + 1)
    except (OSError, HTTPException) as exc:
        raise EngineError('REMOTE_DOWNLOAD_FAILED', 'Reference download failed.', True) from exc
    finally:
        connection.close()


class NoUpstreamRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise EngineError('UPSTREAM_GENERATION_FAILED', 'Codex Images redirects are not allowed.', False)


def urlopen(request, timeout):
    # Dedicated opener: no shared/global redirect policy or environment proxies.
    return build_opener(ProxyHandler({}), NoUpstreamRedirect()).open(request, timeout=timeout)


def data_url(source):
    if isinstance(source, str) and source.startswith('https://'):
        source = RemoteImage(source, validate_public_https(source)[0])
    if isinstance(source, RemoteImage):
        data = download_reference(source)
    else:
        path = Path(source)
        if path.stat().st_size > MAX_INPUT_BYTES:
            raise EngineError('INPUT_MATERIALIZATION_FAILED', 'Reference exceeds the per-file size limit.')
        data = path.read_bytes()
    if len(data) > MAX_INPUT_BYTES:
        raise EngineError('INPUT_MATERIALIZATION_FAILED', 'Reference exceeds the per-file size limit.')
    try:
        with Image.open(BytesIO(data)) as image:
            mime = {'PNG': 'image/png', 'JPEG': 'image/jpeg', 'WEBP': 'image/webp'}.get(image.format or '')
            image.verify()
        if mime is None:
            raise ValueError('unsupported image')
    except (OSError, ValueError) as exc:
        raise EngineError('UNSUPPORTED_MIME_TYPE', 'Reference must be a valid PNG, JPEG, or WebP image.') from exc
    return {'image_url': f'data:{mime};base64,' + base64.b64encode(data).decode('ascii')}


def engine_config(args) -> dict[str, Any]:
    """Resolve the image engine chosen by the server owner (never by the client)."""
    name = os.environ.get('CODEX_IMAGES_ENGINE', 'chatgpt').strip().lower() or 'chatgpt'
    if name not in ('chatgpt', 'api'):
        raise EngineError('INVALID_ARGUMENT', 'CODEX_IMAGES_ENGINE must be "chatgpt" or "api".')
    if args.get('quality') is not None and args['quality'] not in QUALITIES:
        raise EngineError('INVALID_ARGUMENT', f'quality must be one of {", ".join(QUALITIES)}.')
    if args.get('complexity') is not None and args['complexity'] not in COMPLEXITIES:
        raise EngineError('INVALID_ARGUMENT', f'complexity must be one of {", ".join(COMPLEXITIES)}.')
    if args.get('model') is not None and not (isinstance(args['model'], str) and MODEL_RE.fullmatch(args['model'])):
        raise EngineError('INVALID_ARGUMENT', 'model must be a GPT Image model id such as gpt-image-2.6-sunburst.')
    if name == 'chatgpt':
        return {'engine': 'chatgpt-plan', 'base': BASE, 'max_refs': 5, 'model': 'gpt-image-2'}
    key = os.environ.get('OPENAI_API_KEY', '').strip()
    if not key:
        raise EngineError('UPSTREAM_AUTH_FAILED', 'CODEX_IMAGES_ENGINE=api requires OPENAI_API_KEY.')
    model = args.get('model') or os.environ.get('CODEX_IMAGES_API_MODEL') or DEFAULT_API_MODEL
    if not MODEL_RE.fullmatch(model):
        raise EngineError('INVALID_ARGUMENT', 'CODEX_IMAGES_API_MODEL must be a GPT Image model id.')
    return {'engine': 'openai-api', 'base': API_BASE, 'max_refs': 16, 'model': model, 'key': key}


def _api_error(exc: HTTPError, key: str) -> EngineError:
    """Map an OpenAI API error without ever echoing the key."""
    try:
        detail = json.loads(exc.read() or b'{}').get('error', {}).get('message') or ''
    except (ValueError, AttributeError, OSError):
        detail = ''
    detail = detail.replace(key, '[redacted]')[:300] if isinstance(detail, str) else ''
    suffix = f': {detail}' if detail else '.'
    if exc.code in (401, 403):
        return EngineError('UPSTREAM_AUTH_FAILED', 'OpenAI API rejected the API key or organization' + suffix, False)
    return EngineError('UPSTREAM_GENERATION_FAILED', f'OpenAI API returned HTTP {exc.code}' + suffix,
                       exc.code == 429 or exc.code >= 500)


def request_image(body, config=None) -> tuple[bytes, bool]:
    config = config or {'engine': 'chatgpt-plan', 'base': BASE}
    api = config['engine'] == 'openai-api'
    if api:
        headers = {'Authorization': f"Bearer {config['key']}"}
    else:
        try:
            headers = {k: v for k, v in oauth_headers(None).items() if k.lower() != 'openai-beta'}
        except Exception as exc:
            raise EngineError('UPSTREAM_AUTH_FAILED', 'Codex OAuth authentication failed.') from exc
        headers.update({'originator': 'codex_cli_rs', 'User-Agent': 'codex_cli_rs/0.163.0 (Ubuntu 24.4.0; x86_64) xterm'})
    headers.update({'Content-Type': 'application/json', 'Accept': 'application/json'})
    request = Request(config['base'] + ('/edits' if body.get('images') else '/generations'), data=json.dumps(body).encode(), headers=headers, method='POST')
    label = 'OpenAI API' if api else 'Codex Images'
    for attempt in range(2):
        try:
            with urlopen(request, timeout=280) as response:
                result = json.loads(response.read())
            data = base64.b64decode(result['data'][0]['b64_json'], validate=True)
            with Image.open(BytesIO(data)) as image:
                image.load()
                if image.format != 'PNG':
                    raise ValueError('Expected PNG')
                opaque = image.convert('RGBA').getchannel('A').getextrema()[0] == 255
            return data, opaque
        except HTTPError as exc:
            content_type = exc.headers.get('Content-Type', '').lower() if exc.headers else ''
            if api:
                error = _api_error(exc, config['key'])
            elif exc.code in (401, 403) and 'json' in content_type:
                error = EngineError('UPSTREAM_AUTH_FAILED', 'Codex Images authentication was rejected.', False)
            elif exc.code == 403:
                error = EngineError('UPSTREAM_GENERATION_FAILED', 'Codex Images was blocked by Cloudflare (HTML 403).', False)
            else:
                error = EngineError('UPSTREAM_GENERATION_FAILED', f'Codex Images returned HTTP {exc.code}.', exc.code == 429 or exc.code >= 500)
        except (URLError, TimeoutError, socket.timeout):
            error = EngineError('UPSTREAM_GENERATION_FAILED', f'{label} request timed out or failed to connect.', True)
        except (ValueError, KeyError, IndexError, TypeError, OSError):
            error = EngineError('UPSTREAM_GENERATION_FAILED', f'{label} returned an invalid image response.', False)
        if not error.retryable or attempt:
            raise error

    raise EngineError('UPSTREAM_GENERATION_FAILED', 'Request failed.')


def generate(args):
    """Return the legacy-shaped intermediate consumed by the v2 artifact adapter."""
    warnings = []
    images, errors = [], []
    try:
        config = engine_config(args)
    except EngineError as exc:
        error = exc.record(0)
        return {'ok': False, 'images': [], 'errors': [error], 'error': error['message'], 'warnings': [], 'models': {}}
    if config['engine'] == 'openai-api':
        models = {'engine': 'openai-api', 'requested': config['model'], 'dispatched': config['model'], 'confirmed': True}
    else:
        models = {'engine': 'chatgpt-plan', 'dispatched': 'gpt-image-2', 'provider': None, 'confirmed': False}
        ignored = [field for field in API_OPTIONS if args.get(field) is not None]
        if ignored:
            warnings.append(f"{', '.join(ignored)} are ignored on the ChatGPT plan route; the server picks the model. "
                            "Set CODEX_IMAGES_ENGINE=api with OPENAI_API_KEY to choose them.")
    prompt = args.get('prompt')
    prompts = prompt if isinstance(prompt, list) else [prompt] * (int(args.get('count', 1)) if args.get('batch_mode') == 'variants' else 1)
    if not prompts:
        error = EngineError('INVALID_ARGUMENT', 'Prompt list must contain at least one item.').record(0)
        return {'ok': False, 'images': [], 'errors': [error], 'error': error['message'], 'warnings': [], 'models': models}
    root = Path(args['output_dir'])
    root.mkdir(parents=True, exist_ok=True)

    def one(index, prompt, refs):
        try:
            if len(refs) > config['max_refs']:
                raise EngineError('TOO_MANY_REFERENCES', f"This engine accepts at most {config['max_refs']} reference images per request.")
            body: dict[str, Any] = {'prompt': render_prompt(prompt), 'background': 'transparent' if args.get('transparent') else 'opaque', 'model': config['model'], 'quality': 'auto', 'size': size_hint(args.get('size', 'auto'))}
            if config['engine'] == 'openai-api':
                body['output_format'] = 'png'
                body['quality'] = args.get('quality') or 'auto'
                if args.get('complexity'):
                    body['complexity'] = args['complexity']
            if refs:
                body['images'] = [data_url(ref) for ref in refs]
            if args.get('mask'):
                if not refs:
                    raise EngineError('INVALID_ARGUMENT', 'A mask requires a reference image.')
                body['mask'] = data_url(args['mask'])
            data, opaque = request_image(body, config)
            path = root / ('anchor.png' if index == -1 else f'{index}.png')
            path.write_bytes(data)
            notes = []
            if args.get('transparent') and opaque:
                notes.append(f'Image {index} is fully opaque despite requested transparency; no fallback was applied.')
            return {'index': index, 'path': str(path)}, None, notes
        except EngineError as exc:
            return None, exc.record(index), []
        except Exception as exc:
            # Preserve safe typed boundary failures without copying arbitrary exception text.
            code = getattr(exc, 'code', 'UPSTREAM_GENERATION_FAILED')
            retryable = bool(getattr(exc, 'retryable', False))
            return None, EngineError(code, 'Image generation or input processing failed.', retryable).record(index), []

    refs = list(args.get('references') or [])
    mode = args.get('batch_mode', 'auto')
    anchor_path = None
    if args.get('anchor') is not None and mode in ('chain', 'branded-parallel', 'auto'):
        anchor, error, notes = one(-1, args['anchor'], refs)
        warnings.extend(notes)
        if error:
            errors = [{**error, 'input_index': i} for i in range(len(prompts))]
        else:
            anchor_path = anchor['path']
            if mode == 'auto':
                mode = 'branded-parallel'
    if not errors:
        base_refs = refs + ([anchor_path] if anchor_path else [])
        if mode == 'chain':
            outcomes = []
            previous = None
            for index, item in enumerate(prompts):
                outcome = one(index, item, base_refs + ([previous] if previous else []))
                outcomes.append(outcome)
                if outcome[0]:
                    previous = outcome[0]['path']
        else:
            with ThreadPoolExecutor(max_workers=4) as pool:
                outcomes = list(pool.map(lambda pair: one(pair[0], pair[1], base_refs), enumerate(prompts)))
        for image, error, notes in outcomes:
            warnings.extend(notes)
            if image:
                images.append(image)
            if error:
                errors.append(error)
    return {'ok': not errors, 'images': images, 'errors': errors, 'error': errors[0]['message'] if errors else None, 'warnings': warnings, 'models': models}
