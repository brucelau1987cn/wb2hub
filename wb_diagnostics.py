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
_HEADER = re.compile(r'(?im)(?<![\w-])((?:proxy-)?authorization|cookie|set-cookie)\s*[:=]\s*[^\r\n]*')
_PAYLOAD = re.compile(r'(?im)(?<![\w-])(?:prompt|body|request[_-]?body|response[_-]?body|headers|auth[_-]?headers|messages)\s*[:=]\s*[^\r\n]*')
_AUTH = re.compile(r'(?i)\b(bearer|basic)([ \t]+)[^\r\n]*')
_API_KEY = re.compile(r'\bsk-[A-Za-z0-9_-]{12,}')
_FIELDS = re.compile(
    r'''(?ix)(?<![\w-])(["']?)(?:access[_-]?token|refresh[_-]?token|id[_-]?token|
    device[_-]?token|session[_-]?token|token|password|passwd|secret|client[_-]?secret|
    api[_-]?key|x-api-key|x-panel-token|codebuddy_api_key|authorization|proxy-authorization|
    cookie|set-cookie)\1(?![\w-])\s*[:=]\s*
    (?:"(?:\\.|[^"\\])*"?|'(?:\\.|[^'\\])*'?|[^\r\n]+)'''
)


_MESSAGE_SUMMARY = re.compile(
    r'''messages: model=[\w./:-]+ stream=(?:True|False) msgs=\d+ '''
    r'''effort=(?:None|'[\w.-]+'|"[\w.-]+") tools=\d+ key=[\w.-]+\s*\Z''')


def _redact_payload(match):
    # The gateway's fixed metric-only Messages log is not a client body dump.
    return match.group() if _MESSAGE_SUMMARY.fullmatch(match.group()) else '[payload omitted]'


def _redact_field(match):
    # Preserve the field spelling and delimiter, never any of its value.
    prefix = re.match(r'''["']?[^:=]+?\s*[:=]\s*''', match.group())
    label = prefix.group() if prefix else ''
    value = match.group()[len(label):]
    # Retain an explicit quoted boundary on repeated passes. An unquoted marker
    # would become an opaque rest-of-line value and swallow following metadata.
    quote = value[0] if value and value[0] in '\"\'' else ''
    return label + quote + REDACTED + quote


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


def _clip(text, limit):
    if len(text) <= limit:
        return text
    return text[:max(0, limit - 1)] + ('…' if limit else '')


def _bounded_parts(parts, limit):
    """Budget string values, never slice serialized JSON or its closing syntax."""
    def render(cap):
        def bounded(value):
            if isinstance(value, dict):
                return {key: bounded(item) for key, item in value.items()}
            if isinstance(value, str):
                return _clip(value, cap)
            return value
        return ''.join(json.dumps(bounded(part), ensure_ascii=False)
                       if isinstance(part, dict) else _clip(part, cap) for part in parts)

    full = render(limit)
    if len(full) <= limit:
        return full
    if all(isinstance(part, str) for part in parts):
        return _clip(''.join(parts), limit)
    # Binary search serialized size, including escaped quotes and backslashes.
    # Keep scalar codes and all envelope keys even when reasons need shortening.
    low, high = 0, limit
    if len(render(0)) > limit:
        # An envelope's fixed syntax alone cannot fit (tiny bound/many objects).
        return '{}' if limit >= 2 else _clip('…', limit)
    while low < high:
        mid = (low + high + 1) // 2
        if len(render(mid)) <= limit:
            low = mid
        else:
            high = mid - 1
    return render(low)


def _project_envelopes(text, limit):
    # Decode only a copy of embedded JSON, never the caller's parser body.
    decoder = json.JSONDecoder()
    parts = []
    cursor = 0
    while True:
        start = text.find('{', cursor)
        # Resolve opaque credentials before looking inside them for objects.
        # Decode actual objects first, so no regex can corrupt their JSON syntax.
        spans = [match for pattern in (_PAYLOAD, _HEADER, _AUTH, _FIELDS)
                 if (match := pattern.search(text, cursor)) is not None]
        span = min(spans, key=lambda match: match.start()) if spans else None
        if span is not None and (start < 0 or span.start() < start):
            parts.append(_redact_text(text[cursor:span.start()]))
            parts.append(_redact_text(span.group()))
            cursor = span.end()
            continue
        if start < 0:
            parts.append(_redact_text(text[cursor:]))
            return _bounded_parts(parts, limit)
        parts.append(_redact_text(text[cursor:start]))
        try:
            value, end = decoder.raw_decode(text, start)
            parts.append(_error_metadata(value))
            cursor = end
        except (ValueError, RecursionError):
            # A malformed object may be a body dump: fail closed for its suffix.
            parts.append('[unparsed diagnostic object omitted]')
            return _bounded_parts(parts, limit)


def _redact_text(text):
    # A textual header is opaque: consume the whole line, not one word/cookie.
    text = _PAYLOAD.sub(_redact_payload, text)
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
    return _project_envelopes(text, limit)
