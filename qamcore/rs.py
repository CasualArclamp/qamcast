"""Reed-Solomon over GF(256), systematic, errors-only.

RS is the outer code. Its job is not raw coding gain -- the convolutional
inner code provides that -- but cleaning up after it. A Viterbi decoder that
loses the trellis does not emit scattered single-bit errors, it emits a short
burst of them, and a burst is exactly what a byte-oriented block code handles
best. That division of labour is why concatenation works.

Two shortenings are in use, chosen per MODCOD in profiles.py:

    RS(255,223), t=16   the rugged half of the ladder
    RS(255,239), t=8    64QAM 5/6 and above, where the 6.3% saved is what
                        lets the top rung clear 192 kbps

Generator roots are alpha^1 .. alpha^2t (first consecutive root = 1), and the
field polynomial is 0x11D. Both are wire format.
"""

from __future__ import annotations

import functools

import numpy as np
from numba import njit

N = 255
PRIM = 0x11D
FCR = 1  # first consecutive root exponent


@functools.lru_cache(maxsize=None)
def _tables() -> tuple[np.ndarray, np.ndarray]:
    """(exp, log) for GF(256). ``exp`` is doubled to 512 entries so that
    ``exp[log[a] + log[b]]`` needs no modulo in the inner loop."""
    exp = np.zeros(512, dtype=np.int64)
    log = np.zeros(256, dtype=np.int64)
    x = 1
    for i in range(255):
        exp[i] = x
        log[x] = i
        x <<= 1
        if x & 0x100:
            x ^= PRIM
    exp[255:510] = exp[0:255]
    exp[510] = exp[0]
    exp[511] = exp[1]
    return exp, log


@njit(cache=True, inline="always")
def _mul(a, b, exp, log):
    if a == 0 or b == 0:
        return 0
    return exp[log[a] + log[b]]


@njit(cache=True, inline="always")
def _div(a, b, exp, log):
    if a == 0:
        return 0
    return exp[log[a] - log[b] + 255]


@functools.lru_cache(maxsize=None)
def generator(nparity: int) -> np.ndarray:
    """Generator polynomial, monic, highest power first."""
    exp, log = _tables()
    g = np.zeros(nparity + 1, dtype=np.int64)
    g[0] = 1
    glen = 1
    for i in range(nparity):
        root = exp[i + FCR]
        # multiply g by (x + root)
        new = np.zeros(glen + 1, dtype=np.int64)
        for j in range(glen):
            new[j] ^= g[j]
            if g[j] and root:
                new[j + 1] ^= exp[log[g[j]] + log[root]]
        g[:glen + 1] = new
        glen += 1
    return g


@njit(cache=True)
def _encode(msg, g, nparity, exp, log):
    k = msg.shape[0]
    par = np.zeros(nparity, dtype=np.int64)
    for i in range(k):
        fb = msg[i] ^ par[0]
        for j in range(nparity - 1):
            par[j] = par[j + 1] ^ _mul(fb, g[j + 1], exp, log)
        par[nparity - 1] = _mul(fb, g[nparity], exp, log)
    return par


def encode(message: np.ndarray, k: int) -> np.ndarray:
    """Systematic RS encode. ``message`` is ``k`` bytes, result is 255."""
    message = np.asarray(message, dtype=np.uint8).ravel()
    if len(message) != k:
        raise ValueError(f"expected {k} message bytes, got {len(message)}")
    nparity = N - k
    exp, log = _tables()
    par = _encode(message.astype(np.int64), generator(nparity), nparity, exp, log)
    return np.concatenate([message, par.astype(np.uint8)])


@functools.lru_cache(maxsize=None)
def _syndrome_tables(nparity: int) -> np.ndarray:
    """Multiply-by-alpha^(j+FCR) as a lookup, one row per syndrome.

    The syndromes are the codeword evaluated at ``nparity`` fixed points, and
    each evaluation is a Horner chain whose multiplier never changes. A general
    GF multiply costs two log lookups, an add, an exp lookup and two branches
    for the zero cases; against a *constant* it is one lookup, and the constant
    is known before any data arrives.
    """
    exp, log = _tables()
    tab = np.zeros((nparity, 256), dtype=np.int64)
    for j in range(nparity):
        a = exp[j + FCR]                 # a power of alpha, never zero
        for s in range(1, 256):
            tab[j, s] = exp[log[s] + log[a]]
    return tab


@njit(cache=True)
def _syndromes(r, nparity, tab):
    """Horner evaluation at each syndrome point.

    This is the whole cost of decoding an undamaged codeword -- the common
    case by a wide margin, since it runs on every codeword and most of them are
    clean -- so it is worth having the inner step be a load and an xor and
    nothing else.
    """
    syn = np.zeros(nparity, dtype=np.int64)
    for j in range(nparity):
        row = tab[j]
        s = 0
        for i in range(r.shape[0]):
            s = row[s] ^ r[i]
        syn[j] = s
    return syn


