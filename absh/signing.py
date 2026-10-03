"""What a signed release looks like, and how to tell whether one is ours.

A release carries one manifest, SHA256SUMS, listing the sha256 and name of
every archive the workflow built, and one detached signature over it,
SHA256SUMS.sig, made by the maintainer on their own machine after CI has
published. Signing the list rather than each file keeps it to one signature
per release however many archives there are, and keeps the manifest readable
by `sha256sum -c`, which ignores the "#" lines.

The manifest names its tag. Without that, a genuine signature over an old
release's manifest could be attached to a new release and still verify; with
it, the updater can insist the manifest it was handed is for the release it
asked for.

    # audiobookshelf-helper release manifest v1
    # tag v1.0.0
    <sha256>  audiobookshelf-helper-native-1.0.0.zip
    ...

The signature file has one line per key, so a release can be signed by an
old and a new key at once while installed copies move from one to the other:

    # audiobookshelf-helper release signature v1
    ed25519 <key id> <base64 signature>

Nothing here decides which keys to trust. That is the installed copy's
absh/release_keys.py, read by the updater from the installation it is about
to replace - never from the release being installed, which would let a
release vouch for itself.
"""
import ast
import base64
import binascii
import hashlib
import re
from pathlib import Path

from . import ed25519

MANIFEST_NAME = "SHA256SUMS"
SIGNATURE_NAME = "SHA256SUMS.sig"
MANIFEST_HEADER = "# audiobookshelf-helper release manifest v1"
SIGNATURE_HEADER = "# audiobookshelf-helper release signature v1"
KEY_PREFIX = "ed25519:"

_LINE = re.compile(r"^([0-9a-f]{64})  ([^\s/\\]+)$")


class SignatureError(Exception):
    """A manifest, signature or key that cannot be accepted, and why."""


# ----------------------------------------------------------------- keys
def format_key(public):
    return KEY_PREFIX + base64.b64encode(public).decode("ascii")


def parse_key(text):
    """32 raw bytes from "ed25519:<base64>", or SignatureError."""
    text = (text or "").strip()
    if not text.startswith(KEY_PREFIX):
        raise SignatureError(f"{text!r} is not an {KEY_PREFIX}<base64> key")
    try:
        raw = base64.b64decode(text[len(KEY_PREFIX):], validate=True)
    except (binascii.Error, ValueError):
        raise SignatureError(f"{text!r} is not valid base64")
    if len(raw) != 32:
        raise SignatureError(f"{text!r} is {len(raw)} bytes, not 32")
    return raw


def key_id(public):
    """A short, stable name for a key, for messages and the signature file."""
    return hashlib.sha256(public).hexdigest()[:16]


def read_pinned(path):
    """The keys listed in a release_keys.py, without executing it.

    Parsed rather than imported because the file being read is usually not
    the module this process imported - it is the installation on disk - and
    because importing is running code, which is the wrong way to read a
    trust list. A missing file is no keys; a malformed one is an error, not
    an empty list, so a typo cannot quietly look like "nothing pinned".
    """
    try:
        source = Path(path).read_text(encoding="utf-8")
    except FileNotFoundError:
        return []
    except OSError as e:
        raise SignatureError(f"cannot read {path}: {e}")
    return parse_pinned(source, path)


def parse_pinned(source, path="release_keys.py"):
    """read_pinned over text already in hand, such as a tagged revision's."""
    try:
        tree = ast.parse(source, filename=str(path))
    except SyntaxError as e:
        raise SignatureError(f"{path} is not valid Python: {e}")
    for node in tree.body:
        if (isinstance(node, ast.Assign) and len(node.targets) == 1
                and isinstance(node.targets[0], ast.Name)
                and node.targets[0].id == "KEYS"):
            try:
                value = ast.literal_eval(node.value)
            except (ValueError, TypeError, SyntaxError):
                raise SignatureError(f"KEYS in {path} must be a plain list of strings")
            if not isinstance(value, (list, tuple)):
                raise SignatureError(f"KEYS in {path} must be a list")
            out = []
            for entry in value:
                if not isinstance(entry, str):
                    raise SignatureError(f"KEYS in {path} holds {entry!r}, not a key string")
                k = parse_key(entry)
                if k not in out:
                    out.append(k)
            return out
    raise SignatureError(f"{path} does not define KEYS")


