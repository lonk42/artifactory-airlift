"""Adapters between httpx streaming responses and file-like readers."""

from __future__ import annotations

import io
from typing import IO

import httpx


class ResponseReader(io.RawIOBase):
    """Expose a streaming httpx response as a blocking binary reader.

    ``tarfile.addfile`` wants an object with ``read(n)``, while httpx offers an
    iterator of chunks. Bridging the two lets a blob stream from object storage
    straight into the archive without ever being buffered whole in memory or
    staged on the spool volume.
    """

    def __init__(self, response: httpx.Response) -> None:
        self._response = response
        self._chunks = response.iter_bytes()
        self._buf = b""

    def readable(self) -> bool:
        return True

    def read(self, size: int | None = -1) -> bytes:
        if size is None or size < 0:
            rest = b"".join(self._chunks)
            out, self._buf = self._buf + rest, b""
            return out
        while len(self._buf) < size:
            try:
                self._buf += next(self._chunks)
            except StopIteration:
                break
        out, self._buf = self._buf[:size], self._buf[size:]
        return out

    def readinto(self, buffer) -> int:  # pragma: no cover - exercised via read()
        data = self.read(len(buffer))
        buffer[: len(data)] = data
        return len(data)

    def close(self) -> None:
        try:
            self._response.close()
        finally:
            super().close()


class RangeReader(io.RawIOBase):
    """Read a fixed byte window of an open file, for upload chunking.

    Handing httpx a bounded view avoids slurping a multi-gigabyte part into
    memory just to give the request a body.
    """

    def __init__(self, fh: IO[bytes], length: int) -> None:
        self._fh = fh
        self._remaining = length

    def readable(self) -> bool:
        return True

    def read(self, size: int | None = -1) -> bytes:
        if self._remaining <= 0:
            return b""
        want = self._remaining if size is None or size < 0 else min(size, self._remaining)
        data = self._fh.read(want)
        self._remaining -= len(data)
        return data

    def close(self) -> None:
        # The caller owns the underlying handle; only the window ends here.
        super().close()