@njit(cache=True)
def _berlekamp_massey(syn, nparity, exp, log):
    C = np.zeros(nparity + 1, dtype=np.int64)
    B = np.zeros(nparity + 1, dtype=np.int64)
    T = np.zeros(nparity + 1, dtype=np.int64)
    C[0] = 1
    B[0] = 1
    L = 0
    m = 1
    b = 1
    for n in range(nparity):
        d = syn[n]
        for i in range(1, L + 1):
            d ^= _mul(C[i], syn[n - i], exp, log)
        if d == 0:
            m += 1
        elif 2 * L <= n:
            for i in range(nparity + 1):
                T[i] = C[i]
            coef = _div(d, b, exp, log)
            for i in range(nparity + 1 - m):
                C[i + m] ^= _mul(coef, B[i], exp, log)
            L = n + 1 - L
            for i in range(nparity + 1):
                B[i] = T[i]
            b = d
            m = 1
        else:
            coef = _div(d, b, exp, log)
            for i in range(nparity + 1 - m):
                C[i + m] ^= _mul(coef, B[i], exp, log)
            m += 1
    return C, L


@njit(cache=True)
def _decode(r, nparity, exp, log, tab):
    """Returns (corrected, n_errors). n_errors is -1 if uncorrectable."""
    n = r.shape[0]
    syn = _syndromes(r, nparity, tab)

    clean = True
    for j in range(nparity):
        if syn[j] != 0:
            clean = False
            break
    if clean:
        return r, 0

    C, L = _berlekamp_massey(syn, nparity, exp, log)
    t = nparity // 2
    if L > t:
        return r, -1

    # Chien search: roots of C(x) are alpha^-i for error positions i.
    positions = np.empty(L, dtype=np.int64)
    found = 0
    for i in range(n):
        # evaluate C at alpha^-i
        acc = 0
        xinv = 255 - (i % 255)
        for j in range(L + 1):
            if C[j] != 0:
                acc ^= exp[log[C[j]] + (j * xinv) % 255]
        if acc == 0:
            if found < L:
                positions[found] = i
            found += 1
    if found != L:
        return r, -1

    # Omega(x) = S(x) * C(x) mod x^nparity
    omega = np.zeros(nparity, dtype=np.int64)
    for i in range(nparity):
        acc = 0
        for j in range(i + 1):
            if j <= nparity and C[j] != 0 and syn[i - j] != 0:
                acc ^= _mul(C[j], syn[i - j], exp, log)
        omega[i] = acc

    out = r.copy()
    for e in range(L):
        i = positions[e]
        xinv = 255 - (i % 255)
        # Omega(alpha^-i)
        num = 0
        for j in range(nparity):
            if omega[j] != 0:
                num ^= exp[log[omega[j]] + (j * xinv) % 255]
        # C'(alpha^-i): in GF(2) only odd-index terms survive
        den = 0
        for j in range(1, L + 1, 2):
            if C[j] != 0:
                den ^= exp[log[C[j]] + ((j - 1) * xinv) % 255]
        if den == 0:
            return r, -1
        mag = _div(num, den, exp, log)
        if FCR != 1:
            mag = _mul(mag, exp[(i * (1 - FCR)) % 255], exp, log)
        # position i counted from the highest power -> index n-1-i
        idx = n - 1 - i
        if idx < 0 or idx >= n:
            return r, -1
        out[idx] ^= mag

    # Verify: a corrected codeword must have zero syndromes. Without this a
    # codeword with more than t errors can decode to a plausible-looking wrong
    # answer, and silently wrong audio is worse than a muted frame.
    check = _syndromes(out, nparity, tab)
    for j in range(nparity):
        if check[j] != 0:
            return r, -1
    return out, L


def decode(codeword: np.ndarray, k: int) -> tuple[np.ndarray, int]:
    """Decode 255 bytes to ``k`` message bytes.

    Returns ``(message, n_corrected)`` where ``n_corrected`` is -1 if the
    codeword could not be corrected -- in which case the message is returned
    uncorrected and the caller should treat it as lost.
    """
    codeword = np.asarray(codeword, dtype=np.uint8).ravel()
    if len(codeword) != N:
        raise ValueError(f"expected {N} codeword bytes, got {len(codeword)}")
    nparity = N - k
    exp, log = _tables()
    out, nerr = _decode(codeword.astype(np.int64), nparity, exp, log,
                        _syndrome_tables(nparity))
    return out[:k].astype(np.uint8), int(nerr)


def encode_stream(data: np.ndarray, k: int) -> np.ndarray:
    """Encode a whole number of ``k``-byte blocks into 255-byte codewords."""
    data = np.asarray(data, dtype=np.uint8).ravel()
    if len(data) % k:
        raise ValueError(f"{len(data)} bytes is not a multiple of k={k}")
    blocks = len(data) // k
    out = np.empty(blocks * N, dtype=np.uint8)
    for b in range(blocks):
        out[b * N:(b + 1) * N] = encode(data[b * k:(b + 1) * k], k)
    return out


def decode_stream(data: np.ndarray, k: int) -> tuple[np.ndarray, int, int]:
    """Decode codewords back to messages.

    Returns ``(messages, corrected_bytes, failed_codewords)``. The failure
    count is what the receiver displays as block error rate.
    """
    data = np.asarray(data, dtype=np.uint8).ravel()
    if len(data) % N:
        raise ValueError(f"{len(data)} bytes is not a multiple of n={N}")
    blocks = len(data) // N
    out = np.empty(blocks * k, dtype=np.uint8)
    corrected = 0
    failed = 0
    for b in range(blocks):
        msg, nerr = decode(data[b * N:(b + 1) * N], k)
        out[b * k:(b + 1) * k] = msg
        if nerr < 0:
            failed += 1
        else:
            corrected += nerr
    return out, corrected, failed
