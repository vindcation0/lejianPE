"""解析 libsportaction.so 的 ELF 符号表，定位 Rengis.hs 并评估是否可静态还原。"""
import struct, math, os

APK = r"C:\Users\vindacation\Documents\deepseek-harness\lejianPE\captures\apk"
LIBS = [
    r"lib\arm64-v8a\libsportaction.so",
    r"lib\armeabi-v7a\libsportaction.so",
    r"lib\arm64-v8a\libdexvmp.so",
]


def entropy(b):
    if not b:
        return 0.0
    freq = [0] * 256
    for x in b:
        freq[x] += 1
    n = len(b)
    return -sum((c / n) * math.log2(c / n) for c in freq if c)


def parse_elf(path):
    with open(path, "rb") as f:
        data = f.read()
    if data[:4] != b"\x7fELF":
        return None, "not ELF"
    is64 = data[4] == 2
    end = "<" if data[5] == 1 else ">"
    if is64:
        e_shoff, = struct.unpack_from(end + "Q", data, 0x28)
        e_shentsize, e_shnum = struct.unpack_from(end + "HH", data, 0x3A)
    else:
        e_shoff, = struct.unpack_from(end + "I", data, 0x20)
        e_shentsize, e_shnum = struct.unpack_from(end + "HH", data, 0x2E)

    secs = []
    for i in range(e_shnum):
        off = e_shoff + i * e_shentsize
        if is64:
            name, typ, flags, addr, offset, size, link, info, align, entsize = \
                struct.unpack_from(end + "IIQQQQIIQQ", data, off)
        else:
            name, typ, flags, addr, offset, size, link, info, align, entsize = \
                struct.unpack_from(end + "IIIIIIIIII", data, off)
        secs.append(dict(name=name, type=typ, addr=addr, offset=offset,
                         size=size, link=link, entsize=entsize))
    return (data, is64, end, secs), None


def scan_symbols(path):
    info, err = parse_elf(path)
    if err:
        print(f"  {path}: {err}")
        return []
    data, is64, end, secs = info
    out = []
    for s in secs:
        if s["type"] != 11:  # SHT_DYNSYM
            continue
        strtab = secs[s["link"]]
        n = s["size"] // s["entsize"] if s["entsize"] else 0
        for i in range(n):
            off = s["offset"] + i * s["entsize"]
            try:
                if is64:
                    nameoff, = struct.unpack_from(end + "I", data, off)
                    value, size = struct.unpack_from(end + "QQ", data, off + 8)
                else:
                    nameoff, = struct.unpack_from(end + "I", data, off)
                    value, size = struct.unpack_from(end + "II", data, off + 4)
            except struct.error:
                continue
            if not nameoff:
                continue
            st = strtab["offset"] + nameoff
            e = data.find(b"\x00", st)
            name = data[st:e].decode("utf-8", "ignore")
            if "Rengis" in name or "hs" == name:
                out.append((name, value, size))
    return out


for lib in LIBS:
    p = os.path.join(APK, lib)
    print("=" * 72)
    print(lib, f"({os.path.getsize(p)/1024:.0f} KB)")
    print("=" * 72)
    syms = scan_symbols(p)
    if not syms:
        print("  未找到 Rengis 相关动态符号")
    for name, val, size in syms:
        print(f"  {name}  value=0x{val:x}  size={size}")
    print()
