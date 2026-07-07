# -*- coding: utf-8 -*-
"""
Fallback .pages text recovery — stdlib only, no network.

Apple's modern iWork format stores content as Snappy-compressed protobuf in
Index/*.iwa. When the Pages/osascript export path is unavailable (Pages not
installed, locked bundle), this recovers the cell *text* (a flat list — row/column
grouping is lost, but enough to view/search). Used only as extract_pages.py's
last resort.
"""

import os, zipfile, io


# ---- minimal Snappy block decompressor ----
def _snappy(data):
    out = bytearray()
    i, n = 0, len(data)
    # uncompressed length (varint) — skip
    shift = 0
    while i < n:
        b = data[i]; i += 1
        if not (b & 0x80): break
        shift += 7
    while i < n:
        tag = data[i]; i += 1
        t = tag & 0x03
        if t == 0:  # literal
            ln = tag >> 2
            if ln >= 60:
                k = ln - 59
                ln = int.from_bytes(data[i:i+k], "little"); i += k
            ln += 1
            out += data[i:i+ln]; i += ln
        else:
            if t == 1:
                length = 4 + ((tag >> 2) & 0x07)
                offset = ((tag >> 5) << 8) | data[i]; i += 1
            elif t == 2:
                length = 1 + (tag >> 2)
                offset = int.from_bytes(data[i:i+2], "little"); i += 2
            else:
                length = 1 + (tag >> 2)
                offset = int.from_bytes(data[i:i+4], "little"); i += 4
            if offset == 0 or offset > len(out):
                break
            start = len(out) - offset
            for j in range(length):
                out.append(out[start + j])
    return bytes(out)


def _iwa_chunks(raw):
    """Yield decompressed protobuf bytes from an .iwa stream (0x00 + 3-byte LE len + snappy)."""
    i, n = 0, len(raw)
    while i + 4 <= n:
        if raw[i] != 0x00:
            break
        ln = int.from_bytes(raw[i+1:i+4], "little"); i += 4
        block = raw[i:i+ln]; i += ln
        if not block:
            break
        try:
            yield _snappy(block)
        except Exception:
            continue


def _strings_from_protobuf(buf):
    """Walk protobuf wire format; yield length-delimited fields that look like text."""
    i, n = 0, len(buf)
    while i < n:
        # tag varint
        tag, shift = 0, 0
        while i < n:
            b = buf[i]; i += 1
            tag |= (b & 0x7F) << shift; shift += 7
            if not (b & 0x80): break
        wire = tag & 0x07
        if wire == 0:      # varint
            while i < n and (buf[i] & 0x80): i += 1
            i += 1
        elif wire == 1:    # 64-bit
            i += 8
        elif wire == 5:    # 32-bit
            i += 4
        elif wire == 2:    # length-delimited -> candidate string
            ln, shift = 0, 0
            while i < n:
                b = buf[i]; i += 1
                ln |= (b & 0x7F) << shift; shift += 7
                if not (b & 0x80): break
            chunk = buf[i:i+ln]; i += ln
            try:
                s = chunk.decode("utf-8")
            except Exception:
                continue
            printable = sum(1 for c in s if c.isprintable() or c in "\n\t")
            if len(s) >= 2 and printable >= len(s) * 0.9 and any(c.isalnum() for c in s):
                yield s.strip()
        else:
            break


def extract_text(pages_path):
    iwa_blobs = []
    if os.path.isdir(pages_path):
        idx_zip = os.path.join(pages_path, "Index.zip")
        if os.path.exists(idx_zip):
            with zipfile.ZipFile(idx_zip) as z:
                for name in z.namelist():
                    if name.endswith(".iwa"): iwa_blobs.append(z.read(name))
        else:
            idx = os.path.join(pages_path, "Index")
            for root, _, files in os.walk(idx):
                for fn in files:
                    if fn.endswith(".iwa"):
                        with open(os.path.join(root, fn), "rb") as f: iwa_blobs.append(f.read())
    else:
        with zipfile.ZipFile(pages_path) as z:
            for name in z.namelist():
                if name.endswith(".iwa"): iwa_blobs.append(z.read(name))
            if not iwa_blobs and "Index.zip" in z.namelist():
                with zipfile.ZipFile(io.BytesIO(z.read("Index.zip"))) as z2:
                    for name in z2.namelist():
                        if name.endswith(".iwa"): iwa_blobs.append(z2.read(name))

    seen, lines = set(), []
    for raw in iwa_blobs:
        for proto in _iwa_chunks(raw):
            for s in _strings_from_protobuf(proto):
                if s and s not in seen and not s.startswith(("http", "com.apple", "{\\")):
                    seen.add(s); lines.append(s)
    return "\n".join(lines)


if __name__ == "__main__":
    import sys
    print(extract_text(sys.argv[1])[:3000])
