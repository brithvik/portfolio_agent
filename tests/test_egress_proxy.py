"""Tests for the egress proxy's refusals. No outside network needed.

    python -m pytest tests/test_egress_proxy.py -v
"""
import asyncio
import importlib

import pytest


@pytest.fixture
def proxy(monkeypatch):
    monkeypatch.setenv("ALLOWED_HOSTS", "generativelanguage.googleapis.com, localhost")
    import egress_proxy
    return importlib.reload(egress_proxy)


def ask(proxy, request):
    """Start the proxy on a free port, send one request, and return the status line."""
    async def go():
        server = await asyncio.start_server(proxy.handle, "127.0.0.1", 0)
        port = server.sockets[0].getsockname()[1]
        reader, writer = await asyncio.open_connection("127.0.0.1", port)
        writer.write(request)
        await writer.drain()
        status = (await asyncio.wait_for(reader.readline(), timeout=5)).decode().strip()
        writer.close()
        server.close()
        await server.wait_closed()
        return status
    return asyncio.run(go())


def test_hosts_not_on_the_allowlist_are_refused(proxy):
    assert "403" in ask(proxy, b"CONNECT paper-api.alpaca.markets:443 HTTP/1.1\r\n\r\n")
    assert "403" in ask(proxy, b"CONNECT example.com:443 HTTP/1.1\r\n\r\n")


def test_ip_addresses_are_refused(proxy):
    assert "403" in ask(proxy, b"CONNECT 10.96.0.1:443 HTTP/1.1\r\n\r\n")
    assert "403" in ask(proxy, b"CONNECT 169.254.169.254:443 HTTP/1.1\r\n\r\n")


def test_only_port_443_is_allowed_even_for_allowed_hosts(proxy):
    assert "403" in ask(proxy, b"CONNECT generativelanguage.googleapis.com:22 HTTP/1.1\r\n\r\n")
    assert "403" in ask(proxy, b"CONNECT generativelanguage.googleapis.com:80 HTTP/1.1\r\n\r\n")


def test_lookalike_hostnames_are_refused(proxy):
    assert "403" in ask(proxy, b"CONNECT generativelanguage.googleapis.com.evil.example:443 HTTP/1.1\r\n\r\n")
    assert "403" in ask(proxy, b"CONNECT evil-generativelanguage.googleapis.com:443 HTTP/1.1\r\n\r\n")


def test_plain_http_is_refused(proxy):
    assert "405" in ask(proxy, b"GET http://generativelanguage.googleapis.com/ HTTP/1.1\r\n\r\n")
    assert "405" in ask(proxy, b"POST / HTTP/1.1\r\n\r\n")


def test_an_allowed_name_that_points_inside_the_network_is_refused(proxy):
    # 'localhost' is on this test's allowlist, but it resolves to a private (loopback) address
    assert "403" in ask(proxy, b"CONNECT localhost:443 HTTP/1.1\r\n\r\n")
