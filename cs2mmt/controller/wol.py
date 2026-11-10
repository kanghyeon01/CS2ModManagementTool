"""Wake-on-LAN magic packets."""

from __future__ import annotations

import re
import socket
from typing import Iterable

__all__ = ["parse_mac", "magic_packet", "send_magic_packet"]


def parse_mac(mac: str) -> bytes:
    digits = re.sub(r"[^0-9A-Fa-f]", "", mac)
    if len(digits) != 12:
        raise ValueError("not a MAC address: %r" % mac)
    return bytes.fromhex(digits)


def magic_packet(mac: str) -> bytes:
    """6 x 0xFF followed by the MAC address 16 times."""
    return b"\xff" * 6 + parse_mac(mac) * 16


def send_magic_packet(mac: str, broadcast: str = "255.255.255.255", port: int = 9, repeat: int = 3, interfaces: Iterable[str] = ()) -> int:
    """Sends the packet ``repeat`` times to the broadcast address (and each extra address given).

    Returns the number of datagrams sent. Directed broadcasts (e.g. 192.168.0.255) are more
    reliable from Docker containers on a bridge network; use host networking or set
    ``broadcast`` to the LAN's broadcast address.
    """
    packet = magic_packet(mac)
    targets = [broadcast] + [i for i in interfaces if i]
    sent = 0
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
        s.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
        for _ in range(max(1, repeat)):
            for t in targets:
                s.sendto(packet, (t, port))
                sent += 1
    return sent
