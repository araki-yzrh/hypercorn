from enum import auto, Enum
from time import time
from typing import Awaitable, Callable, List, Optional, Tuple, Union
from urllib.parse import unquote

from wsproto.connection import Connection, ConnectionState, ConnectionType
from wsproto.events import (
    BytesMessage,
    CloseConnection,
    Event as WSProtoEvent,
    Message,
    Ping,
    TextMessage,
)
from wsproto.extensions import PerMessageDeflate
from wsproto.frame_protocol import CloseReason
from wsproto.handshake import server_extensions_handshake, WEBSOCKET_VERSION
from wsproto.utilities import generate_accept_token, split_comma_header

from .events import Body, Data, EndBody, EndData, Event, Request, Response, StreamClosed
from ..config import Config
from ..utils import build_and_validate_headers, suppress_body, UnexpectedMessage


class ASGIWebsocketState(Enum):
    # Hypercorn supports the ASGI websocket HTTP response extension,
    # which allows HTTP responses rather than acceptance.
    HANDSHAKE = auto()
    CONNECTED = auto()
    RESPONSE = auto()
    CLOSED = auto()
    HTTPCLOSED = auto()


class FrameTooLarge(Exception):
    pass


class Handshake:
    def __init__(self, headers: List[Tuple[bytes, bytes]], http_version: str) -> None:
        self.http_version = http_version
        self.connection_tokens: Optional[List[str]] = None
        self.extensions: Optional[List[str]] = None
        self.key: Optional[bytes] = None
        self.subprotocols: Optional[List[str]] = None
        self.upgrade: Optional[bytes] = None
        self.version: Optional[bytes] = None
        for name, value in headers:
            name = name.lower()
            if name == b"connection":
                self.connection_tokens = split_comma_header(value)
            elif name == b"sec-websocket-extensions":
                self.extensions = split_comma_header(value)
            elif name == b"sec-websocket-key":
                self.key = value
            elif name == b"sec-websocket-protocol":
                self.subprotocols = split_comma_header(value)
            elif name == b"sec-websocket-version":
                self.version = value
            elif name == b"upgrade":
                self.upgrade = value

    def is_valid(self) -> bool:
        if self.http_version < "1.1":
            return False
        elif self.http_version == "1.1":
            if self.key is None:
                return False
            if self.connection_tokens is None or not any(
                token.lower() == "upgrade" for token in self.connection_tokens
            ):
                return False
            if self.upgrade.lower() != b"websocket":
                return False

        if self.version != WEBSOCKET_VERSION:
            return False
        return True

    def accept(
        self, subprotocol: Optional[str]
    ) -> Tuple[int, List[Tuple[bytes, bytes]], Connection]:
        headers = []
        if subprotocol is not None:
            if subprotocol not in self.subprotocols:
                raise Exception("Invalid Subprotocol")
            else:
                headers.append((b"sec-websocket-protocol", subprotocol.encode()))

        extensions = [PerMessageDeflate()]
        accepts = None
        if False and self.extensions is not None:
            accepts = server_extensions_handshake(self.extensions, extensions)

        if accepts:
            headers.append((b"sec-websocket-extensions", accepts))

        if self.key is not None:
            headers.append((b"sec-websocket-accept", generate_accept_token(self.key)))

        status_code = 200
        if self.http_version == "1.1":
            headers.extend([(b"upgrade", b"WebSocket"), (b"connection", b"Upgrade")])
            status_code = 101

        return status_code, headers, Connection(ConnectionType.SERVER, extensions)


class WebsocketBuffer:
    def __init__(self, max_length: int) -> None:
        self.value: Optional[Union[bytes, str]] = None
        self.max_length = max_length

    def extend(self, event: Message) -> None:
        if self.value is None:
            if isinstance(event, TextMessage):
                self.value = ""
            else:
                self.value = b""
        self.value += event.data
        if len(self.value) > self.max_length:
            raise FrameTooLarge()

    def clear(self) -> None:
        self.value = None

    def to_message(self) -> dict:
        return {
            "type": "websocket.receive",
            "bytes": self.value if isinstance(self.value, bytes) else None,
            "text": self.value if isinstance(self.value, str) else None,
        }


