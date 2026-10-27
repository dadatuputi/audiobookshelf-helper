"""Write tags into an MP4 (.m4b/.m4a) without rewriting the audio.

Pure Python on purpose, like tags.py: the helper runs on whatever Python the
browser finds, and must not need mutagen or ffmpeg to work.

An MP4 is a list of boxes. The tags live in moov/udta/meta/ilst, next to the
sample tables, and the audio is in mdat. Those tables hold absolute offsets
into the file (stco/co64), so moov cannot just grow in place when it sits in
front of mdat: everything after it would move and every offset go stale.

So the tags are written with room to spare. A `free` box follows ilst inside
meta, and a later change takes or gives back bytes from it: ilst and free
together keep their size, nothing else in the file moves, and only those few
bytes are written. That is what lets a change of tags reach the player as a
few hundred bytes rather than a whole book.

Two ways to apply the same tags, for two situations:

    rebuild(src, tags)   a whole new copy, as segments of the source plus the
                         new moov. moov is replaced where it stands, the chunk
                         offsets shifted if it grew, padded to a round size.
                         Deterministic: the same source and tags give the
                         same bytes, so a forced recopy can compare them
                         against the player block by block.

    retag(path, tags)    a file already on the player, edited in place. Uses
                         the free box when the tags fit (the common case);
                         otherwise rewrites moov where it is if nothing
                         follows it, and failing that appends a new moov and
                         turns the old one into `free`. Each way leaves a
                         playable file if it is cut off partway.
"""
import hashlib
import json
import struct

FORMAT = 1                  # bump when the bytes written for the same tags change
PAD = 2048                  # free bytes left after ilst, at least
ROUND = 4096                # a rebuilt moov is a multiple of this
TEXT, BINARY = 1, 0         # well-known data types in an ilst `data` box

# What Audiobookshelf's own "Embed Metadata" writes, through ffmpeg's mp4
# muxer: the ffmetadata key on the left, the ilst box it becomes on the right.
# (server/utils/ffmpegHelpers.js getFFMetadataObject; libavformat/movenc.c.)
KEYS = {
    "title": b"\xa9nam",
    "artist": b"\xa9ART",
    "album_artist": b"aART",
    "album": b"\xa9alb",
    "genre": b"\xa9gen",
    "date": b"\xa9day",
    "comment": b"\xa9cmt",
    "description": b"desc",
    "composer": b"\xa9wrt",
    "copyright": b"cprt",
    "grouping": b"\xa9grp",
    "track": b"trkn",
}

# Boxes to walk through on the way to the chunk offset tables.
_TABLE_PATH = {b"moov", b"trak", b"mdia", b"minf", b"stbl"}


class NotMp4(ValueError):
    """The file is not an MP4 this module can edit; copy it as it is."""


# ------------------------------------------------------------------ boxes
def _boxes(buf, start, end):
    """(type, offset, size, header length) for each box in buf[start:end]."""
    out = []
    i = start
    while i + 8 <= end:
        size, kind = struct.unpack(">I4s", buf[i:i + 8])
        hdr = 8
        if size == 1:
            if i + 16 > end:
                raise NotMp4("truncated box header")
            size = struct.unpack(">Q", buf[i + 8:i + 16])[0]
            hdr = 16
        elif size == 0:
            size = end - i
        if size < hdr or i + size > end:
            raise NotMp4(f"bad {kind!r} box at {i}")
        out.append((kind, i, size, hdr))
        i += size
    return out


def top_level(f):
    """The file's top-level boxes, read with seeks rather than loaded."""
    f.seek(0, 2)
    length = f.tell()
    out = []
    i = 0
    while i + 8 <= length:
        f.seek(i)
        head = f.read(16)
        size, kind = struct.unpack(">I4s", head[:8])
        hdr = 8
        if size == 1:
            size = struct.unpack(">Q", head[8:16])[0]
            hdr = 16
        elif size == 0:
            size = length - i
        if size < hdr or i + size > length:
            raise NotMp4(f"bad {kind!r} box at {i}")
        out.append((kind, i, size, hdr))
        i += size
    if i != length:
        raise NotMp4("trailing bytes after the last box")
    kinds = [b[0] for b in out]
    if b"moov" not in kinds or b"ftyp" not in kinds[:2]:
        raise NotMp4("no ftyp/moov")
    return out, length


def _box(kind, payload):
    return struct.pack(">I4s", 8 + len(payload), kind) + payload


def _free(size):
    """A free box of exactly `size` bytes (at least 8)."""
    return struct.pack(">I4s", size, b"free") + b"\0" * (size - 8)


def _meta_children_start(buf, off, hdr):
    """Where meta's children begin. iTunes' meta is a full box (4 bytes of
    version and flags before the children); QuickTime's is not."""
    body = off + hdr
    if buf[body + 4:body + 8] in (b"hdlr", b"ilst", b"free", b"keys"):
        return body                      # QuickTime style
    return body + 4


