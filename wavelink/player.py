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
import random
import time
from typing import TYPE_CHECKING, Any, TypeAlias

import async_timeout
import discord
from discord.abc import Connectable
from discord.utils import MISSING

import wavelink

from .enums import AutoPlayMode, NodeStatus, QueueMode
from .exceptions import (
    ChannelTimeoutException,
    InvalidChannelStateException,
    InvalidNodeException,
    LavalinkException,
    LavalinkLoadException,
    QueueEmpty,
)
from .filters import Filters
from .node import Pool
from .payloads import (
    PlayerUpdateEventPayload,
    TrackEndEventPayload,
    TrackStartEventPayload,
)
from .queue import Queue
from .tracks import Playable, Playlist


if TYPE_CHECKING:
    from collections import deque

    from discord.types.voice import (
        GuildVoiceState as GuildVoiceStatePayload,
        VoiceServerUpdate as VoiceServerUpdatePayload,
    )
    from typing_extensions import Self

    from .node import Node
    from .types.request import Request as RequestPayload
    from .types.state import PlayerBasicState, PlayerVoiceState, VoiceState

    VocalGuildChannel = discord.VoiceChannel | discord.StageChannel

logger: logging.Logger = logging.getLogger(__name__)


T_a: TypeAlias = list[Playable] | Playlist


