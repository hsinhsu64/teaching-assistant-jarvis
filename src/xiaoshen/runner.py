"""在同一個 asyncio 迴圈跑多個 uvicorn server（控制 app 與手機 app）。"""

from __future__ import annotations

import asyncio

import uvicorn
from fastapi import FastAPI


def make_servers(pairs: list[tuple[FastAPI, int]]) -> list[uvicorn.Server]:
    return [
        uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning", lifespan="off"))
        for app, port in pairs
    ]


async def serve(servers: list[uvicorn.Server]) -> None:
    await asyncio.gather(*(s.serve() for s in servers))
