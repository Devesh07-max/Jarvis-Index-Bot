"""Minimal HTTP health app for Render Web Service deployments.

The Telegram bot continues to use long polling. Uvicorn runs alongside it only
so Render has an HTTP port to probe and an uptime monitor can check /health.
"""
import asyncio
import logging

import uvicorn
from fastapi import FastAPI
from fastapi.responses import JSONResponse

log = logging.getLogger("jarvis.web")


def make_app():
    app = FastAPI(title="JARVIS Index Bot", docs_url=None, redoc_url=None, openapi_url=None)

    @app.get("/")
    async def root():
        return {"service": "JARVIS Index Bot", "status": "running"}

    @app.get("/health")
    async def health():
        return JSONResponse({"status": "ok", "service": "jarvis-index-bot"})

    return app


async def start_http_server(port: int):
    """Start the health endpoint without blocking Telegram polling startup."""
    config = uvicorn.Config(
        app=make_app(),
        host="0.0.0.0",
        port=port,
        log_level="warning",
        access_log=False,
        server_header=False,
    )
    server = uvicorn.Server(config)
    task = asyncio.create_task(server.serve(), name="jarvis-http-health")

    # Do not let the bot proceed if the required Render port failed to bind.
    deadline = asyncio.get_running_loop().time() + 15
    while not server.started:
        if task.done():
            exception = task.exception()
            if exception:
                raise RuntimeError("HTTP health server failed to start") from exception
            raise RuntimeError("HTTP health server stopped before startup")
        if asyncio.get_running_loop().time() >= deadline:
            server.should_exit = True
            await asyncio.gather(task, return_exceptions=True)
            raise TimeoutError("HTTP health server did not become ready within 15 seconds")
        await asyncio.sleep(0.05)

    log.info("HTTP health server listening on 0.0.0.0:%s", port)
    return server, task


async def stop_http_server(server, task):
    if server is None or task is None:
        return
    server.should_exit = True
    try:
        await asyncio.wait_for(task, timeout=10)
    except asyncio.TimeoutError:
        server.force_exit = True
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
