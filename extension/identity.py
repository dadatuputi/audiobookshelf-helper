#!/usr/bin/env python3
"""
Who this extension is, in one place.

build.py, native/install.py, the tests and the release workflow all read this,
so the add-on id, the native host name and the Chrome extension id can never
drift apart. A mismatch between the manifest and the host manifest produces a
browser that silently cannot find its helper, which is a miserable thing to
debug - hence one source of truth.
"""
import base64, hashlib, json
from pathlib import Path

HERE = Path(__file__).resolve().parent
PATH = HERE / "identity.json"


def load() -> dict:
    return json.loads(PATH.read_text())


def chrome_id_from_key(key_b64: str) -> str:
    """Derive the Chrome extension id from a base64 DER public key.

    Chrome hashes the key and maps each of the first 32 hex digits onto a-p.
    Pinning `key` in the manifest is what stops an unpacked extension getting a
    fresh id on every load - which otherwise makes the native host's
    allowed_origins impossible to write down in advance.
    """
    if not key_b64:
        return ""
    digest = hashlib.sha256(base64.b64decode(key_b64)).hexdigest()[:32]
    return "".join(chr(ord("a") + int(c, 16)) for c in digest)


def chrome_ids(identity: dict = None) -> list:
    """Explicit ids from identity.json, else the one implied by the key."""
    identity = identity or load()
    explicit = list(identity.get("chromeIds") or [])
    if explicit:
        return explicit
    derived = chrome_id_from_key(identity.get("chromeKey") or "")
    return [derived] if derived else []


if __name__ == "__main__":
    i = load()
    print(f"gecko id   {i['geckoId']}")
    print(f"host name  {i['hostName']}")
    print(f"chrome ids {chrome_ids(i) or '(none - set chromeKey or chromeIds)'}")
