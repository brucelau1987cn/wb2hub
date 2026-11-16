"""Request-local routing namespace; never a client/upstream session identifier."""
import contextvars
import hashlib
import json

_PRINCIPAL: contextvars.ContextVar[str | None] = contextvars.ContextVar(
    'wb_session_principal', default=None)


def enter(entry):
    """Scope only to authenticated credentials, never an unverified input header."""
    if isinstance(entry, dict):
        # Include both settings identity and secret: rotating/reusing a settings ID
        # must not inherit another credential's binding. Never retain the secret.
        material = [entry.get('id'), entry.get('key')]
        principal = hashlib.sha256(json.dumps(material, ensure_ascii=False).encode()).hexdigest()
    else:
        principal = None
    return _PRINCIPAL.set(principal)


def leave(token):
    _PRINCIPAL.reset(token)


def routing_key(session, realm):
    """Disjoint (authenticated key, effective realm, session) binding identity."""
    if not session:
        return None
    material = [_PRINCIPAL.get(), realm, session]
    return 'route-v1-' + hashlib.sha256(json.dumps(
        material, ensure_ascii=False, separators=(',', ':')).encode()).hexdigest()