# ------------------------------------------------------------------- tags
def item_box(key, value):
    """One ilst entry: a box named after the tag holding a `data` box."""
    if key in (b"trkn", b"disk"):
        n, total = value if isinstance(value, tuple) else (int(value), 0)
        data = struct.pack(">IIHHHH", BINARY, 0, 0, n, total, 0)
        if key == b"disk":
            data = data[:-2]
    else:
        data = struct.pack(">II", TEXT, 0) + str(value).encode("utf-8")
    return _box(key, _box(b"data", data))


def _ilst(old_items, tags):
    """The new ilst payload: old entries kept in their order, those named in
    `tags` replaced, new ones after. Mirrors ffmpeg's -map_metadata 1 then 0:
    what is given wins, what is not given stays."""
    out = []
    done = set()
    for kind, raw in old_items:
        if kind in tags:
            if kind not in done:
                out.append(item_box(kind, tags[kind]))
                done.add(kind)
        else:
            out.append(raw)
    for kind, value in tags.items():
        if kind not in done:
            out.append(item_box(kind, value))
    return b"".join(out)


def tags_digest(tags):
    """What was written, short enough for the index: same tags, same digest."""
    flat = sorted((k.decode("latin-1"), list(v) if isinstance(v, tuple) else v)
                  for k, v in tags.items())
    raw = json.dumps([FORMAT, flat], ensure_ascii=False).encode("utf-8")
    return hashlib.sha1(raw).hexdigest()[:16]


# ------------------------------------------------------------- moov edits
class _Moov:
    """A parsed moov, enough to find ilst and its free neighbour."""

    def __init__(self, buf):
        self.buf = buf
        moov = _boxes(buf, 0, len(buf))[0]
        self.hdr = moov[3]
        kids = _boxes(buf, self.hdr, len(buf))
        self.udta = next((k for k in kids if k[0] == b"udta"), None)
        self.meta = self.ilst = self.free = None
        self.meta_start = None
        if self.udta:
            u = self.udta
            ukids = _boxes(buf, u[1] + u[3], u[1] + u[2])
            self.meta = next((k for k in ukids if k[0] == b"meta"), None)
        if self.meta:
            m = self.meta
            self.meta_start = _meta_children_start(buf, m[1], m[3])
            mkids = _boxes(buf, self.meta_start, m[1] + m[2])
            for n, k in enumerate(mkids):
                if k[0] == b"ilst":
                    self.ilst = k
                    nxt = mkids[n + 1] if n + 1 < len(mkids) else None
                    if nxt and nxt[0] == b"free":
                        self.free = nxt
                    break

    def items(self):
        if not self.ilst:
            return []
        i = self.ilst
        return [(k[0], self.buf[k[1]:k[1] + k[2]]) for k in _boxes(self.buf, i[1] + i[3], i[1] + i[2])]

    def with_tags(self, tags, pad):
        """The whole moov rebuilt with these tags and `pad` free bytes after
        ilst. Every box outside udta/meta/ilst is kept byte for byte."""
        buf = self.buf
        ilst = _box(b"ilst", _ilst(self.items(), tags))
        free = _free(pad) if pad else b""
        hdlr = _box(b"hdlr", b"\0" * 4 + b"\0" * 4 + b"mdirappl" + b"\0" * 9)
        if self.meta:
            m = self.meta
            full = self.meta_start != m[1] + m[3]
            kept = [buf[k[1]:k[1] + k[2]] for k in _boxes(buf, self.meta_start, m[1] + m[2])
                    if k[0] not in (b"ilst",) and not (self.free and k[1] == self.free[1])]
            # ilst and its padding go right after hdlr (or first, if there is
            # no hdlr), where retag() will look for them next time.
            at = next((n + 1 for n, raw in enumerate(kept) if raw[4:8] == b"hdlr"), 0)
            kids = kept[:at] + [ilst, free] + kept[at:]
            meta = _box(b"meta", (b"\0" * 4 if full else b"") + b"".join(kids))
        else:
            meta = _box(b"meta", b"\0" * 4 + hdlr + ilst + free)
        if self.udta:
            u = self.udta
            ukids = []
            placed = False
            for k in _boxes(buf, u[1] + u[3], u[1] + u[2]):
                if k[0] == b"meta" and not placed:
                    ukids.append(meta)
                    placed = True
                else:
                    ukids.append(buf[k[1]:k[1] + k[2]])
            if not placed:
                ukids.append(meta)
            udta = _box(b"udta", b"".join(ukids))
        else:
            udta = _box(b"udta", meta)
        mkids = []
        placed = False
        for k in _boxes(buf, self.hdr, len(buf)):
            if k[0] == b"udta" and not placed:
                mkids.append(udta)
                placed = True
            else:
                mkids.append(buf[k[1]:k[1] + k[2]])
        if not placed:
            mkids.append(udta)
        return _box(b"moov", b"".join(mkids))


