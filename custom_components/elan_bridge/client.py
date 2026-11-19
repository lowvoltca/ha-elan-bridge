"""One connection to the HA Bridge driver on the ELAN controller: reconnects, keepalive, line in/line out.

On every (re)connect `on_connect` runs, so the caller can send HELLO, the device list and current states.
No Home Assistant imports here.
"""

from __future__ import annotations

import asyncio
import logging
import time
from typing import Awaitable, Callable, Optional

from . import protocol as p

_LOGGER = logging.getLogger(__name__)

PING_SECONDS = 60          # the gSC10 is fragile: keep traffic low
PONG_TIMEOUT = 20
RECONNECT_SECONDS = (5, 15, 30, 60, 120, 300)   # never a tight loop: ELAN may be restarting


class BridgeClient:
    def __init__(
        self,
        host: str,
        port: int = p.PORT,
        on_command: Callable[[p.Command], None] = lambda cmd: None,
        on_connect: Callable[[], Awaitable[None]] = None,
        on_connection: Callable[[bool], None] = lambda up: None,
    ) -> None:
        self.host, self.port = host, port
        self.on_command, self.on_connect, self.on_connection = on_command, on_connect, on_connection
        self.connected = False
        self._writer: Optional[asyncio.StreamWriter] = None
        self._reader: Optional[asyncio.StreamReader] = None
        self._task: Optional[asyncio.Task] = None
        self._closing = False
        self._last_rx = 0.0

    async def start(self) -> None:
        self._task = asyncio.ensure_future(self._run())

    async def close(self) -> None:
        self._closing = True
        if self._task:
            self._task.cancel()
        await self._drop()

    async def send(self, line: str) -> bool:
        if self._writer is None:
            return False
        try:
            self._writer.write((line + "\n").encode("utf-8"))
            await self._writer.drain()
            _LOGGER.debug(">> %s", line)
            return True
        except OSError as err:
            _LOGGER.debug("Send failed: %s", err)
            self._writer.close()      # the read loop ends and we reconnect
            return False

    async def _run(self) -> None:
        attempt = 0
        while not self._closing:
            try:
                self._reader, self._writer = await asyncio.wait_for(
                    asyncio.open_connection(self.host, self.port), 10)
            except (OSError, asyncio.TimeoutError) as err:
                delay = RECONNECT_SECONDS[min(attempt, len(RECONNECT_SECONDS) - 1)]
                _LOGGER.debug("ELAN bridge %s:%s not reachable (%s); retrying in %ss",
                              self.host, self.port, err, delay)
                attempt += 1
                await asyncio.sleep(delay)
                continue
            attempt = 0
            self._last_rx = time.monotonic()
            self._set_connected(True)
            keepalive = asyncio.ensure_future(self._keepalive())
            try:
                if self.on_connect:
                    await self.on_connect()
                await self._read()
            except Exception:      # a bug in a handler must not stop reconnecting
                _LOGGER.exception("ELAN bridge connection failed")
            finally:
                keepalive.cancel()
                await self._drop()
            if not self._closing:
                await asyncio.sleep(RECONNECT_SECONDS[0])

    async def _read(self) -> None:
        lines = p.LineBuffer()
        while True:
            data = await self._reader.read(1024)
            if not data:
                _LOGGER.debug("ELAN closed the bridge connection")
                return
            self._last_rx = time.monotonic()
            for line in lines.feed(data):
                _LOGGER.debug("<< %s", line)
                cmd = p.parse(line)
                if cmd is None:
                    _LOGGER.debug("Ignored line from ELAN: %r", line)
                elif cmd.verb == "ERR":
                    _LOGGER.warning("ELAN bridge driver: %s", cmd.value)
                elif cmd.verb not in ("HELLO", "PONG"):
                    self.on_command(cmd)

    async def _keepalive(self) -> None:
        while True:
            await asyncio.sleep(PING_SECONDS)
            await self.send(p.ping())
            await asyncio.sleep(PONG_TIMEOUT)
            if time.monotonic() - self._last_rx > PING_SECONDS + PONG_TIMEOUT:
                _LOGGER.debug("No answer from ELAN; reconnecting")
                if self._writer is not None:
                    self._writer.close()
                return

    async def _drop(self) -> None:
        if self._writer is not None:
            try:
                self._writer.close()
            except Exception:
                pass
        self._reader = self._writer = None
        self._set_connected(False)

    def _set_connected(self, up: bool) -> None:
        if up != self.connected:
            self.connected = up
            self.on_connection(up)
