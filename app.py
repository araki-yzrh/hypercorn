import time
import os
import asyncio
from starlette.applications import Starlette
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import PlainTextResponse
from starlette.routing import Route


class ASGIMiddleware:
    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        print(f"[{os.getpid()}] ASGIMiddleware await")
        await asyncio.sleep(1)
        print(f"[{os.getpid()}] ASGIMiddleware call")
        await self.app(scope, receive, send)
        print(f"[{os.getpid()}] ASGIMiddleware done")


class HTTPMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next):
        print(f"[{os.getpid()}] HTTPMiddleware await")
        time.sleep(1)
        print(f"[{os.getpid()}] HTTPMiddleware call")
        response = await call_next(request)
        print(f"[{os.getpid()}] HTTPMiddleware done")
        return response


class HTTPMiddlewareAsync(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next):
        print(f"[{os.getpid()}] app.HTTPMiddlewareAsync await")
        await asyncio.sleep(1)
        print(f"[{os.getpid()}] app.HTTPMiddlewareAsync call")
        response = await call_next(request)
        print(f"[{os.getpid()}] app.HTTPMiddlewareAsync done")
        return response


async def root(request):
    print(f"[{os.getpid()}] app.root endpoint begin")
    time.sleep(1)
    print(f"[{os.getpid()}] app.root endpoint finish")
    return PlainTextResponse("OK")

app = Starlette(routes=[
    Route('/', root),
])


# app.add_middleware(ASGIMiddleware)
# app.add_middleware(ASGIMiddleware)
# app.add_middleware(HTTPMiddleware)
app.add_middleware(HTTPMiddlewareAsync)
app.add_middleware(HTTPMiddlewareAsync)
