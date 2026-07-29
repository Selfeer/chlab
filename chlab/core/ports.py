"""Host port allocation.

Ports are handed out in per-instance blocks and recorded in the instance's spec, so a named
instance keeps the same ports for its whole life and you can bookmark its ``/play`` URL.
"""

from __future__ import annotations

import socket

from .errors import ChlabError

#: Well clear of the ephemeral range and of ClickHouse's own defaults.
SEARCH_START = 21000
SEARCH_END = 60000
BLOCK_ALIGNMENT = 100


class NoFreePorts(ChlabError):
    pass


def is_free(port: int, host: str = "127.0.0.1") -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        probe.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            probe.bind((host, port))
        except OSError:
            return False
    return True


def allocate_block(span: int, *, taken: set[int] | None = None, start: int = SEARCH_START) -> int:
    """Find the first aligned block of ``span`` free ports and return its first port.

    ``taken`` lists ports already promised to other instances — including stopped ones, whose
    ports are free at the OS level but must not be reassigned.

    The bind probe is inherently racy: two ``chlab up`` runs started at the same moment can
    pick the same block, and only the second one's ``docker compose up`` will fail. The
    registry check closes the common case (sequential launches); the window that remains is
    the few hundred milliseconds between probing and binding.
    """
    taken = taken or set()
    base = start - (start % BLOCK_ALIGNMENT)
    while base + span <= SEARCH_END:
        candidate = range(base, base + span)
        if not taken.intersection(candidate) and all(is_free(port) for port in candidate):
            return base
        base += BLOCK_ALIGNMENT
    raise NoFreePorts(
        f"could not find {span} consecutive free ports between {start} and {SEARCH_END}"
    )
