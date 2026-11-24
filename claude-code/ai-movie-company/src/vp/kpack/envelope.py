"""Verify the exact bytes signed by a release key before interpreting the payload."""
import base64

from cryptography.exceptions import InvalidSignature, UnsupportedAlgorithm
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

from . import keys
from .schema import MAX_ENVELOPE, PackError, decode_json, object_fields, validate


def _decode(value):
    try:
        if isinstance(value, str):
            decoded = base64.b64decode(value, validate=True)
            if base64.b64encode(decoded).decode("ascii") == value:
                return decoded
    except ValueError:
        pass
    raise PackError("base64", "invalid envelope base64")


def verify(raw, public_keys=None):
    if len(raw) > MAX_ENVELOPE:
        raise PackError("size", "envelope exceeds 512 KiB")
    entry = decode_json(raw)
    object_fields(entry, {"format", "key_id", "sig", "payload"}, "envelope")
    if type(entry["format"]) is not int or entry["format"] != 1:
        raise PackError("schema", "unsupported envelope format")
    trusted = keys.PUBLIC_KEYS if public_keys is None else public_keys
    key_id = entry["key_id"]
    if not isinstance(key_id, str) or key_id not in trusted:
        raise PackError("key", "unknown signing key")
    payload = _decode(entry["payload"])
    signature = _decode(entry["sig"])
    try:
        Ed25519PublicKey.from_public_bytes(_decode(trusted[key_id])).verify(signature, payload)
    except (InvalidSignature, UnsupportedAlgorithm, ValueError) as exc:
        raise PackError("signature", "invalid Ed25519 signature") from exc
    obj = decode_json(payload)
    validate(obj)
    return obj
