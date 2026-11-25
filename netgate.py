"""Startup gate: don't let a pod do anything until its network rules are actually in force.

Some network plugins (k3s's, for one) apply a new pod's NetworkPolicies a few seconds AFTER
the pod starts. In that window the pod's own restrictions don't exist yet, so it could reach
the open internet, and the pods it needs (Postgres) don't yet recognise it, so they refuse it.
netcheck caught this on the AWS server: an agent pod reached example.com in its first seconds.

So every pod that runs our code starts with this as an init container, in the same network
position as the real program. It waits until both are true:

  1. A connection that NO pod is allowed to make is refused. Nothing in k8s/network.yaml
     allows outbound port 80, so "1.1.1.1:80 is blocked" proves this pod's rules are active.
     (Two independent addresses are tried, so one being down can't fake a pass.)
  2. Everything the program needs (NETGATE_REQUIRE, e.g. "postgres:5432") accepts it.

Then it exits and the real program starts. If that hasn't happened within NETGATE_TIMEOUT
seconds, it exits with an error and the pod never starts: it fails closed, and the failed
run raises an alert, instead of quietly running unprotected.
"""
import os
import socket
import sys
import time

CANARIES = [("1.1.1.1", 80), ("8.8.8.8", 80)]   # literal IPs: no DNS needed, nothing may reach them on port 80
PROBE_TIMEOUT = 2.0


def reachable(host, port, timeout=PROBE_TIMEOUT):
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False


def parse(spec):
    """'postgres:5432,egress-proxy:3128' -> [('postgres', 5432), ('egress-proxy', 3128)]"""
    out = []
    for item in filter(None, (s.strip() for s in spec.split(","))):
        host, _, port = item.rpartition(":")
        out.append((host, int(port)))
    return out


def wait(required, timeout, check=reachable, sleep=time.sleep, clock=time.monotonic, log=print):
    """Returns the seconds it took for the rules to be in force, or None if they never were."""
    start = clock()
    while True:
        leaking = [f"{h}:{p}" for h, p in CANARIES if check(h, p)]
        missing = [f"{h}:{p}" for h, p in required if not check(h, p)]
        if not leaking and not missing:
            return clock() - start
        if clock() - start >= timeout:
            log(f"[netgate] network rules not in force after {timeout:.0f}s "
                f"(still reachable: {leaking or 'none'}; still refused: {missing or 'none'}). Refusing to start.")
            return None
        sleep(1)


def main():
    required = parse(os.environ.get("NETGATE_REQUIRE", ""))
    took = wait(required, float(os.environ.get("NETGATE_TIMEOUT", "60")))
    if took is None:
        return 1
    print(f"[netgate] network rules in force after {took:.1f}s; starting.", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
