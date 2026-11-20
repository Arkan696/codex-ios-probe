"""Host-side, single-generation relay for the simulator's fixed editing fixture."""
import http.client
import json
import os
import re
import ssl
import tempfile
from pathlib import Path


class RelayError(RuntimeError):
    """A diagnostic safe to report to the runner."""


_INSTRUCTION = '把会议改到周四下午两点，其他内容不变。'
_HOST = 'new.maoyu.cfd'
_ID = re.compile(r'[A-Za-z0-9][A-Za-z0-9._:/-]{0,199}')
_MINI = re.compile(r'gpt-[0-9]+(?:\.[0-9]+|o)?-mini(?:-\d{4}-\d{2}-\d{2})?')


def _unique(pairs):
    obj = dict(pairs)
    if len(obj) != len(pairs):
        raise ValueError('duplicate field')
    return obj


def _json(stream, limit):
    data = stream.read(limit + 1)
    if len(data) > limit:
        raise ValueError('size limit')
    return json.loads(data.decode('utf-8'), object_pairs_hook=_unique)


def _api(key, path, payload, limit):
    # Unlike a URL opener this connection neither follows redirects nor consults
    # proxy settings. Explicit TLS setup also avoids SSLKEYLOGFILE tracing.
    tls = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    tls.load_default_certs()
    conn = http.client.HTTPSConnection(_HOST, timeout=30, context=tls)
    conn.set_debuglevel(0)
    headers = {'Authorization': 'Bearer ' + key, 'Accept': 'application/json'}
    try:
        body = None
        if payload is not None:
            headers['Content-Type'] = 'application/json'
            body = json.dumps(payload, ensure_ascii=False).encode('utf-8')
        conn.request('GET' if body is None else 'POST', path, body, headers)
        with conn.getresponse() as response:
            if response.status != 200:
                # Never read or propagate an HTTP error body/reason/location.
                raise RelayError(f'Live relay {path}: HTTP {int(response.status)}')
            return _json(response, limit)
    finally:
        conn.close()


def _select(catalog):
    ids = {entry['id'] for entry in catalog['data']}
    minis = sorted(i for i in ids if _MINI.fullmatch(i))
    for model in ('deepseek-v4-flash', 'deepseek-v4-flash-0731', 'glm-5.3-flash',
                  'gpt-4o-mini', 'gpt-4.1-mini', *minis, 'mistral-small-latest',
                  'ministral-8b-latest', 'qwen3.8-27b', 'minimax-m3', 'gpt-6.2-astra'):
        if model in ids:
            return model
    raise RelayError('Live relay: no supported text model advertised')


def _extract(response, nonce, key):
    choice, = response['choices']
    message = choice['message']
    if (response.get('error') is not None or choice.get('finish_reason') != 'stop'
            or message.get('role') != 'assistant' or message.get('tool_calls')
            or message.get('function_call') or message.get('refusal')):
        raise RelayError('Live relay: expected one completed assistant text response')
    candidate = message.get('content')
    if candidate != f'Meeting: Thursday at 14:00\nReference: {nonce}':
        raise RelayError('Live relay: candidate does not match the required edit')
    model, rid = response['model'], response['id']
    if not all(isinstance(s, str) and _ID.fullmatch(s) and key not in s for s in (model, rid)):
        raise RelayError('Live relay: invalid upstream identifiers')
    return dict(runID=nonce, candidate=candidate, model=model, responseID=rid)


def _publish(dest, record):
    with tempfile.NamedTemporaryFile(dir=dest.parent, delete=False) as tmp:
        path = Path(tmp.name)
        try:
            tmp.write(json.dumps(record, ensure_ascii=False).encode('utf-8'))
            tmp.close()
            os.replace(path, dest)
        finally:
            path.unlink(missing_ok=True)


def service(container: Path, nonce: str, key: str) -> None:
    """Run once after live-request.json exists; atomically publish or raise RelayError."""
    phase = 'invalid local request'
    try:
        docs = container / 'Documents'
        with (docs / 'live-request.json').open('rb') as stream:
            req = _json(stream, 4096)
        if (not isinstance(nonce, str) or not nonce or req != dict(
                runID=nonce, original=f'Meeting: Tuesday at 10:00\nReference: {nonce}',
                instruction=_INSTRUCTION)):
            raise RelayError('Live relay: invalid local request')
        if not isinstance(key, str) or not re.fullmatch(r'[!-~]{1,4096}', key):
            raise RelayError('Live relay: invalid credential')
        phase = 'model discovery failed'
        model = _select(_api(key, '/v1/models', None, 128 * 1024))
        phase = 'generation failed or malformed response'
        messages = [dict(role='system', content='Return only the full edited document. Keep the original language, 24-hour time format and line breaks. No markdown or explanation.'),
                    dict(role='user', content=req['instruction'] + '\n\n' + req['original'])]
        result = _api(key, '/v1/chat/completions', dict(model=model, messages=messages,
                      max_tokens=512, stream=False), 32 * 1024)
        record = _extract(result, nonce, key)
        phase = 'could not publish response'
        _publish(docs / 'live-response.json', record)
    except RelayError:
        raise
    except Exception:
        # Raise outside the handler: no raw network/JSON/OS exception chain.
        pass
    else:
        return
    raise RelayError(f'Live relay: {phase}')
