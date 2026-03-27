"""
Network connection handling for TMW.

Manages TCP connections to login, char, and map servers.
Handles packet framing (reading correct number of bytes based on packet ID).
"""

import socket
import struct
import time
import logging

from packets import PACKET_SIZES, parse_packet

log = logging.getLogger(__name__)


class Connection:
    """A TCP connection to a TMW server with packet-level read/write."""

    def __init__(self, host: str, port: int, timeout: float = 10.0):
        self.host = host
        self.port = port
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self.sock.settimeout(timeout)
        self.sock.connect((host, port))
        self._recv_buf = bytearray()
        log.info('Connected to %s:%d', host, port)

    def send_packet(self, data: bytes):
        """Send a raw packet."""
        self.sock.sendall(data)
        pkt_id = struct.unpack_from('<H', data, 0)[0]
        log.debug('Sent packet 0x%04x (%d bytes)', pkt_id, len(data))

    def _recv_bytes(self, n: int) -> bytes:
        """Receive exactly n bytes, buffering as needed."""
        while len(self._recv_buf) < n:
            try:
                chunk = self.sock.recv(4096)
            except socket.timeout:
                raise TimeoutError('Timed out waiting for data')
            if not chunk:
                raise ConnectionError('Connection closed by server')
            self._recv_buf.extend(chunk)
        result = bytes(self._recv_buf[:n])
        del self._recv_buf[:n]
        return result

    def recv_packet(self):
        """Receive and parse one packet. Returns parsed result."""
        # Read packet ID (2 bytes)
        header = self._recv_bytes(2)
        pkt_id = struct.unpack_from('<H', header, 0)[0]

        size = PACKET_SIZES.get(pkt_id)

        if pkt_id == 0x8000:
            # Special hold/transaction packet: 4-byte header (id + length),
            # but we only consume the 4-byte header; the embedded length
            # tells the server about transaction grouping, not our read size.
            len_bytes = self._recv_bytes(2)
            data = header + len_bytes
            log.debug('Received packet 0x8000 (hold notify, 4 bytes)')
            return parse_packet(pkt_id, data)
        elif size is not None:
            # Fixed-size packet
            remaining = self._recv_bytes(size - 2)
            data = header + remaining
        elif size is None and pkt_id in PACKET_SIZES:
            # Variable-size packet: next 2 bytes are the total length
            len_bytes = self._recv_bytes(2)
            total_len = struct.unpack_from('<H', len_bytes, 0)[0]
            remaining = self._recv_bytes(total_len - 4)
            data = header + len_bytes + remaining
        else:
            # Unknown packet - try to read as variable-length (has length at offset 2)
            # This is a reasonable fallback since most unknown packets are var-length
            log.warning('Unknown packet 0x%04x, trying as variable-length', pkt_id)
            try:
                len_bytes = self._recv_bytes(2)
                total_len = struct.unpack_from('<H', len_bytes, 0)[0]
                if total_len < 4 or total_len > 65535:
                    log.error('Unknown packet 0x%04x with implausible length %d', pkt_id, total_len)
                    raise ValueError(f'Unknown packet ID 0x{pkt_id:04x}')
                remaining = self._recv_bytes(total_len - 4)
                data = header + len_bytes + remaining
            except (TimeoutError, ConnectionError):
                log.error('Unknown packet 0x%04x, cannot determine size!', pkt_id)
                raise ValueError(f'Unknown packet ID 0x{pkt_id:04x}')

        log.debug('Received packet 0x%04x (%d bytes)', pkt_id, len(data))
        return parse_packet(pkt_id, data)

    def recv_packet_nonblock(self, timeout: float = 0.0):
        """Try to receive a packet with a short timeout. Returns None if nothing available."""
        old_timeout = self.sock.gettimeout()
        self.sock.settimeout(timeout)
        try:
            return self.recv_packet()
        except (TimeoutError, socket.timeout):
            return None
        finally:
            self.sock.settimeout(old_timeout)

    def has_data(self) -> bool:
        """Check if there's data in the receive buffer."""
        return len(self._recv_buf) >= 2

    def close(self):
        """Close the connection."""
        try:
            self.sock.close()
        except OSError:
            pass
        log.info('Disconnected from %s:%d', self.host, self.port)
