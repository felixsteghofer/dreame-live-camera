"""LinkVisual's encrypted, two-byte generic RTP payload carrying Annex B H264.

Reconstructed from the stock LinkVisual SDK and verified with the LAN PoC.
No media, session keys or camera PINs are persisted.
"""
import ipaddress
import struct

from Crypto.Cipher import AES


def private_ipv4(value):
    """Accept a single RFC1918 host, never a subnet, DNS name or public relay."""
    try:
        address = ipaddress.IPv4Address(value)
    except (ValueError, TypeError):
        raise ValueError("A private IPv4 camera address is required") from None
    if not any(address in net for net in (
        ipaddress.IPv4Network("10.0.0.0/8"),
        ipaddress.IPv4Network("172.16.0.0/12"),
        ipaddress.IPv4Network("192.168.0.0/16"),
    )):
        raise ValueError("A private IPv4 camera address is required")
    return str(address)


def device_address(info):
    """Discover only the device's localIp; gateways and cloud hosts do not count."""
    if isinstance(info, dict):
        if "localIp" in info:
            return private_ipv4(info["localIp"])
        for value in info.values():
            if isinstance(value, (dict, list)):
                found = device_address(value)
                if found:
                    return found
    elif isinstance(info, list):
        for value in info:
            found = device_address(value)
            if found:
                return found
    return None


def decrypt_rtp(packet, password):
    if len(packet) < 32 or packet[12:14] != b"\xbb\xbb":
        return packet
    key = password.encode()[:16]
    if len(key) != 16:
        raise ValueError("Incomplete negotiated media key")
    end = 16 + ((len(packet) - 16) // 16) * 16
    return packet[:16] + AES.new(key, AES.MODE_CBC, iv=bytes(16)).decrypt(packet[16:end]) + packet[end:]


class VideoFrames:
    """Bound frames; discard gaps/duplicates until the next first-packet marker."""
    def __init__(self):
        self.timestamp = self.seq = None
        self.buffer = bytearray()
        self.invalid = True
        self.dropped = 0

    def feed(self, packet):
        if len(packet) < 14 or packet[0] >> 6 != 2 or packet[1] & 127 != 96:
            return None
        offset = 12 + 4 * (packet[0] & 15)
        if packet[0] & 16:
            if len(packet) < offset + 4:
                return None
            offset += 4 + 4 * struct.unpack_from("!H", packet, offset + 2)[0]
        end = len(packet)
        if packet[0] & 32:
            padding = packet[-1]
            if not padding or padding > end - offset:
                return None
            end -= padding
        if offset + 2 >= end:
            return None
        descriptor = packet[offset]
        # LinkVisual audio/dummy packets use other flag bits.
        if descriptor & ~3:
            return None
        seq, timestamp = struct.unpack_from("!HI", packet, 2)
        if descriptor & 2:
            self.timestamp, self.seq = timestamp, None
            self.buffer.clear()
            self.invalid = False
        elif timestamp != self.timestamp:
            return None
        if self.seq is not None and seq != (self.seq + 1) & 65535:
            self.invalid = True
        self.seq = seq
        if len(self.buffer) + end - offset - 2 > 2 * 1024 * 1024:
            self.invalid = True
            self.buffer.clear()
        elif not self.invalid:
            self.buffer.extend(packet[offset + 2:end])
        if packet[1] & 128:
            result = bytes(self.buffer) if not self.invalid and self.buffer else None
            if result is None:
                self.dropped += 1
            self.buffer.clear()
            self.invalid = True
            return result
        return None