class Player(discord.VoiceProtocol):
    channel: VocalGuildChannel

    def __call__(self, client: discord.Client, channel: VocalGuildChannel) -> Self:
        super().__init__(client, channel)
        self._guild = channel.guild
        return self

    def __init__(
        self, client: discord.Client = MISSING, channel: Connectable = MISSING, *, nodes: list[Node] | None = None
    ) -> None:
        super().__init__(client, channel)

        self.client: discord.Client = client
        self._guild: discord.Guild | None = None

        self._voice_state: PlayerVoiceState = {"voice": {}}

        self._node: Node
        if not nodes:
            self._node = Pool.get_node()
        else:
            self._node = sorted(nodes, key=lambda n: len(n.players))[0]

        if self.client is MISSING and self.node.client:
            self.client = self.node.client

        self._last_update: int | None = None
        self._last_position: int = 0
        self._ping: int = -1

        self._connected: bool = False
        self._connection_event: asyncio.Event = asyncio.Event()
        self._pending_voice_dispatch: bool = False

        self._current: Playable | None = None
        self._original: Playable | None = None
        self._previous: Playable | None = None

        self.queue: Queue = Queue()
        self.auto_queue: Queue = Queue()

        self._volume: int = 100
        self._paused: bool = False

        self._auto_cutoff: int = 20
        self._auto_weight: int = 3
        self._previous_seeds_cutoff: int = self._auto_cutoff * self._auto_weight
        self._history_count: int | None = None

        self._autoplay: AutoPlayMode = AutoPlayMode.disabled
        self.__previous_seeds: asyncio.Queue[str] = asyncio.Queue(maxsize=self._previous_seeds_cutoff)

        self._auto_lock: asyncio.Lock = asyncio.Lock()
        self._error_count: int = 0

        self._inactive_channel_limit: int | None = self._node._inactive_channel_tokens
        self._inactive_channel_count: int = self._inactive_channel_limit if self._inactive_channel_limit else 0

        self._filters: Filters = Filters()

        self._inactivity_task: asyncio.Task[bool] | None = None
        self._inactivity_wait: int | None = self._node._inactive_player_timeout

        self._should_wait: int = 10
        self._reconnecting: asyncio.Event = asyncio.Event()
        self._reconnecting.set()

    async def _disconnected_wait(self, code: int, by_remote: bool) -> None:
        if code != 4014 or not by_remote:
            return

        self._connected = False
        await self._reconnecting.wait()

        # Grace period for voice migration/DAVE renegotiation to settle.
        # Multiple WebSocketClosedEvents can fire during channel moves;
        # this prevents premature destruction while the connection stabilizes.
        await asyncio.sleep(5)

        if self._connected:
            return

        await self._destroy()

    def _inactivity_task_callback(self, task: asyncio.Task[bool]) -> None:
        cancelled: bool = False

        try:
            result: bool = task.result()
        except asyncio.CancelledError:
            cancelled = True
            result = False

        if cancelled or result is False:
            return

        if result is not True:
            return

        if not self._guild:
            return

        if self.playing:
            return

        self.client.dispatch("wavelink_inactive_player", self)

    async def _inactivity_runner(self, wait: int) -> bool:
        try:
            await asyncio.sleep(wait)
        except asyncio.CancelledError:
            return False

        return True

    def _inactivity_cancel(self) -> None:
        if self._inactivity_task:
            try:
                self._inactivity_task.cancel()
            except Exception:
                pass

        self._inactivity_task = None

    def _inactivity_start(self) -> None:
        if self._inactivity_wait is not None and self._inactivity_wait > 0:
            self._inactivity_task = asyncio.create_task(self._inactivity_runner(self._inactivity_wait))
            self._inactivity_task.add_done_callback(self._inactivity_task_callback)

    async def _track_start(self, payload: TrackStartEventPayload) -> None:
        self._inactivity_cancel()

    async def _auto_play_event(self, payload: TrackEndEventPayload) -> None:
        if not self.channel:
            return

        members: int = len([m for m in self.channel.members if not m.bot])
        self._inactive_channel_count = (
            self._inactive_channel_count - 1 if not members else self._inactive_channel_limit or 0
        )

        if self._inactive_channel_limit and self._inactive_channel_count <= 0:
            self._inactive_channel_count = self._inactive_channel_limit
            self._inactivity_cancel()
            self.client.dispatch("wavelink_inactive_player", self)

        elif self._autoplay is AutoPlayMode.disabled:
            self._inactivity_start()
            return

        if self._error_count >= 3:
            logger.warning(
                "AutoPlay was unable to continue as you have received too many consecutive errors."
            )
            self._inactivity_start()
            return

        if payload.reason == "replaced":
            self._error_count = 0
            return
        elif payload.reason == "loadFailed":
            self._error_count += 1
        else:
            self._error_count = 0

        if self.node.status is not NodeStatus.CONNECTED:
            return

        if not isinstance(self.queue, Queue) or not isinstance(self.auto_queue, Queue):
            self._inactivity_start()
            return

        if self.queue.mode is QueueMode.loop:
            await self._do_partial(history=False)
        elif self.queue.mode is QueueMode.loop_all or (self._autoplay is AutoPlayMode.partial or self.queue):
            await self._do_partial()
        elif self._autoplay is AutoPlayMode.enabled:
            async with self._auto_lock:
                await self._do_recommendation()

    async def _do_partial(self, *, history: bool = True) -> None:
        self._inactivity_start()

        if self._current is None:
            try:
                track: Playable = self.queue.get()
            except QueueEmpty:
                return

            await self.play(track, add_history=history)

    async def _do_recommendation(
        self,
        *,
        populate_track: wavelink.Playable | None = None,
        max_population: int | None = None,
    ) -> None:
        assert self.guild is not None
        assert self.queue.history is not None and self.auto_queue.history is not None

        max_population_: int = max_population if max_population else self._auto_cutoff

        if len(self.auto_queue) > self._auto_cutoff + 1 and not populate_track:
            self._inactivity_start()

            track: Playable = self.auto_queue.get()
            self.auto_queue.history.put(track)

            await self.play(track, add_history=False)
            return

        weighted_history: list[Playable] = self.queue.history[::-1][: max(5, 5 * self._auto_weight)]
        weighted_upcoming: list[Playable] = self.auto_queue[: max(3, int((5 * self._auto_weight) / 3))]
        choices: list[Playable | None] = [*weighted_history, *weighted_upcoming, self._current, self._previous]

        _previous: deque[str] = self.__previous_seeds._queue  # type: ignore
        seeds: list[Playable] = [t for t in choices if t is not None and t.identifier not in _previous]
        random.shuffle(seeds)

        if populate_track:
            seeds.insert(0, populate_track)

        spotify: list[str] = [t.identifier for t in seeds if t.source == "spotify"]
        youtube: list[str] = [t.identifier for t in seeds if t.source == "youtube"]

        spotify_query: str | None = None
        youtube_query: str | None = None

        count: int = len(self.queue.history)
        changed_by: int = min(3, count) if self._history_count is None else count - self._history_count

        if changed_by > 0:
            self._history_count = count

        changed_history: list[Playable] = self.queue.history[::-1]

        added: int = 0
        for i in range(min(changed_by, 3)):
            track: Playable = changed_history[i]

            if added == 2 and track.source == "spotify":
                break

            if track.source == "spotify":
                spotify.insert(0, track.identifier)
                added += 1

            elif track.source == "youtube":
                youtube[0] = track.identifier

        if spotify:
            spotify_seeds: list[str] = spotify[:3]
            spotify_query = f"sprec:seed_tracks={','.join(spotify_seeds)}&limit=10"

            for s_seed in spotify_seeds:
                self._add_to_previous_seeds(s_seed)

        if youtube:
            ytm_seed: str = youtube[0]
            youtube_query = f"https://music.youtube.com/watch?v={ytm_seed}8&list=RD{ytm_seed}"
            self._add_to_previous_seeds(ytm_seed)

        async def _search(query: str | None) -> T_a:
            if query is None:
                return []

            try:
                search: wavelink.Search = await Pool.fetch_tracks(query, node=self._node)
            except (LavalinkLoadException, LavalinkException):
                return []

            if not search:
                return []

            tracks: list[Playable] = search.tracks.copy() if isinstance(search, Playlist) else search
            return tracks

        results: tuple[T_a, T_a] = await asyncio.gather(_search(spotify_query), _search(youtube_query))
        filtered_r: list[Playable] = [t for r in results for t in r]

        if not filtered_r and not self.auto_queue:
            self._inactivity_start()
            return

        history: list[Playable] = (
            self.auto_queue[:40] + self.queue[:40] + self.queue.history[:-41:-1] + self.auto_queue.history[:-61:-1]
        )

        added: int = 0

        random.shuffle(filtered_r)
        for track in filtered_r:
            if track in history:
                continue

            track._recommended = True
            added += await self.auto_queue.put_wait(track)

            if added >= max_population_:
                break

        if not self._current and not populate_track:
            try:
                now: Playable = self.auto_queue.get()
                self.auto_queue.history.put(now)

                await self.play(now, add_history=False)
            except wavelink.QueueEmpty:
                self._inactivity_start()

    @property
    def state(self) -> PlayerBasicState:
        data: PlayerBasicState = {
            "voice_state": self._voice_state.copy(),
            "position": self.position,
            "connected": self.connected,
            "current": self.current,
            "paused": self.paused,
            "volume": self.volume,
            "filters": self.filters,
        }
        return data

    async def switch_node(self, new_node: wavelink.Node, /) -> None:
        assert self._guild

        if new_node.identifier == self.node.identifier:
            raise InvalidNodeException(f"Player '{self._guild.id}' current node is identical to the passed node.")

        await self._destroy(with_invalidate=False)
        self._node = new_node

        await self._dispatch_voice_update()
        if not self.connected:
            raise RuntimeError(f"Switch Node on player '{self._guild.id}' failed. Failed to switch voice_state.")

        self.node._players[self._guild.id] = self

        if not self._current:
            await self.set_filters(self.filters)
            await self.set_volume(self.volume)
            await self.pause(self.paused)
            return

        await self.play(
            self._current,
            replace=True,
            start=self.position,
            volume=self.volume,
            filters=self.filters,
            paused=self.paused,
        )

    @property
    def inactive_channel_tokens(self) -> int | None:
        return self._inactive_channel_limit

    @inactive_channel_tokens.setter
    def inactive_channel_tokens(self, value: int | None) -> None:
        if not value or value <= 0:
            self._inactive_channel_limit = None
            return

        self._inactive_channel_limit = value
        self._inactive_channel_count = value

    @property
    def inactive_timeout(self) -> int | None:
        return self._inactivity_wait

    @inactive_timeout.setter
    def inactive_timeout(self, value: int | None) -> None:
        if not value or value <= 0:
            self._inactivity_wait = None
            self._inactivity_cancel()
            return

        self._inactivity_wait = value
        self._inactivity_cancel()

        if self.connected and not self.playing:
            self._inactivity_start()

    @property
    def autoplay(self) -> AutoPlayMode:
        return self._autoplay

    @autoplay.setter
    def autoplay(self, value: Any) -> None:
        if not isinstance(value, AutoPlayMode):
            raise ValueError("Please provide a valid 'wavelink.AutoPlayMode' to set.")

        self._autoplay = value

    @property
    def node(self) -> Node:
        return self._node

    @property
    def guild(self) -> discord.Guild | None:
        return self._guild

    @property
    def connected(self) -> bool:
        return self.channel and self._connected

    @property
    def current(self) -> Playable | None:
        return self._current

    @property
    def volume(self) -> int:
        return self._volume

    @property
    def filters(self) -> Filters:
        return self._filters

    @property
    def paused(self) -> bool:
        return self._paused

    @property
    def ping(self) -> int:
        return self._ping

    @property
    def playing(self) -> bool:
        return self._connected and self._current is not None

    @property
    def position(self) -> int:
        if self.current is None or not self.playing or not self.connected or self._last_update is None:
            return 0

        if self.paused:
            return self._last_position

        position: int = int((time.monotonic_ns() - self._last_update) / 1000000) + self._last_position
        return min(position, self.current.length)

    async def _update_event(self, payload: PlayerUpdateEventPayload) -> None:
        self._last_update = time.monotonic_ns()
        self._last_position = payload.position
        self._ping = payload.ping

