"""Side-effect-free diagnostic output filtering; never use on protocol payloads.

Adapted from NextAgentX's PolyForm Noncommercial reference; see
 docs/LOG-REDACTION.md for the pinned source revision and adaptations.
Required Notice: Copyright (c) 2026 NextAgentX (https://github.com/NextAgentX/qoder-trae-workbuddy-switch)
License: https://polyformproject.org/licenses/noncommercial/1.0.0
This adaptation is not relicensed under the surrounding repository's license.
"""
import json
import re

REDACTED = '<redacted>'
MAX_DETAIL = 4096
_JWT = re.compile(r'eyJ[A-Za-z0-9_+/=-]*\.[A-Za-z0-9_+/=-]+\.[A-Za-z0-9_+/=-]+')
_HEADER = re.compile(r'(?im)(?<![\w-])((?:proxy-)?authorization|cookie|set-cookie)\s*:\s*[^\r\n]*')
_PAYLOAD = re.compile(r'(?im)(?<![\w-])(?:prompt|body|request[_-]?body|response[_-]?body|headers|auth[_-]?headers|messages)\s*[:=]\s*[^\r\n]*')
_AUTH = re.compile(r'(?i)\b(bearer|basic)([ \t]+)[^\s\"\',;}&\])]+')
_API_KEY = re.compile(r'\bsk-[A-Za-z0-9_-]{12,}')
_FIELDS = re.compile(
    r'''(?ix)(?<![\w-])(["']?)(?:access[_-]?token|refresh[_-]?token|id[_-]?token|
    device[_-]?token|session[_-]?token|token|password|passwd|secret|client[_-]?secret|
    api[_-]?key|x-api-key|x-panel-token|codebuddy_api_key|authorization|proxy-authorization|
    cookie|set-cookie)\1(?![\w-])\s*[:=]\s*
    (?:"(?:\\.|[^"\\])*"?|'(?:\\.|[^'\\])*'?|[^\s"',;&}\])]+)'''
)


def _redact_field(match):
    # Preserve the field spelling and delimiter, never any of its value.
    prefix = re.match(r'''["']?[^:=]+?\s*[:=]\s*''', match.group())
    return (prefix.group() if prefix else '') + REDACTED


# Error envelopes keep only diagnostic metadata; never arbitrary nested data.
_ERROR_FIELDS = frozenset(('code', 'error_code', 'status', 'statusCode', 'message',
                          'msg', 'displayMsg', 'reason', 'type', 'requestId',
                          'request_id', 'error', 'detail', 'model', 'endpoint', 'method'))


def _error_metadata(value):
    if isinstance(value, dict):
        return {key: _error_metadata(item) for key, item in value.items()
                if key in _ERROR_FIELDS}
    if isinstance(value, str):
        return _redact_text(value)
    if isinstance(value, (int, float, bool)) or value is None:
        return value
    return REDACTED


def _project_envelopes(text):
    # Decode only a copy of embedded JSON, never the caller's parser body.
    decoder = json.JSONDecoder()
    parts = []
    cursor = 0
    while True:
        start = text.find('{', cursor)
        if start < 0:
            parts.append(_redact_text(text[cursor:]))
            return ''.join(parts)
        parts.append(_redact_text(text[cursor:start]))
        try:
            value, end = decoder.raw_decode(text, start)
            parts.append(json.dumps(_error_metadata(value), ensure_ascii=False))
            cursor = end
        except (ValueError, RecursionError):
            # A malformed object may be a body dump: fail closed for its suffix.
            parts.append('[unparsed diagnostic object omitted]')
            return ''.join(parts)


def _redact_text(text):
    # A textual header is opaque: consume the whole line, not one word/cookie.
    text = _HEADER.sub(lambda m: m.group(1) + ': ' + REDACTED, text)
    text = _AUTH.sub(lambda m: m.group(1) + m.group(2) + REDACTED, text)
    text = _FIELDS.sub(_redact_field, text)
    text = _API_KEY.sub(REDACTED, text)
    return _JWT.sub(REDACTED, text)


def diagnostic_detail(value, limit=MAX_DETAIL):
    """Redact the complete text, then bound Unicode code points incl. ellipsis.

    This is defense in depth, not permission to pass prompts or HTTP bodies.
    """
    text = str(value) if value is not None else ''
    # Opaque explicit payload assignments: don't try to sanitize their content.
    text = _PAYLOAD.sub('[payload omitted]', text)
    text = _project_envelopes(text)
    if len(text) > limit:
        return text[:max(0, limit - 1)] + ('…' if limit else '')
    return text
