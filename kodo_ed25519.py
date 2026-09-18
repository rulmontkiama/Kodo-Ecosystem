# -*- coding: utf-8 -*-
"""
Kōdo POS - Signature / vérification Ed25519 (RFC 8032) en Python pur.

Aucune dépendance externe : le module est embarqué tel quel dans l'application (PyInstaller) et sert
de racine de confiance pour les mises à jour distantes. Il est volontairement court et sans
optimisation. Validé contre les vecteurs de test de la RFC 8032 et recoupé avec la bibliothèque
`cryptography` (voir tests_patching.py).

La vérification n'a pas besoin d'être en temps constant (données publiques). La signature, elle,
n'est utilisée que sur le poste du développeur (scripts/release/kodo_release.py).
"""

import hashlib

_P = 2 ** 255 - 19
_Q = 2 ** 252 + 27742317777372353535851937790883648493


def _sha512(data: bytes) -> bytes:
    return hashlib.sha512(data).digest()


def _inv(x: int) -> int:
    return pow(x, _P - 2, _P)


_D = -121665 * _inv(121666) % _P
_SQRT_M1 = pow(2, (_P - 1) // 4, _P)


def _add(p1, p2):
    a = (p1[1] - p1[0]) * (p2[1] - p2[0]) % _P
    b = (p1[1] + p1[0]) * (p2[1] + p2[0]) % _P
    c = 2 * p1[3] * p2[3] * _D % _P
    d = 2 * p1[2] * p2[2] % _P
    e, f, g, h = b - a, d - c, d + c, b + a
    return (e * f % _P, g * h % _P, f * g % _P, e * h % _P)


def _mul(s: int, pt):
    q = (0, 1, 1, 0)  # élément neutre
    while s > 0:
        if s & 1:
            q = _add(q, pt)
        pt = _add(pt, pt)
        s >>= 1
    return q


def _equal(p1, p2) -> bool:
    if (p1[0] * p2[2] - p2[0] * p1[2]) % _P != 0:
        return False
    if (p1[1] * p2[2] - p2[1] * p1[2]) % _P != 0:
        return False
    return True


def _recover_x(y: int, sign: int):
    if y >= _P:
        return None
    x2 = (y * y - 1) * _inv(_D * y * y + 1)
    if x2 == 0:
        return None if sign else 0
    x = pow(x2, (_P + 3) // 8, _P)
    if (x * x - x2) % _P != 0:
        x = x * _SQRT_M1 % _P
    if (x * x - x2) % _P != 0:
        return None
    if (x & 1) != sign:
        x = _P - x
    return x


def _compress(pt) -> bytes:
    zinv = _inv(pt[2])
    x = pt[0] * zinv % _P
    y = pt[1] * zinv % _P
    return int.to_bytes(y | ((x & 1) << 255), 32, "little")


def _decompress(s: bytes):
    if len(s) != 32:
        return None
    y = int.from_bytes(s, "little")
    sign = y >> 255
    y &= (1 << 255) - 1
    x = _recover_x(y, sign)
    if x is None:
        return None
    return (x, y, 1, x * y % _P)


_G_Y = 4 * _inv(5) % _P
_G_X = _recover_x(_G_Y, 0)
_G = (_G_X, _G_Y, 1, _G_X * _G_Y % _P)


def _expand(secret: bytes):
    if len(secret) != 32:
        raise ValueError("La clé privée doit faire 32 octets.")
    h = _sha512(secret)
    a = int.from_bytes(h[:32], "little")
    a &= (1 << 254) - 8
    a |= 1 << 254
    return a, h[32:]


def _sha512_modq(data: bytes) -> int:
    return int.from_bytes(_sha512(data), "little") % _Q


def public_key_from_secret(secret: bytes) -> bytes:
    a, _ = _expand(secret)
    return _compress(_mul(a, _G))


def sign(secret: bytes, message: bytes) -> bytes:
    a, prefix = _expand(secret)
    pub = _compress(_mul(a, _G))
    r = _sha512_modq(prefix + message)
    r_enc = _compress(_mul(r, _G))
    h = _sha512_modq(r_enc + pub + message)
    s = (r + h * a) % _Q
    return r_enc + int.to_bytes(s, 32, "little")


def verify(public: bytes, message: bytes, signature: bytes) -> bool:
    """Vrai uniquement si `signature` est une signature Ed25519 valide de `message` par `public`."""
    try:
        if len(public) != 32 or len(signature) != 64:
            return False
        a_pt = _decompress(public)
        if a_pt is None:
            return False
        r_enc = signature[:32]
        r_pt = _decompress(r_enc)
        if r_pt is None:
            return False
        s = int.from_bytes(signature[32:], "little")
        if s >= _Q:
            return False
        h = _sha512_modq(r_enc + public + message)
        return _equal(_mul(s, _G), _add(r_pt, _mul(h, a_pt)))
    except Exception:
        return False
