"""PNG files built byte by byte for the clipboard tests: the shape WIRE.md section 3 checks before
anything decodes an image. Only a 1 by 1 image carries pixels a decoder could draw.

The zlib streams are written here rather than by zlib.compress, whose output differs between zlib
builds (the Mac's and Windows' Pythons disagree), so the vectors made from these are the same
bytes on every machine."""

import struct
import zlib

from core import protocol


def stored(data: bytes) -> bytes:
    """A zlib stream holding `data` in stored blocks: no compression, the same bytes everywhere."""
    blocks = [data[at:at + 65535] for at in range(0, len(data), 65535)] or [b""]
    body = b"".join(
        bytes([index == len(blocks) - 1]) + struct.pack("<HH", len(block), len(block) ^ 0xFFFF) + block
        for index, block in enumerate(blocks)
    )
    return b"\x78\x01" + body + struct.pack(">I", zlib.adler32(data))


def run(byte: bytes, count: int) -> bytes:
    """A zlib stream inflating to `byte` repeated `count` times, about count / 258 bytes long: one
    fixed-Huffman block of the literal, then copies of 258 at distance 1, then literals."""
    bits, used, out = 0, 0, bytearray()

    def put(value, width):
        nonlocal bits, used
        bits |= value << used
        used += width
        while used >= 8:
            out.append(bits & 0xFF)
            bits >>= 8
            used -= 8

    def code(value, width):
        put(int(format(value, f"0{width}b")[::-1], 2), width)

    literal = byte[0]
    put(1, 1)                       # the last block
    put(1, 2)                       # fixed Huffman codes
    copies, rest = divmod(count - 1, 258)
    for _ in range(1 + rest if count else 0):
        code(0x30 + literal, 8) if literal < 144 else code(0x190 + literal - 144, 9)
    for _ in range(copies):
        code(0xC5, 8)               # length 258 (code 285)
        code(0, 5)                  # distance 1
    code(0, 7)                      # end of block
    if used:
        out.append(bits & 0xFF)
    return b"\x78\x01" + bytes(out) + struct.pack(">I", zlib.adler32(byte * count))


def chunk(kind: bytes, data: bytes) -> bytes:
    return struct.pack(">I", len(data)) + kind + data + struct.pack(">I", zlib.crc32(kind + data))


def ihdr(width: int, height: int) -> bytes:
    return chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 6, 0, 0, 0))


def png(width: int = 1, height: int = 1, *extra: bytes, header: bytes = None) -> bytes:
    """A PNG of `width` by `height` (8-bit RGBA), with the chunks in `extra` after its header."""
    pixels = stored(b"\x00" + b"\x00\x00\x00\x00") if (width, height) == (1, 1) else stored(b"")
    return (protocol.PNG_SIGNATURE + (ihdr(width, height) if header is None else header) + b"".join(extra)
            + chunk(b"IDAT", pixels) + chunk(b"IEND", b""))


def ztxt(size: int, keyword: bytes = b"Comment") -> bytes:
    """A zTXt chunk whose text inflates to `size` bytes."""
    return chunk(b"zTXt", keyword + b"\x00\x00" + run(b"a", size))


def iccp(size: int) -> bytes:
    return chunk(b"iCCP", b"Display P3\x00\x00" + run(b"p", size))


def itxt(size: int, compressed: bool) -> bytes:
    text = run(b"x", size) if compressed else b"x" * size
    return chunk(b"iTXt", b"XML:com.adobe.xmp\x00" + bytes([int(compressed), 0]) + b"en\x00\x00" + text)
