"""Tests for the startup gate (no network needed: connections are simulated).

    python -m pytest tests/test_netgate.py -v
"""
import netgate


class World:
    """Simulated network: rules take effect after `rules_after` seconds."""

    def __init__(self, rules_after):
        self.now, self.rules_after = 0.0, rules_after

    def check(self, host, port):
        rules_on = self.now >= self.rules_after
        if (host, port) in netgate.CANARIES:
            return not rules_on          # the internet is reachable until the rules apply
        return rules_on                  # postgres refuses this pod until the rules apply

    def sleep(self, seconds):
        self.now += seconds

    def clock(self):
        return self.now


def run(world, timeout=60, required=(("postgres", 5432),)):
    return netgate.wait(list(required), timeout, check=world.check, sleep=world.sleep, clock=world.clock, log=lambda _: None)


def test_it_waits_until_the_rules_are_in_force():
    assert run(World(rules_after=7)) == 7


def test_it_passes_at_once_when_the_rules_already_apply():
    assert run(World(rules_after=0)) == 0


def test_it_fails_closed_if_the_rules_never_apply():
    assert run(World(rules_after=1_000), timeout=60) is None


def test_internet_still_reachable_means_not_ready_even_if_postgres_answers():
    world = World(rules_after=5)
    world.check = lambda host, port: True        # everything reachable: no egress rules at all
    assert run(world, timeout=10) is None


def test_required_destinations_are_parsed():
    assert netgate.parse("postgres:5432, egress-proxy:3128") == [("postgres", 5432), ("egress-proxy", 3128)]
    assert netgate.parse("") == []
