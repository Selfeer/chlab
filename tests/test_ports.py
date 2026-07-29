"""Port-block allocation, with the bind probe faked so the search logic is what's tested."""

from __future__ import annotations

import pytest

from chlab.core import ports
from chlab.core.ports import BLOCK_ALIGNMENT, SEARCH_END, NoFreePorts, allocate_block


@pytest.fixture
def free_ports(monkeypatch):
    """Control which ports the OS reports as free."""
    busy: set[int] = set()
    monkeypatch.setattr(ports, "is_free", lambda port, host="127.0.0.1": port not in busy)
    return busy


def test_returns_the_first_aligned_block(free_ports):
    assert allocate_block(20, start=21000) == 21000


def test_start_is_rounded_down_to_the_alignment(free_ports):
    assert allocate_block(10, start=21050) % BLOCK_ALIGNMENT == 0


def test_skips_a_block_holding_a_bound_port(free_ports):
    free_ports.add(21005)
    assert allocate_block(20, start=21000) == 21000 + BLOCK_ALIGNMENT


def test_skips_ports_promised_to_another_instance(free_ports):
    """Stopped instances hold their ports even though the OS reports them free."""
    assert allocate_block(20, start=21000, taken=set(range(21000, 21020))) == 21100


def test_a_taken_port_anywhere_in_the_block_disqualifies_it(free_ports):
    assert allocate_block(50, start=21000, taken={21049}) == 21100


def test_adjacent_blocks_do_not_overlap(free_ports):
    first = allocate_block(50, start=21000)
    second = allocate_block(50, start=21000, taken=set(range(first, first + 50)))
    assert set(range(first, first + 50)).isdisjoint(range(second, second + 50))


def test_exhaustion_raises_rather_than_returning_a_bad_block(free_ports, monkeypatch):
    monkeypatch.setattr(ports, "SEARCH_END", 21200)
    with pytest.raises(NoFreePorts, match="consecutive free ports"):
        allocate_block(50, start=21000, taken=set(range(21000, 21200)))


def test_the_last_usable_block_is_reachable(free_ports, monkeypatch):
    """`base + span < END` would skip the block that ends exactly at the limit."""
    monkeypatch.setattr(ports, "SEARCH_END", 21100)
    assert allocate_block(100, start=21000) == 21000


def test_real_probe_agrees_with_a_bound_socket():
    """One check against the real socket layer, so the fake above cannot drift from it."""
    import socket

    with socket.socket() as held:
        held.bind(("127.0.0.1", 0))
        held.listen(1)
        port = held.getsockname()[1]
        assert ports.is_free(port) is False
    assert SEARCH_END > 21000
