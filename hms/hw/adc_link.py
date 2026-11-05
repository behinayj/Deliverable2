"""1.1.1 ADC Link: move raw messages between the ADC and the HMS.

Secret: TCP sockets on port 5050, one UTF-8 JSON object per line, connection handling.
"""
import queue
import socket
import threading
from abc import ABC, abstractmethod
from dataclasses import dataclass
from datetime import datetime

ChannelId = int
_MAX_LINE = 64 * 1024


@dataclass(frozen=True)
class RawFrame:
    data: bytes
    arrived_at: datetime
    channel: ChannelId


class LinkStartError(Exception):
    pass


class AdcLink(ABC):
    @abstractmethod
    def start(self) -> None: ...

    @abstractmethod
    def next_message(self) -> RawFrame | None: ...

    @abstractmethod
    def send(self, channel: ChannelId, raw: bytes) -> None: ...


class TcpAdcLink(AdcLink):
    def __init__(self, clock, port: int = 5050, host: str = "0.0.0.0"):
        self._clock = clock
        self._addr = (host, port)
        self._queue: queue.Queue[RawFrame] = queue.Queue()
        self._conns: dict[ChannelId, socket.socket] = {}
        self._lock = threading.Lock()
        self._next_channel = 1
        self._started = False

    def start(self) -> None:
        if self._started:  # RESTART re-runs the self-check; the listener is already up
            return
        try:
            srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            srv.bind(self._addr)
            srv.listen()
        except OSError as e:
            raise LinkStartError(str(e)) from e
        self._started = True
        threading.Thread(target=self._accept_loop, args=(srv,), daemon=True).start()

    def next_message(self) -> RawFrame | None:
        try:
            return self._queue.get_nowait()
        except queue.Empty:
            return None

    def send(self, channel: ChannelId, raw: bytes) -> None:
        with self._lock:
            conn = self._conns.get(channel)
        if conn is None:
            return
        try:
            conn.sendall(raw + b"\n")
        except OSError:
            pass

    def _accept_loop(self, srv):
        while True:
            conn, _ = srv.accept()
            with self._lock:
                ch = self._next_channel
                self._next_channel += 1
                self._conns[ch] = conn
            threading.Thread(target=self._read_loop, args=(conn, ch), daemon=True).start()

    def _read_loop(self, conn, ch):
        buf = b""
        try:
            while True:
                chunk = conn.recv(4096)
                if not chunk:
                    break
                buf += chunk
                while b"\n" in buf:
                    line, buf = buf.split(b"\n", 1)
                    if line.strip():
                        self._queue.put(RawFrame(line[:_MAX_LINE], self._clock.now(), ch))
                if len(buf) > _MAX_LINE:
                    buf = b""
        except OSError:
            pass
        finally:
            with self._lock:
                self._conns.pop(ch, None)
            conn.close()
