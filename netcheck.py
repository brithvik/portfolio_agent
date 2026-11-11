"""Checks, from inside an agent pod, that the network rules actually hold.

NetworkPolicies are only words in a YAML file unless the cluster's network plugin enforces
them, and some plugins silently ignore them. So this doesn't trust the YAML: it tries the
connections and reports what really happened.

    python netcheck.py          (run inside the cluster by scripts\\netcheck.ps1)
"""
import os
import socket
import sys

PROXY = os.environ.get("NETCHECK_PROXY", "egress-proxy:3128")
TIMEOUT = 6


def tcp(host, port):
    """Can we open a TCP connection? Returns (reached, detail)."""
    try:
        with socket.create_connection((host, port), timeout=TIMEOUT):
            return True, "connected"
    except socket.gaierror:
        return False, "name did not resolve"
    except (socket.timeout, TimeoutError):
        return False, "timed out (blocked)"
    except OSError as e:
        return False, f"refused ({e.__class__.__name__})"


def through_proxy(host):
    """Ask the egress proxy for a tunnel to host:443. Returns (allowed, detail)."""
    proxy_host, _, proxy_port = PROXY.partition(":")
    try:
        with socket.create_connection((proxy_host, int(proxy_port)), timeout=TIMEOUT) as s:
            s.settimeout(TIMEOUT + 10)
            s.sendall(f"CONNECT {host}:443 HTTP/1.1\r\nHost: {host}:443\r\n\r\n".encode())
            status = s.recv(200).split(b"\r\n")[0].decode("latin-1")
            return " 200 " in status + " ", status
    except OSError as e:
        return False, f"could not reach the proxy ({e.__class__.__name__})"


CHECKS = [
    # (what, how, should it get through?)
    ("Database            postgres:5432",              lambda: tcp("postgres", 5432), True),
    ("Broker gateway      gateway:8080",               lambda: tcp("gateway", 8080), True),
    ("Egress proxy        egress-proxy:3128",          lambda: tcp(*PROXY.split(":")[:1], int(PROXY.split(":")[1])), True),
    ("Gemini API          through the proxy",          lambda: through_proxy("generativelanguage.googleapis.com"), True),
    ("Alpaca trading API  directly",                   lambda: tcp("paper-api.alpaca.markets", 443), False),
    ("Alpaca trading API  through the proxy",          lambda: through_proxy("paper-api.alpaca.markets"), False),
    ("Alpaca data API     directly",                   lambda: tcp("data.alpaca.markets", 443), False),
    ("The open internet   directly (example.com)",     lambda: tcp("example.com", 443), False),
    ("The open internet   through the proxy",          lambda: through_proxy("example.com"), False),
]


def main():
    print("Network check, from inside an agent pod\n")
    print(f"  {'Destination':<46} {'Expected':<10} {'Result':<10} Detail")
    failures, leaks = [], []
    for name, probe, should_pass in CHECKS:
        got_through, detail = probe()
        ok = got_through == should_pass
        expected = "reachable" if should_pass else "BLOCKED"
        result = "reachable" if got_through else "blocked"
        print(f"  {name:<46} {expected:<10} {result:<10} {detail}  {'' if ok else '<-- WRONG'}")
        if not ok:
            failures.append(name)
            if got_through:
                leaks.append(name)

    print()
    if not failures:
        print("PASS: the agent can reach only the database, the gateway, and the Gemini API "
              "(through the allowlisting proxy). Everything else is blocked.")
        return 0
    if leaks:
        print("FAIL: these should be blocked but were reachable:")
        for name in leaks:
            print(f"   - {' '.join(name.split())}")
        if any("directly" in name for name in leaks):
            print("\nDirect connections got through, which usually means this cluster's network "
                  "plugin is NOT enforcing NetworkPolicies. The rules exist but do nothing.")
    blocked_wrongly = [n for n in failures if n not in leaks]
    if blocked_wrongly:
        print("FAIL: these should be reachable but were blocked:")
        for name in blocked_wrongly:
            print(f"   - {' '.join(name.split())}")
    return 1


if __name__ == "__main__":
    sys.exit(main())
