from fastapi.testclient import TestClient

from app.web import make_app


def test_root_health_endpoint_reports_running_service():
    with TestClient(make_app()) as client:
        response = client.get("/")
    assert response.status_code == 200
    assert response.json() == {"service": "JARVIS Index Bot", "status": "running"}


def test_health_endpoint_is_lightweight_and_public():
    with TestClient(make_app()) as client:
        response = client.get("/health")
    assert response.status_code == 200
    assert response.json() == {"status": "ok", "service": "jarvis-index-bot"}


import asyncio
import urllib.request

import pytest

from app.web import start_http_server, stop_http_server


@pytest.mark.asyncio
async def test_uvicorn_health_listener_binds_and_serves_http():
    server, task = await start_http_server(0)
    try:
        sockets = [sock for srv in server.servers for sock in srv.sockets]
        assert sockets, "Uvicorn started without a listening socket"
        port = sockets[0].getsockname()[1]
        response = await asyncio.to_thread(urllib.request.urlopen, f"http://127.0.0.1:{port}/health")
        assert response.status == 200
        assert b'"status":"ok"' in response.read()
    finally:
        await stop_http_server(server, task)
