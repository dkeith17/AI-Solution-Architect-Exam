"""Incremental Server-Sent Events parser (WHATWG HTML "event stream" rules). Stdlib only.

Network chunks can split anywhere: mid-line, between CR and LF, or inside a
multi-byte UTF-8 character. ``SSEParser.feed`` accepts arbitrary byte chunks and
returns only fully dispatched events; memory is bounded by the largest single event.
"""

from __future__ import annotations

import codecs
from dataclasses import dataclass


@dataclass(frozen=True)
class SSEEvent:
    event: str
    data: str
    id: str | None = None


class SSEParser:
    def __init__(self, max_event_bytes: int = 1024 * 1024) -> None:
        self._decoder = codecs.getincrementaldecoder("utf-8")(errors="strict")
        self._buf = ""
        self._data: list[str] = []
        self._event = ""
        self._id: str | None = None
        self._first = True
        self._max = max_event_bytes
        self._size = 0

    def feed(self, chunk: bytes) -> list[SSEEvent]:
        text = self._decoder.decode(chunk)
        if self._first and text:
            if text.startswith("\ufeff"):
                text = text[1:]
            self._first = False
        self._buf += text
        return self._drain(final=False)

    def close(self) -> list[SSEEvent]:
        """Flush at end of stream. An unterminated trailing event is discarded per spec."""
        self._buf += self._decoder.decode(b"", final=True)
        events = self._drain(final=True)
        self._data, self._event, self._size = [], "", 0
        return events

    def _drain(self, final: bool) -> list[SSEEvent]:
        events: list[SSEEvent] = []
        buf = self._buf
        start = 0
        n = len(buf)
        while True:
            i = start
            while i < n and buf[i] not in "\r\n":
                i += 1
            if i >= n:
                break
            if buf[i] == "\r" and i + 1 >= n and not final:
                break  # CR at chunk end: the LF of a CRLF may arrive next
            line = buf[start:i]
            start = i + 2 if (buf[i] == "\r" and i + 1 < n and buf[i + 1] == "\n") else i + 1
            ev = self._line(line)
            if ev is not None:
                events.append(ev)
        self._buf = buf[start:]
        if len(self._buf) > self._max:
            raise ValueError("SSE line exceeds maximum size")
        return events

    def _line(self, line: str) -> SSEEvent | None:
        if line == "":
            if not self._data:
                self._event = ""
                return None
            ev = SSEEvent(event=self._event or "message", data="\n".join(self._data), id=self._id)
            self._data, self._event, self._size = [], "", 0
            return ev
        if line.startswith(":"):
            return None
        name, sep, value = line.partition(":")
        if sep and value.startswith(" "):
            value = value[1:]
        if name == "data":
            self._size += len(value)
            if self._size > self._max:
                raise ValueError("SSE event exceeds maximum size")
            self._data.append(value)
        elif name == "event":
            self._event = value
        elif name == "id" and "\x00" not in value:
            self._id = value
        return None
