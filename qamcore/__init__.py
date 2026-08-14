"""qamcore -- the wire format.

One copy, shared by tx.py and rx.py. Two copies that had drifted by one
constellation label or one header field would still start up, still lock, and
decode noise.
"""

__version__ = "0.1.0"