# --- DAVE PROTOCOL VOICE UPDATE HANDLING ---
    async def on_voice_state_update(self, data: GuildVoiceStatePayload, /) -> None:
        channel_id = data.get("channel_id")

        if not channel_id:
            logger.debug(f"Guild {self._guild.id if self._guild else 'Unknown'} disconnected from voice. Destroying player.")
            await self._destroy()
            return

        self._connected = True

        old_channel_id = self._voice_state["voice"].get("channelId")

        self._voice_state["voice"]["sessionId"] = data["session_id"]
        self._voice_state["voice"]["channelId"] = str(channel_id)  # <-- REQUIRED FOR LAVALINK V4.0.8+
        self.channel = self.client.get_channel(int(channel_id))  # type: ignore

        # Dispatch if pending from initial connection race (voice_server arrived before voice_state)
        if self._pending_voice_dispatch:
            await self._dispatch_voice_update()

        # If channel changed (bot was moved), protect the player from _disconnected_wait destruction
        # and dispatch with existing credentials (works for same-server moves).
        # on_voice_server_update will re-dispatch with fresh credentials if the server changed.
        channel_changed = old_channel_id is not None and old_channel_id != str(channel_id)
        if channel_changed:
            self._reconnecting.clear()
            self._connection_event.clear()
            await self._dispatch_voice_update()

    async def on_voice_server_update(self, data: VoiceServerUpdatePayload, /) -> None:
        # Discord occasionally sends null endpoints during server reallocation.
        endpoint = data.get("endpoint")
        if not endpoint:
            logger.warning(f"Discord dispatched a null voice endpoint for guild {self.guild.id}. Waiting for valid reconnect.")
            return

        self._voice_state["voice"]["token"] = data["token"]
        self._voice_state["voice"]["endpoint"] = endpoint

        await self._dispatch_voice_update()

        # Fresh voice server credentials have been dispatched - safe for _disconnected_wait to proceed.
        self._reconnecting.set()

    async def _dispatch_voice_update(self) -> None:
        assert self.guild is not None
        data: dict[str, Any] = self._voice_state["voice"]

        session_id: str | None = data.get("sessionId")
        token: str | None = data.get("token")
        endpoint: str | None = data.get("endpoint")
        channel_id: str | None = data.get("channelId")

        if not session_id or not token or not endpoint or not channel_id:
            self._pending_voice_dispatch = True
            logger.debug(f"Waiting for full Voice Update payload for guild {self.guild.id}.")
            return

        self._pending_voice_dispatch = False

        request: RequestPayload = {
            "voice": {
                "sessionId": session_id,
                "token": token,
                "endpoint": endpoint,
                "channelId": channel_id  # <-- REQUIRED FOR LAVALINK V4.0.8+
            }
        }

        try:
            logger.debug(f"Dispatching DAVE protocol voice state for guild {self.guild.id} to Lavalink.")
            await self.node._update_player(self.guild.id, data=request)
        except LavalinkException as e:
            logger.error(f"Voice Server Update failed for guild {self.guild.id}: {e}")
            await self.disconnect()
        else:
            self._connected = True
            self._connection_event.set()

    async def connect(
        self, *, timeout: float = 10.0, reconnect: bool, self_deaf: bool = False, self_mute: bool = False
    ) -> None:
        if self.channel is MISSING:
            raise InvalidChannelStateException("Player tried to connect without a valid channel.")

        if not self._guild:
            self._guild = self.channel.guild

        self.node._players[self._guild.id] = self

        assert self.guild is not None
        await self.guild.change_voice_state(channel=self.channel, self_mute=self_mute, self_deaf=self_deaf)

        try:
            async with async_timeout.timeout(timeout):
                await self._connection_event.wait()
        except (asyncio.TimeoutError, asyncio.CancelledError):
            raise ChannelTimeoutException(f"Unable to connect to {self.channel} as it exceeded the timeout.")

    async def move_to(
        self,
        channel: VocalGuildChannel | None,
        *,
        timeout: float = 10.0,
        self_deaf: bool | None = None,
        self_mute: bool | None = None,
    ) -> None:
        if not self.guild:
            raise InvalidChannelStateException("Player tried to move without a valid guild.")

        self._connection_event.clear()
        self._reconnecting.clear()
        voice: discord.VoiceState | None = self.guild.me.voice

        if self_deaf is None and voice:
            self_deaf = voice.self_deaf

        if self_mute is None and voice:
            self_mute = voice.self_mute

        self_deaf = bool(self_deaf)
        self_mute = bool(self_mute)

        await self.guild.change_voice_state(channel=channel, self_mute=self_mute, self_deaf=self_deaf)

        if channel is None:
            self._reconnecting.set()
            return

        try:
            async with async_timeout.timeout(timeout):
                await self._connection_event.wait()
        except (asyncio.TimeoutError, asyncio.CancelledError):
            raise ChannelTimeoutException(f"Unable to connect to {channel} as it exceeded the timeout.")
        finally:
            self._reconnecting.set()

    async def play(
        self,
        track: Playable,
        *,
        replace: bool = True,
        start: int = 0,
        end: int | None = None,
        volume: int | None = None,
        paused: bool | None = None,
        add_history: bool = True,
        filters: Filters | None = None,
        populate: bool = False,
        max_populate: int = 5,
    ) -> Playable:
        assert self.guild is not None

        original_vol: int = self._volume
        vol: int = volume or self._volume

        if vol != self._volume:
            self._volume = vol

        if replace or not self._current:
            self._current = track
            self._original = track

        old_previous = self._previous
        self._previous = self._current
        self.queue._loaded = track

        pause: bool = paused if paused is not None else self._paused

        if filters:
            self._filters = filters

        request: RequestPayload = {
            "track": {"encoded": track.encoded, "userData": dict(track.extras)},
            "volume": vol,
            "position": start,
            "endTime": end,
            "paused": pause,
            "filters": self._filters(),
        }

        try:
            await self.node._update_player(self.guild.id, data=request, replace=replace)
        except LavalinkException as e:
            self.queue._loaded = old_previous
            self._current = None
            self._original = None
            self._previous = old_previous
            self._volume = original_vol
            raise e

        self._paused = pause

        if add_history:
            assert self.queue.history is not None
            self.queue.history.put(track)

        if populate:
            await self._do_recommendation(populate_track=track, max_population=max_populate)

        return track

    async def pause(self, value: bool, /) -> None:
        assert self.guild is not None
        request: RequestPayload = {"paused": value}
        await self.node._update_player(self.guild.id, data=request)
        self._paused = value

    async def seek(self, position: int = 0, /) -> None:
        assert self.guild is not None
        if not self._current:
            return
        request: RequestPayload = {"position": position}
        await self.node._update_player(self.guild.id, data=request)

    async def set_filters(self, filters: Filters | None = None, /, *, seek: bool = False) -> None:
        assert self.guild is not None
        if filters is None:
            filters = Filters()
        request: RequestPayload = {"filters": filters()}
        await self.node._update_player(self.guild.id, data=request)
        self._filters = filters

        if self.playing and seek:
            await self.seek(self.position)

    async def set_volume(self, value: int = 100, /) -> None:
        assert self.guild is not None
        vol: int = max(min(value, 1000), 0)
        request: RequestPayload = {"volume": vol}
        await self.node._update_player(self.guild.id, data=request)
        self._volume = vol

    async def disconnect(self, **kwargs: Any) -> None:
        assert self.guild
        await self._destroy()
        await self.guild.change_voice_state(channel=None)

    async def stop(self, *, force: bool = True) -> Playable | None:
        return await self.skip(force=force)

    async def skip(self, *, force: bool = True) -> Playable | None:
        assert self.guild is not None
        old: Playable | None = self._current

        if force:
            self.queue._loaded = None

        request: RequestPayload = {"track": {"encoded": None}}
        await self.node._update_player(self.guild.id, data=request, replace=True)

        return old

    def _invalidate(self) -> None:
        self._connected = False
        self._connection_event.clear()
        self._inactivity_cancel()

        try:
            self.cleanup()
        except (AttributeError, KeyError):
            pass

    async def _destroy(self, with_invalidate: bool = True) -> None:
        assert self.guild

        if with_invalidate:
            self._invalidate()

        player: Player | None = self.node._players.pop(self.guild.id, None)

        if player:
            try:
                await self.node._destroy_player(self.guild.id)
            except Exception as e:
                logger.debug("Disregarding. Failed to send 'destroy_player' payload to Lavalink: %s", e)

    def _add_to_previous_seeds(self, seed: str) -> None:
        if self.__previous_seeds.full():
            self.__previous_seeds.get_nowait()
        self.__previous_seeds.put_nowait(seed)