# ------------------------------------------------------------- manifest
def make_manifest(tag, digests):
    """The manifest bytes for `tag`, from {asset name: sha256 hex}."""
    if not tag or any(c.isspace() for c in tag):
        raise SignatureError(f"tag {tag!r} cannot go in a manifest")
    lines = [MANIFEST_HEADER, f"# tag {tag}"]
    for name in sorted(digests):
        line = f"{digests[name]}  {name}"
        if not _LINE.match(line):
            raise SignatureError(f"cannot list {name!r} in a manifest")
        lines.append(line)
    return ("\n".join(lines) + "\n").encode("utf-8")


def manifest_for(tag, paths):
    """make_manifest over files on disk, named by their basenames."""
    digests = {}
    for p in paths:
        p = Path(p)
        digests[p.name] = hashlib.sha256(p.read_bytes()).hexdigest()
    return make_manifest(tag, digests)


def parse_manifest(data):
    """(tag, {name: sha256}) from manifest bytes, strictly.

    Strict because these are the bytes the signature covers: anything this
    does not understand is refused rather than skipped, so there is no line
    a signer saw one way and the updater reads another.
    """
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError:
        raise SignatureError("the manifest is not UTF-8 text")
    lines = text.split("\n")
    if lines and lines[-1] == "":
        lines.pop()
    if len(lines) < 2 or lines[0] != MANIFEST_HEADER:
        raise SignatureError("the manifest is not an audiobookshelf-helper release manifest")
    m = re.match(r"^# tag (\S+)$", lines[1])
    if not m:
        raise SignatureError("the manifest does not name its tag")
    digests = {}
    for line in lines[2:]:
        hit = _LINE.match(line)
        if not hit:
            raise SignatureError(f"the manifest has a line it should not: {line[:80]!r}")
        if hit.group(2) in digests:
            raise SignatureError(f"the manifest lists {hit.group(2)} twice")
        digests[hit.group(2)] = hit.group(1)
    return m.group(1), digests


# ------------------------------------------------------------ signature
def sign_manifest(secrets, manifest):
    """Signature-file text: one line per private key in `secrets`."""
    lines = [SIGNATURE_HEADER]
    for secret in secrets:
        pub = ed25519.public_key(secret)
        sig = ed25519.sign(secret, manifest)
        lines.append(f"ed25519 {key_id(pub)} {base64.b64encode(sig).decode('ascii')}")
    return "\n".join(lines) + "\n"


def parse_signature(data):
    """[(key id, 64-byte signature)] from signature-file bytes."""
    try:
        text = data.decode("utf-8") if isinstance(data, bytes) else data
    except UnicodeDecodeError:
        raise SignatureError("the signature file is not UTF-8 text")
    lines = [ln.strip() for ln in text.splitlines() if ln.strip()]
    if not lines or lines[0] != SIGNATURE_HEADER:
        raise SignatureError("the signature file is not an audiobookshelf-helper signature")
    out = []
    for line in lines[1:]:
        parts = line.split()
        if len(parts) != 3 or parts[0] != "ed25519":
            raise SignatureError(f"the signature file has a line it should not: {line[:80]!r}")
        try:
            sig = base64.b64decode(parts[2], validate=True)
        except (binascii.Error, ValueError):
            raise SignatureError("the signature file holds a signature that is not base64")
        out.append((parts[1], sig))
    if not out:
        raise SignatureError("the signature file holds no signatures")
    return out


def verify(manifest, signature, keys):
    """The id of the trusted key that signed `manifest`, or SignatureError.

    Every line is tried against every pinned key it could belong to; one
    good pair is enough. The id on a line only says which key to try - it is
    never trusted on its own, so a line claiming a pinned key's id with a
    signature that does not verify is a failure, not a match.
    """
    if not keys:
        raise SignatureError("no signing key is pinned")
    pinned = {key_id(k): k for k in keys}
    lines = parse_signature(signature)
    for kid, sig in lines:
        k = pinned.get(kid)
        if k is not None and ed25519.verify(k, manifest, sig):
            return kid
    known = [kid for kid, _ in lines if kid in pinned]
    if known:
        raise SignatureError(
            f"the signature claims key {known[0]}, which this copy trusts, but does "
            "not verify - the manifest or its signature has been altered")
    raise SignatureError(
        "it is signed only by key " + ", ".join(kid for kid, _ in lines)
        + ", which this copy does not trust")
