"""A tiny allowlisting egress proxy for the agent.

Kubernetes NetworkPolicies filter by IP address, not by website name, so they can't say "the
agent may reach Google's Gemini API and nothing else". This proxy can. Agent pods are allowed to
reach only this proxy (plus the database and the gateway), and this proxy only opens tunnels to
the hostnames on its allowlist.

It speaks one thing: HTTP CONNECT on port 443 (how HTTPS goes through a proxy). The traffic
inside the tunnel stays encrypted end to end; the proxy sees only the hostname.
"""
import asyncio
import ipaddress
import os
import socket

ALLOWED_HOSTS = {h.strip().lower() for h in os.environ.get(
    "ALLOWED_HOSTS", "generativelanguage.googleapis.com").split(",") if h.strip()}
PORT = int(os.environ.get("PROXY_PORT", "3128"))
ALLOW_PRIVATE = os.environ.get("ALLOW_PRIVATE_TARGETS") == "true"   # tests only


def log(message):
    print(f"[egress-proxy] {message}", flush=True)


async def refuse(writer, status, reason):
    writer.write(f"HTTP/1.1 {status}\r\nContent-Length: 0\r\nConnection: close\r\n\r\n".encode())
    try:
        await writer.drain()
    finally:
        writer.close()
    log(f"DENIED  {reason}")


async def pipe(reader, writer):
    try:
        while data := await reader.read(65536):
            writer.write(data)
            await writer.drain()
    except (ConnectionError, asyncio.CancelledError, OSError):
        pass
    finally:
        try:
            writer.close()
        except OSError:
            pass


async def handle(reader, writer):
    client = writer.get_extra_info("peername", ("?",))[0]
    try:
        head = await asyncio.wait_for(reader.readuntil(b"\r\n\r\n"), timeout=10)
    except (asyncio.TimeoutError, asyncio.IncompleteReadError, asyncio.LimitOverrunError):
        writer.close()
        return

    parts = head.split(b"\r\n", 1)[0].decode("latin-1").split()
    if len(parts) != 3 or parts[0].upper() != "CONNECT":
        return await refuse(writer, "405 Method Not Allowed",
                            f"{client}: only HTTPS tunnels (CONNECT) are supported")
    host, _, port = parts[1].rpartition(":")
    host = host.lower().strip("[]")
    if port != "443" or host not in ALLOWED_HOSTS:
        return await refuse(writer, "403 Forbidden", f"{client} -> {parts[1]} (not on the allowlist)")

    # Resolve the name ourselves and refuse private addresses, so an allowed name can't be
    # pointed at something inside the cluster.
    try:
        infos = await asyncio.get_running_loop().getaddrinfo(host, 443, type=socket.SOCK_STREAM)
        address = infos[0][4][0]
    except OSError:
        return await refuse(writer, "502 Bad Gateway", f"{client} -> {host} (name did not resolve)")
    ip = ipaddress.ip_address(address)
    if not ALLOW_PRIVATE and (ip.is_private or ip.is_loopback or ip.is_link_local):
        return await refuse(writer, "403 Forbidden", f"{client} -> {host} resolved to a private address")

    try:
        upstream_reader, upstream_writer = await asyncio.wait_for(
            asyncio.open_connection(address, 443), timeout=10)
    except (OSError, asyncio.TimeoutError):
        return await refuse(writer, "502 Bad Gateway", f"{client} -> {host} (could not connect)")

    writer.write(b"HTTP/1.1 200 Connection established\r\n\r\n")
    await writer.drain()
    log(f"ALLOWED {client} -> {host}:443")
    await asyncio.gather(pipe(reader, upstream_writer), pipe(upstream_reader, writer))


async def main():
    server = await asyncio.start_server(handle, "0.0.0.0", PORT, limit=16384)
    log(f"listening on :{PORT}; allowed hosts: {', '.join(sorted(ALLOWED_HOSTS))}")
    async with server:
        await server.serve_forever()


if __name__ == "__main__":
    asyncio.run(main())