class WSStream:
    def __init__(
        self,
        config: Config,
        ssl: bool,
        client: Optional[Tuple[str, int]],
        server: Optional[Tuple[str, int]],
        send: Callable[[Event], Awaitable[None]],
        spawn_app: Callable[[dict, Callable], Awaitable[Callable]],
        stream_id: int,
    ) -> None:
        self.app_put: Optional[Callable] = None
        self.buffer = WebsocketBuffer(config.websocket_max_message_size)
        self.client = client
        self.closed = False
        self.config = config
        self.response: dict
        self.scope: dict
        self.send = send
        # RFC 8441 for HTTP/2 says use http or https, ASGI says ws or wss
        self.scheme = "wss" if ssl else "ws"
        self.server = server
        self.spawn_app = spawn_app
        self.start_time: float
        self.state = ASGIWebsocketState.HANDSHAKE
        self.stream_id = stream_id

        self.connection: Connection
        self.handshake: Handshake

    async def handle(self, event: Event) -> None:
        if isinstance(event, Request):
            self.handshake = Handshake(event.headers, event.http_version)
            path, _, query_string = event.raw_path.partition(b"?")
            self.scope = {
                "type": "websocket",
                "asgi": {"spec_version": "2.1"},
                "scheme": self.scheme,
                "http_version": event.http_version,
                "path": unquote(path.decode("ascii")),
                "raw_path": path,
                "query_string": query_string,
                "root_path": self.config.root_path,
                "headers": event.headers,
                "client": self.client,
                "server": self.server,
                "subprotocols": self.handshake.subprotocols or [],
                "extensions": {"websocket.http.response": {}},
            }
            self.start_time = time()
            if not self.handshake.is_valid():
                await self._send_error_response(400)
            else:
                self.app_put = await self.spawn_app(self.scope, self.app_send)
                await self.app_put({"type": "websocket.connect"})
        elif isinstance(event, (Body, Data)):
            if event.data == b"":
                # WSProto expects None to indicate the connection has
                # closed on it.
                self.connection.receive_data(None)
            else:
                self.connection.receive_data(event.data)
            await self._handle_events()
        elif isinstance(event, StreamClosed) and self.app_put is not None:
            await self.app_put({"type": "websocket.disconnect"})
            self.closed = True

    async def app_send(self, message: Optional[dict]) -> None:
        if self.closed:
            # Allow app to finish after close
            return

        if message is None:  # App has errored
            if self.state == ASGIWebsocketState.HANDSHAKE:
                await self._send_error_response(500)
                self.config.access_logger.access(
                    self.scope, {"status": 500, "headers": []}, time() - self.start_time
                )
            elif self.state == ASGIWebsocketState.CONNECTED:
                await self._send_wsproto_event(CloseConnection(code=CloseReason.ABNORMAL_CLOSURE))
            await self.send(StreamClosed(stream_id=self.stream_id))
        else:
            if message["type"] == "websocket.accept" and self.state == ASGIWebsocketState.HANDSHAKE:
                self.state = ASGIWebsocketState.CONNECTED
                status_code, headers, self.connection = self.handshake.accept(
                    message.get("subprotocol")
                )
                await self.send(
                    Response(stream_id=self.stream_id, status_code=status_code, headers=headers)
                )
                self.config.access_logger.access(
                    self.scope, {"status": status_code, "headers": []}, time() - self.start_time
                )
            elif (
                message["type"] == "websocket.http.response.start"
                and self.state == ASGIWebsocketState.HANDSHAKE
            ):
                self.response = message
            elif message["type"] == "websocket.http.response.body" and self.state in {
                ASGIWebsocketState.HANDSHAKE,
                ASGIWebsocketState.RESPONSE,
            }:
                await self._send_rejection(message)
            elif message["type"] == "websocket.send" and self.state == ASGIWebsocketState.CONNECTED:
                event: WSProtoEvent
                if message.get("bytes") is not None:
                    event = BytesMessage(data=bytes(message["bytes"]))
                elif not isinstance(message["text"], str):
                    raise TypeError(f"{message['text']} should be a str")
                else:
                    event = TextMessage(data=message["text"])
                await self._send_wsproto_event(event)
            elif (
                message["type"] == "websocket.close" and self.state == ASGIWebsocketState.HANDSHAKE
            ):
                await self._send_error_response(403)
                self.state = ASGIWebsocketState.HTTPCLOSED
            elif message["type"] == "websocket.close":
                await self._send_wsproto_event(
                    CloseConnection(code=int(message.get("code", CloseReason.NORMAL_CLOSURE)))
                )
                await self.send(EndData(stream_id=self.stream_id))
                self.state = ASGIWebsocketState.CLOSED
            else:
                raise UnexpectedMessage(self.state, message["type"])

    async def _handle_events(self) -> None:
        for event in self.connection.events():
            if isinstance(event, Message):
                try:
                    self.buffer.extend(event)
                except FrameTooLarge:
                    await self._send_wsproto_event(
                        CloseConnection(code=CloseReason.MESSAGE_TOO_BIG)
                    )
                    break

                if event.message_finished:
                    await self.app_put(self.buffer.to_message())
                    self.buffer.clear()
            elif isinstance(event, Ping):
                await self._send_wsproto_event(event.response())
            elif isinstance(event, CloseConnection):
                if self.connection.state == ConnectionState.REMOTE_CLOSING:
                    await self._send_wsproto_event(event.response())
                await self.send(StreamClosed(stream_id=self.stream_id))

    async def _send_error_response(self, status_code: int) -> None:
        await self.send(
            Response(
                stream_id=self.stream_id,
                status_code=status_code,
                headers=[(b"content-length", b"0"), (b"connection", b"close")],
            )
        )
        await self.send(EndBody(stream_id=self.stream_id))
        self.config.access_logger.access(
            self.scope, {"status": status_code, "headers": []}, time() - self.start_time
        )

    async def _send_wsproto_event(self, event: WSProtoEvent) -> None:
        data = self.connection.send(event)
        await self.send(Data(stream_id=self.stream_id, data=data))

    async def _send_rejection(self, message: dict) -> None:
        body_suppressed = suppress_body("GET", self.response["status"])
        if self.state == ASGIWebsocketState.HANDSHAKE:
            headers = build_and_validate_headers(self.response["headers"])
            await self.send(
                Response(
                    stream_id=self.stream_id,
                    status_code=int(self.response["status"]),
                    headers=headers,
                )
            )
            self.state = ASGIWebsocketState.RESPONSE
        if not body_suppressed:
            await self.send(Body(stream_id=self.stream_id, data=bytes(message.get("body", b""))))
        if not message.get("more_body", False):
            await self.send(EndBody(stream_id=self.stream_id))
            self.state = ASGIWebsocketState.HTTPCLOSED
            self.config.access_logger.access(self.scope, self.response, time() - self.start_time)
