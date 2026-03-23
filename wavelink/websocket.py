"""
MIT License

Copyright (c) 2019-Current Wyno

Permission is hereby granted, free of charge, to any person obtaining a copy
of this software and associated documentation files (the "Software"), to deal
in the Software without restriction, including without limitation the rights
to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
copies of the Software, and to permit persons to whom the Software is
furnished to do so, subject to the following conditions:

The above copyright notice and this permission notice shall be included in all
copies or substantial portions of the Software.

THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
SOFTWARE.
"""

from __future__ import annotations

import asyncio
import logging
from typing import TYPE_CHECKING, Any

import aiohttp

from . import __version__
from .backoff import Backoff
from .enums import NodeStatus
from .exceptions import AuthorizationFailedException, NodeException
from .payloads import *
from .payloads import NodeDisconnectedEventPayload
from .tracks import Playable


if TYPE_CHECKING:
    from .node import Node
    from .player import Player
    from .types.request import UpdateSessionRequest
    from .types.response import InfoResponse
    from .types.state import PlayerState
    from .types.websocket import TrackExceptionPayload, WebsocketOP


logger: logging.Logger = logging.getLogger(__name__)
LOGGER_TRACK: logging.Logger = logging.getLogger("wavelink.TrackException")


class Websocket:
    def __init__(self, *, node: Node) -> None:
        self.node = node

        self.backoff: Backoff = Backoff()

        self.socket: aiohttp.ClientWebSocketResponse | None = None
        self.keep_alive_task: asyncio.Task[None] | None = None

    @property
    def headers(self) -> dict[str, str]:
        assert self.node.client is not None
        assert self.node.client.user is not None

        data = {
            "Authorization": self.node.password,
            "User-Id": str(self.node.client.user.id),
            "Client-Name": f"Wavelink-Wyno/{__version__}",
        }

        if self.node.session_id:
            data["Session-Id"] = self.node.session_id

        return data

    def is_connected(self) -> bool:
        return self.socket is not None and not self.socket.closed

    async def _update_node(self) -> None:
        if self.node._resume_timeout > 0:
            udata: UpdateSessionRequest = {"resuming": True, "timeout": self.node._resume_timeout}
            await self.node._update_session(data=udata)

        info: InfoResponse = await self.node._fetch_info()
        if "spotify" in info.get("sourceManagers", []):
            self.node._spotify_enabled = True

    async def connect(self) -> None:
        if self.node._status is NodeStatus.CONNECTED:
            payload: NodeDisconnectedEventPayload = NodeDisconnectedEventPayload(node=self.node)
            self.dispatch("node_disconnected", payload)

        self.node._status = NodeStatus.CONNECTING

        if self.keep_alive_task:
            try:
                self.keep_alive_task.cancel()
            except Exception:
                pass

        retries: int | None = self.node._retries
        session: aiohttp.ClientSession = self.node._session
        heartbeat: float = self.node.heartbeat
        uri: str = f"{self.node.uri.removesuffix('/')}/v4/websocket"

        while True:
            try:
                self.socket = await session.ws_connect(url=uri, heartbeat=heartbeat, headers=self.headers)  # type: ignore
            except Exception as e:
                if isinstance(e, aiohttp.WSServerHandshakeError) and e.status == 401:
                    await self.cleanup()
                    raise AuthorizationFailedException("Invalid Lavalink Password") from e
                elif isinstance(e, aiohttp.WSServerHandshakeError) and e.status == 404:
                    await self.cleanup()
                    raise NodeException("Lavalink Endpoint Not Found. Ensure you are running v4.0.0+") from e
                else:
                    logger.warning(f"Connection error on node {self.node}: {e}")

            if self.is_connected():
                self.backoff.reset()
                self.keep_alive_task = asyncio.create_task(self.keep_alive())
                break

            if retries == 0:
                logger.warning(f"Exhausted retries for {self.node}.")
                await self.cleanup()
                break

            if retries is not None:
                retries -= 1

            delay: float = self.backoff.calculate()
            logger.info(f"Retrying connection for {self.node} in {delay} seconds.")
            await asyncio.sleep(delay)

    async def keep_alive(self) -> None:
        assert self.socket is not None

        while True:
            message: aiohttp.WSMessage = await self.socket.receive()

            if message.type in (aiohttp.WSMsgType.CLOSED, aiohttp.WSMsgType.CLOSING):
                asyncio.create_task(self.connect())
                break

            if message.data is None:
                continue

            data: WebsocketOP = message.json()
            await self._handle_payload(data)

    async def _handle_payload(self, data: WebsocketOP) -> None:
        op = data.get("op")

        if op == "ready":
            resumed: bool = data["resumed"]
            session_id: str = data["sessionId"]

            self.node._status = NodeStatus.CONNECTED
            self.node._session_id = session_id

            await self._update_node()

            ready_payload: NodeReadyEventPayload = NodeReadyEventPayload(
                node=self.node, resumed=resumed, session_id=session_id
            )
            self.dispatch("node_ready", ready_payload)

        elif op == "playerUpdate":
            playerup: Player | None = self.get_player(data["guildId"])
            state: PlayerState = data["state"]

            updatepayload: PlayerUpdateEventPayload = PlayerUpdateEventPayload(player=playerup, state=state)
            self.dispatch("player_update", updatepayload)

            if playerup:
                asyncio.create_task(playerup._update_event(updatepayload))

        elif op == "stats":
            statspayload: StatsEventPayload = StatsEventPayload(data=data)
            self.node._total_player_count = statspayload.players
            self.dispatch("stats_update", statspayload)

        elif op == "event":
            player: Player | None = self.get_player(data["guildId"])

            if data["type"] == "TrackStartEvent":
                track: Playable = Playable(data["track"])
                startpayload: TrackStartEventPayload = TrackStartEventPayload(player=player, track=track)
                self.dispatch("track_start", startpayload)

                if player:
                    asyncio.create_task(player._track_start(startpayload))

            elif data["type"] == "TrackEndEvent":
                track: Playable = Playable(data["track"])
                reason: str = data["reason"]

                if player and reason != "replaced":
                    player._current = None

                endpayload: TrackEndEventPayload = TrackEndEventPayload(player=player, track=track, reason=reason)
                self.dispatch("track_end", endpayload)

                if player:
                    asyncio.create_task(player._auto_play_event(endpayload))

            elif data["type"] == "TrackExceptionEvent":
                track: Playable = Playable(data["track"])
                exception: TrackExceptionPayload = data["exception"]

                excpayload: TrackExceptionEventPayload = TrackExceptionEventPayload(
                    player=player, track=track, exception=exception
                )

                LOGGER_TRACK.error(
                    f"A Lavalink TrackException was received on {self.node} for player {player}: {exception.get('message', '')}, caused by: {exception.get('cause')}"
                )
                self.dispatch("track_exception", excpayload)

            elif data["type"] == "TrackStuckEvent":
                track: Playable = Playable(data["track"])
                threshold: int = data["thresholdMs"]

                stuckpayload: TrackStuckEventPayload = TrackStuckEventPayload(
                    player=player, track=track, threshold=threshold
                )
                self.dispatch("track_stuck", stuckpayload)

            elif data["type"] == "WebSocketClosedEvent":
                code: int = data["code"]
                reason: str = data["reason"]
                by_remote: bool = data["byRemote"]

                wcpayload: WebsocketClosedEventPayload = WebsocketClosedEventPayload(
                    player=player, code=code, reason=reason, by_remote=by_remote
                )
                self.dispatch("websocket_closed", wcpayload)

                if player:
                    asyncio.create_task(player._disconnected_wait(code, by_remote))

            else:
                other_payload: ExtraEventPayload = ExtraEventPayload(node=self.node, player=player, data=data)
                self.dispatch("extra_event", other_payload)

    def get_player(self, guild_id: str | int) -> Player | None:
        return self.node.get_player(int(guild_id))

    def dispatch(self, event: str, /, *args: Any, **kwargs: Any) -> None:
        assert self.node.client is not None
        self.node.client.dispatch(f"wavelink_{event}", *args, **kwargs)

    async def cleanup(self) -> None:
        if self.keep_alive_task:
            try:
                self.keep_alive_task.cancel()
            except Exception:
                pass

        if self.socket:
            try:
                await self.socket.close()
            except Exception:
                pass

        self.node._status = NodeStatus.DISCONNECTED
        self.node._session_id = None
        self.node._players = {}
        self.node._websocket = None

        payload: NodeDisconnectedEventPayload = NodeDisconnectedEventPayload(node=self.node)
        self.dispatch("node_disconnected", payload)