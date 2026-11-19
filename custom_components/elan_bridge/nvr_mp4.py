"""MP4 -> H.264 frames for the ELAN NVR emulator. No Home Assistant imports here.

Handles plain (moov + stbl tables) and fragmented (moof/trun) MP4 and yields Annex-B NAL units with their time.
H.265 (hvc1/hev1) is reported by codec name, not decoded.
"""

from __future__ import annotations

import struct

START_CODE = b"\x00\x00\x00\x01"


# ---------------------------------------------------------------- MP4 demux
def _boxes(buf, start=0, end=None):
    end = len(buf) if end is None else end
    i = start
    while i + 8 <= end:
        size, typ = struct.unpack_from(">I4s", buf, i)
        hdr = 8
        if size == 1:
            size, = struct.unpack_from(">Q", buf, i + 8)
            hdr = 16
        elif size == 0:
            size = end - i
        if size < hdr or i + size > end:
            size = end - i      # truncated tail (e.g. partial download): take what is there
            if size < hdr:
                return
        yield typ.decode("latin-1"), i + hdr, i + size
        i += size


def _child(buf, s, e, path):
    for name in path.split("/"):
        for typ, cs, ce in _boxes(buf, s, e):
            if typ == name:
                s, e = cs, ce
                break
        else:
            return None
    return s, e


class Track:
    def __init__(self):
        self.tid = None
        self.timescale = 90000
        self.codec = None
        self.nal_len = 4
        self.params = []        # SPS, PPS as raw NALs
        self.samples = []       # (dts_seconds, offset, size, is_key)


def _parse_avcc(buf, s, e, tr):
    p = s
    tr.nal_len = (buf[p + 4] & 3) + 1
    nsps = buf[p + 5] & 0x1f
    p += 6
    for _ in range(nsps):
        n, = struct.unpack_from(">H", buf, p)
        tr.params.append(bytes(buf[p + 2:p + 2 + n]))
        p += 2 + n
    npps = buf[p]
    p += 1
    for _ in range(npps):
        n, = struct.unpack_from(">H", buf, p)
        tr.params.append(bytes(buf[p + 2:p + 2 + n]))
        p += 2 + n


def _video_track(buf, ts, te):
    tr = Track()
    tkhd = _child(buf, ts, te, "tkhd")
    if tkhd:
        v = buf[tkhd[0]]
        tr.tid, = struct.unpack_from(">I", buf, tkhd[0] + (20 if v == 1 else 12))
    mdhd = _child(buf, ts, te, "mdia/mdhd")
    if mdhd:
        v = buf[mdhd[0]]
        tr.timescale, = struct.unpack_from(">I", buf, mdhd[0] + (20 if v == 1 else 12))
    hdlr = _child(buf, ts, te, "mdia/hdlr")
    if not hdlr or buf[hdlr[0] + 8:hdlr[0] + 12] != b"vide":
        return None
    stsd = _child(buf, ts, te, "mdia/minf/stbl/stsd")
    if stsd:
        for typ, cs, ce in _boxes(buf, stsd[0] + 8, stsd[1]):
            tr.codec = typ
            if typ in ("avc1", "avc3"):
                for t2, s2, e2 in _boxes(buf, cs + 78, ce):
                    if t2 == "avcC":
                        _parse_avcc(buf, s2, e2, tr)
            break
    stbl = _child(buf, ts, te, "mdia/minf/stbl")
    if stbl:
        _sample_table(buf, stbl, tr)
    return tr


def _sample_table(buf, stbl, tr):
    tab = {typ: (cs, ce) for typ, cs, ce in _boxes(buf, *stbl)}
    if "stsz" not in tab:
        return
    s, _ = tab["stsz"]
    fixed, count = struct.unpack_from(">II", buf, s + 4)
    sizes = [fixed] * count if fixed else list(struct.unpack_from(f">{count}I", buf, s + 12))
    if count == 0:
        return
    # chunk offsets
    if "stco" in tab:
        s, _ = tab["stco"]
        n, = struct.unpack_from(">I", buf, s + 4)
        offs = struct.unpack_from(f">{n}I", buf, s + 8)
    else:
        s, _ = tab["co64"]
        n, = struct.unpack_from(">I", buf, s + 4)
        offs = struct.unpack_from(f">{n}Q", buf, s + 8)
    s, _ = tab["stsc"]
    n, = struct.unpack_from(">I", buf, s + 4)
    stsc = [struct.unpack_from(">III", buf, s + 8 + 12 * k) for k in range(n)]
    # durations
    s, _ = tab["stts"]
    n, = struct.unpack_from(">I", buf, s + 4)
    durs = []
    for k in range(n):
        c, d = struct.unpack_from(">II", buf, s + 8 + 8 * k)
        durs.extend([d] * c)
    keys = None
    if "stss" in tab:
        s, _ = tab["stss"]
        n, = struct.unpack_from(">I", buf, s + 4)
        keys = set(struct.unpack_from(f">{n}I", buf, s + 8))
    # walk chunks
    si, t = 0, 0
    for ci, off in enumerate(offs, start=1):
        per = next((spc for first, spc, _ in reversed(stsc) if first <= ci), stsc[0][1])
        for _ in range(per):
            if si >= count:
                break
            tr.samples.append((t / tr.timescale, off, sizes[si], keys is None or (si + 1) in keys))
            t += durs[si] if si < len(durs) else (durs[-1] if durs else 0)
            off += sizes[si]
            si += 1