def _shift_chunk_offsets(moov, after, delta):
    """Add `delta` to every chunk offset that points past `after`. Returns
    None if an offset would no longer fit in stco's 32 bits."""
    buf = bytearray(moov)

    def walk(start, end):
        for kind, off, size, hdr in _boxes(buf, start, end):
            if kind in _TABLE_PATH:
                if walk(off + hdr, off + size) is False:
                    return False
            elif kind in (b"stco", b"co64"):
                body = off + hdr
                count = struct.unpack(">I", buf[body + 4:body + 8])[0]
                wide = kind == b"co64"
                step = 8 if wide else 4
                fmt = ">Q" if wide else ">I"
                for n in range(count):
                    p = body + 8 + n * step
                    v = struct.unpack(fmt, buf[p:p + step])[0]
                    if v > after:
                        v += delta
                        if not wide and v > 0xFFFFFFFF:
                            return False
                        struct.pack_into(fmt, buf, p, v)
        return True

    hdr = _boxes(buf, 0, len(buf))[0][3]
    if walk(hdr, len(buf)) is False:
        return None
    return bytes(buf)


def _read_moov(f, boxes):
    moov = next(b for b in boxes if b[0] == b"moov")
    f.seek(moov[1])
    buf = f.read(moov[2])
    if len(buf) != moov[2]:
        raise NotMp4("short read")
    return moov, buf


def rebuild(f, tags):
    """The tagged file as segments: ("src", start, end) ranges of `f` and
    ("bytes", b) inserts. Concatenated, they are the new file."""
    boxes, length = top_level(f)
    moov, buf = _read_moov(f, boxes)
    m = _Moov(buf)
    base = len(m.with_tags(tags, 0))
    size = -(-(base + PAD + 8) // ROUND) * ROUND
    new = m.with_tags(tags, size - base)
    delta = len(new) - moov[2]
    if delta:
        shifted = _shift_chunk_offsets(new, moov[1], delta)
        if shifted is None:                  # past 4 GB: leave moov's place alone
            return [("src", 0, moov[1]), ("bytes", _free(moov[2])[:8]),
                    ("src", moov[1] + 8, length), ("bytes", new)]
        new = shifted
    return [("src", 0, moov[1]), ("bytes", new), ("src", moov[1] + moov[2], length)]


def segments_length(segs):
    return sum((s[2] - s[1]) if s[0] == "src" else len(s[1]) for s in segs)


def retag(f, tags):
    """Writes that put `tags` into the open file `f` in place, in the order
    to apply them, and the length to truncate to (None to leave it).

    Each write is (offset, bytes). Apply them in order: every prefix of the
    list leaves a valid file, so a player pulled out midway still plays."""
    boxes, length = top_level(f)
    moov, buf = _read_moov(f, boxes)
    m = _Moov(buf)

    # 1. The common case: the new ilst fits where ilst and its free box are.
    if m.ilst:
        span = m.ilst[2] + (m.free[2] if m.free else 0)
        ilst = _box(b"ilst", _ilst(m.items(), tags))
        room = span - len(ilst)
        if room == 0 or room >= 8:
            data = ilst + (_free(room) if room else b"")
            return [(moov[1] + m.ilst[1], data)], None

    new = m.with_tags(tags, PAD)
    after = [b for b in boxes if b[1] > moov[1]]
    # 2. moov is last, or only free boxes follow it: rewrite it where it is.
    if all(b[0] == b"free" for b in after):
        room = length - moov[1]
        if len(new) <= room - 8 or len(new) == room:
            tail = _free(room - len(new)) if room > len(new) else b""
            return [(moov[1] + len(new), tail), (moov[1], new)] if tail else [(moov[1], new)], None
        return [(moov[1], new)], moov[1] + len(new)
    # 3. Something follows it (mdat, usually): add the new moov at the end,
    # then retire the old one. Until that last 4-byte write the old moov is
    # first in the file, so readers still use it.
    return [(length, new), (moov[1] + 4, b"free")], None


def read_tags(f):
    """{box name: text} for the text tags in the file. For tests and checks."""
    boxes, _ = top_level(f)
    _, buf = _read_moov(f, boxes)
    out = {}
    for kind, raw in _Moov(buf).items():
        inner = _boxes(raw, 8, len(raw))
        data = next((b for b in inner if b[0] == b"data"), None)
        if not data:
            continue
        payload = raw[data[1] + data[3]:data[1] + data[2]]
        dtype = struct.unpack(">I", payload[:4])[0] & 0xFFFFFF
        value = payload[8:]
        if dtype == TEXT:
            out[kind] = value.decode("utf-8", "replace")
        elif kind in (b"trkn", b"disk") and len(value) >= 6:
            out[kind] = struct.unpack(">HH", value[2:6])
    return out
