"""Standalone field proposal experiment. No filesystem writes and no simulator."""
import json
import os
import re
import secrets
from live_relay import RelayError, _api, _unique, _MODEL, _INSTRUCTION

DAYS = 'Monday Tuesday Wednesday Thursday Friday Saturday Sunday'.split()
SYSTEM = ('Extract the requested meeting weekday and time. Return only a JSON object with '
          'exactly two string keys: weekday (full English weekday name), time (24-hour HH:MM). '
          'No prose, markdown, or document text.')

def parse(raw):
    if not isinstance(raw, str):
        raise RelayError('field diagnostic: non-text content')
    try:
        fields = json.loads(raw, object_pairs_hook=_unique)
    except ValueError:
        raise RelayError('field diagnostic: invalid JSON or duplicate key') from None
    if not isinstance(fields, dict) or set(fields) != {'weekday', 'time'}:
        raise RelayError('field diagnostic: unexpected fields')
    day, clock = fields['weekday'], fields['time']
    if day not in DAYS or not isinstance(clock, str) or not re.fullmatch(r'(?:[01][0-9]|2[0-3]):[0-5][0-9]', clock):
        raise RelayError('field diagnostic: invalid weekday or time')
    return day, clock

def preview(source, day, clock):
    match = re.match(rb'\AMeeting: (Tuesday) at (10:00)(?=\r?\n|\Z)', source)
    if not match:
        raise RelayError('field diagnostic: unexpected local base')
    return (source[:match.start(1)] + day.encode('ascii') + source[match.end(1):match.start(2)]
            + clock.encode('ascii') + source[match.end(2):])

def diagnose(key, source):
    if not key:
        raise RelayError('field diagnostic: missing credential')
    schema = {'type': 'object', 'properties': {'weekday': {'type': 'string', 'enum': DAYS},
              'time': {'type': 'string'}}, 'required': ['weekday', 'time'], 'additionalProperties': False}
    fmt = {'type': 'json_schema', 'json_schema': {'name': 'meeting_fields', 'strict': True, 'schema': schema}}
    response = _api(key, '/v1/chat/completions', dict(model=_MODEL, stream=False, max_tokens=512, response_format=fmt,
                    messages=[dict(role='system', content=SYSTEM), dict(role='user', content=_INSTRUCTION)]), 32768)
    choices = response.get('choices') or []
    if len(choices) != 1 or choices[0].get('finish_reason') != 'stop':
        raise RelayError('field diagnostic: no complete single reply')
    msg = choices[0].get('message') or {}
    if msg.get('role') != 'assistant' or any(msg.get(k) for k in ('tool_calls','function_call','refusal')):
        raise RelayError('field diagnostic: not a plain assistant message')
    fields = parse(msg.get('content'))
    if fields != ('Thursday', '14:00'):
        raise RelayError('field diagnostic: valid fields but incorrect requested change')
    return preview(source, *fields)

if __name__ == '__main__':
    suffix = b'\r\nReference: ' + secrets.token_hex(16).encode() + b'\n'
    try:
        got = diagnose(os.environ.pop('MODEL_API_KEY', ''), b'Meeting: Tuesday at 10:00' + suffix)
        if got != b'Meeting: Thursday at 14:00' + suffix:
            raise RelayError('field diagnostic: preview byte mismatch')
    except RelayError as exc:
        raise SystemExit(str(exc))
    except Exception:
        raise SystemExit('field diagnostic: transport or response structure failure')
    print('Field diagnostic passed: Thursday 14:00; unrelated bytes preserved; no writes')