def _fragments(buf, tr, trex_default):
    """Append samples from every moof/traf for this track (fragmented MP4)."""
    t_next = tr.samples[-1][0] * tr.timescale if tr.samples else 0
    for typ, ms, me in _boxes(buf):
        if typ != "moof":
            continue
        moof_start = ms - 8
        for t2, ts, te in _boxes(buf, ms, me):
            if t2 != "traf":
                continue
            tfhd = _child(buf, ts, te, "tfhd")
            flags = int.from_bytes(buf[tfhd[0] + 1:tfhd[0] + 4], "big")
            tid, = struct.unpack_from(">I", buf, tfhd[0] + 4)
            if tr.tid is not None and tid != tr.tid:
                continue
            p = tfhd[0] + 8
            base = moof_start
            if flags & 0x1:
                base, = struct.unpack_from(">Q", buf, p); p += 8
            if flags & 0x2:
                p += 4
            d_dur, d_size, d_flags = trex_default
            if flags & 0x8:
                d_dur, = struct.unpack_from(">I", buf, p); p += 4
            if flags & 0x10:
                d_size, = struct.unpack_from(">I", buf, p); p += 4
            if flags & 0x20:
                d_flags, = struct.unpack_from(">I", buf, p); p += 4
            tfdt = _child(buf, ts, te, "tfdt")
            if tfdt:
                v = buf[tfdt[0]]
                t_next = struct.unpack_from(">Q" if v == 1 else ">I", buf, tfdt[0] + 4)[0]
            for t3, rs, re_ in _boxes(buf, ts, te):
                if t3 != "trun":
                    continue
                rflags = int.from_bytes(buf[rs + 1:rs + 4], "big")
                n, = struct.unpack_from(">I", buf, rs + 4)
                p = rs + 8
                off = base
                if rflags & 0x1:
                    do, = struct.unpack_from(">i", buf, p); p += 4
                    off = base + do
                first_flags = None
                if rflags & 0x4:
                    first_flags, = struct.unpack_from(">I", buf, p); p += 4
                for k in range(n):
                    dur, size, sflags = d_dur, d_size, d_flags
                    if rflags & 0x100:
                        dur, = struct.unpack_from(">I", buf, p); p += 4
                    if rflags & 0x200:
                        size, = struct.unpack_from(">I", buf, p); p += 4
                    if rflags & 0x400:
                        sflags, = struct.unpack_from(">I", buf, p); p += 4
                    if rflags & 0x800:
                        p += 4
                    if k == 0 and first_flags is not None:
                        sflags = first_flags
                    key = not (sflags >> 16) & 1    # sample_is_non_sync_sample
                    tr.samples.append((t_next / tr.timescale, off, size, key))
                    t_next += dur
                    off += size


def demux_h264(buf):
    """Parse an MP4. Returns (codec, [(t_seconds, nal_bytes_annexb, is_keyframe_start)]); t relative to file start."""
    buf = memoryview(buf) if not isinstance(buf, memoryview) else buf
    moov = _child(buf, 0, len(buf), "moov")
    if not moov:
        return None, []
    tr = None
    for typ, ts, te in _boxes(buf, *moov):
        if typ == "trak":
            tr = _video_track(buf, ts, te)
            if tr:
                break
    if not tr:
        return None, []
    trex = (0, 0, 0)
    for typ, ts, te in _boxes(buf, *moov):
        if typ == "mvex":
            for t2, s2, e2 in _boxes(buf, ts, te):
                if t2 == "trex":
                    trex = struct.unpack_from(">III", buf, s2 + 12)
    _fragments(buf, tr, trex)
    if tr.codec not in ("avc1", "avc3"):
        return tr.codec, []
    out = []
    for t, off, size, key in tr.samples:
        if off + size > len(buf):
            break
        if key:
            for p in tr.params:      # repeat SPS/PPS before every keyframe so any seek can start decoding
                out.append((t, START_CODE + p, True))
        p, end = off, off + size
        while p + tr.nal_len <= end:
            n = int.from_bytes(buf[p:p + tr.nal_len], "big")
            p += tr.nal_len
            nal = bytes(buf[p:p + n])
            p += n
            typ = nal[0] & 0x1f if nal else 0
            if typ in (7, 8) and key and tr.params:
                continue                 # already sent from avcC
            if typ == 9:
                continue                 # access unit delimiters: not needed
            out.append((t, START_CODE + nal, False))
    return tr.codec, out


def frames_of(nals):
    """Group NALs into one access unit per packet: the controller takes a packet that starts with an SPS (type 7)
    as a whole picture and hangs up after a preview frame, so SPS+PPS+IDR must travel together, as must all slices
    of one frame. Returns [(t, annexb_bytes, is_keyframe)]."""
    out = []
    for t, nal, is_param in nals:
        if out and out[-1][0] == t and not (is_param and not out[-1][3]):
            out[-1][1].append(nal)
        else:
            out.append([t, [nal], is_param, is_param])   # [t, parts, is_key, still_in_param_run]
        if not is_param:
            out[-1][3] = False
    return [(t, b"".join(parts), key) for t, parts, key, _ in out]
