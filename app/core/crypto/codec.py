"""URL-safe base64 for everything a wrapper or a payload stores as text.

Its own module because three others need it and none of them owns it: the
wrapper, the payload envelope and the blind-write envelope each keep a random
salt, a nonce or a key inside a column that is text.
"""

import base64


def encode_b64(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).decode("ascii")


def decode_b64(value: str) -> bytes:
    try:
        return base64.urlsafe_b64decode(value.encode("ascii"))
    except Exception as error:
        raise ValueError(f"Invalid base64url value: {error}") from error
