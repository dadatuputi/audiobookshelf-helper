"""Ed25519 (RFC 8032), in plain Python, for checking who signed a release.

The standard library has hashes and HMAC but no public-key signatures, and the
helper stays stdlib-only so that the browser can always launch it. The other
way to verify a signature without a dependency is to shell out to something
like `ssh-keygen -Y verify`, which is not on every Windows install, differs by
version where it is, and turns "is this release ours" into "which ssh-keygen
is first on PATH". Doing the arithmetic here costs a few hundred lines and a
few tens of milliseconds once per update, and needs nothing but Python.

This follows the reference code in RFC 8032 section 6 closely, so it can be
read against it, and is tested against the section 7.1 vectors. It is not
constant-time. Verification handles only public data, so that does not
matter there. Signing touches the private key, and is here for the
maintainer's own machine (tools/sign_release.py) and for tests, where the only
observer able to time it is the person who already holds the key.
"""
import hashlib

p = 2 ** 255 - 19
L = 2 ** 252 + 27742317777372353535851937790883648493   # order of the base point
d = -121665 * pow(121666, p - 2, p) % p
SQRT_M1 = pow(2, (p - 1) // 4, p)


def _sha512_int(data):
    return int.from_bytes(hashlib.sha512(data).digest(), "little")


# Points are in extended coordinates (X, Y, Z, T), x = X/Z, y = Y/Z, xy = T/Z.
def _add(P, Q):
    A = (P[1] - P[0]) * (Q[1] - Q[0]) % p
    B = (P[1] + P[0]) * (Q[1] + Q[0]) % p
    C = 2 * P[3] * Q[3] * d % p
    D = 2 * P[2] * Q[2] % p
    E, F, G, H = B - A, D - C, D + C, B + A
    return (E * F % p, G * H % p, F * G % p, E * H % p)


def _mul(s, P):
    Q = (0, 1, 1, 0)                       # the neutral element
    while s > 0:
        if s & 1:
            Q = _add(Q, P)
        P = _add(P, P)
        s >>= 1
    return Q


def _equal(P, Q):
    # x1/z1 == x2/z2 and y1/z1 == y2/z2, without dividing.
    if (P[0] * Q[2] - Q[0] * P[2]) % p != 0:
        return False
    return (P[1] * Q[2] - Q[1] * P[2]) % p == 0


def _recover_x(y, sign):
    if y >= p:
        return None
    x2 = (y * y - 1) * pow(d * y * y + 1, p - 2, p)
    if x2 == 0:
        return None if sign else 0
    x = pow(x2, (p + 3) // 8, p)
    if (x * x - x2) % p != 0:
        x = x * SQRT_M1 % p
    if (x * x - x2) % p != 0:
        return None
    if (x & 1) != sign:
        x = p - x
    return x


_GY = 4 * pow(5, p - 2, p) % p
_GX = _recover_x(_GY, 0)
G = (_GX, _GY, 1, _GX * _GY % p)


def _compress(P):
    zinv = pow(P[2], p - 2, p)
    x, y = P[0] * zinv % p, P[1] * zinv % p
    return int.to_bytes(y | ((x & 1) << 255), 32, "little")


def _decompress(s):
    if len(s) != 32:
        return None
    y = int.from_bytes(s, "little")
    sign = y >> 255
    y &= (1 << 255) - 1
    x = _recover_x(y, sign)
    if x is None:
        return None
    return (x, y, 1, x * y % p)


def _expand(secret):
    if len(secret) != 32:
        raise ValueError("an Ed25519 private key is 32 bytes")
    h = hashlib.sha512(secret).digest()
    a = int.from_bytes(h[:32], "little")
    a &= (1 << 254) - 8
    a |= 1 << 254
    return a, h[32:]


def public_key(secret):
    """The 32-byte public key for a 32-byte private key (the RFC's seed)."""
    a, _ = _expand(secret)
    return _compress(_mul(a, G))


def sign(secret, message):
    a, prefix = _expand(secret)
    A = _compress(_mul(a, G))
    r = _sha512_int(prefix + message) % L
    R = _compress(_mul(r, G))
    h = _sha512_int(R + A + message) % L
    s = (r + h * a) % L
    return R + int.to_bytes(s, 32, "little")


def verify(public, message, signature):
    """True only for a valid signature by `public` over `message`.

    Malformed input of any kind is a False rather than an exception: the
    caller's question is "did this key sign this", and garbage is a no.
    Non-canonical encodings are refused (S >= L, y >= p), which RFC 8032
    requires and which keeps one message from having two valid signatures.
    """
    if not isinstance(public, bytes) or not isinstance(signature, bytes):
        return False
    if len(public) != 32 or len(signature) != 64:
        return False
    A = _decompress(public)
    if A is None:
        return False
    R = _decompress(signature[:32])
    if R is None:
        return False
    s = int.from_bytes(signature[32:], "little")
    if s >= L:
        return False
    h = _sha512_int(signature[:32] + public + message) % L
    return _equal(_mul(s, G), _add(R, _mul(h, A)))
