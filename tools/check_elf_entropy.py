"""检查 libsportaction.so 的节区熵分布，判断 ToHS/SHA1 所在代码是否被加密。"""
import struct, math, os, re

APK = r"C:\Users\vindacation\Documents\deepseek-harness\lejianPE\captures\apk"
FILES = [r"lib\armeabi-v7a\libsportaction.so", r"lib\arm64-v8a\libsportaction.so"]


def entropy(b):
    if not b:
        return 0.0
    freq = [0] * 256
    for x in b:
        freq[x] += 1
    n = len(b)
    return -sum((c / n) * math.log2(c / n) for c in freq if c)


def parse(path):
    data = open(path, "rb").read()
    is64 = data[4] == 2
    end = "<" if data[5] == 1 else ">"
    if is64:
        shoff, = struct.unpack_from(end + "Q", data, 0x28)
        shentsize, shnum, shstrndx = struct.unpack_from(end + "HHH", data, 0x3A)
    else:
        shoff, = struct.unpack_from(end + "I", data, 0x20)
        shentsize, shnum, shstrndx = struct.unpack_from(end + "HHH", data, 0x2E)

    secs = []
    for i in range(shnum):
        o = shoff + i * shentsize
        if is64:
            nm, typ, fl, addr, off, size, link, inf, al, ent = struct.unpack_from(end + "IIQQQQIIQQ", data, o)
        else:
            nm, typ, fl, addr, off, size, link, inf, al, ent = struct.unpack_from(end + "IIIIIIIIII", data, o)
        secs.append(dict(nm=nm, typ=typ, addr=addr, off=off, size=size, link=link, ent=ent))
    strsec = secs[shstrndx]
    for s in secs:
        st = strsec["off"] + s["nm"]
        e = data.find(b"\x00", st)
        s["name"] = data[st:e].decode("utf-8", "ignore")
    return data, is64, end, secs


def find_syms(data, is64, end, secs, wanted):
    found = []
    for s in secs:
        if s["typ"] not in (2, 11):
            continue
        strtab = secs[s["link"]]
        entsize = s["ent"]
        if not entsize:
            continue
        n = s["size"] // entsize
        for i in range(n):
            o = s["off"] + i * entsize
            try:
                if is64:
                    nameoff, info, other, shndx, value, size = struct.unpack_from(end + "IBBHQQ", data, o)
                else:
                    nameoff, value, size, info, other, shndx = struct.unpack_from(end + "IIIIBB", data, o)
            except struct.error:
                continue
            if not nameoff:
                continue
            st = strtab["off"] + nameoff
            e = data.find(b"\x00", st)
            nm = data[st:e].decode("utf-8", "ignore")
            for w in wanted:
                if w in nm:
                    found.append((nm, value, size))
                    break
    return found


for rel in FILES:
    p = os.path.join(APK, rel)
    data, is64, end, secs = parse(p)
    print("=" * 74)
    print(rel, f"({len(data)/1024:.0f} KB, {'64' if is64 else '32'} 位)")
    print("=" * 74)

    print("  主要节区熵：")
    for s in secs:
        if s["name"] in (".text", ".rodata", ".data", ".data.rel.ro", ".rdata"):
            if s["size"] < 512:
                continue
            body = data[s["off"]: s["off"] + s["size"]]
            print(f"    {s['name']:<14} addr=0x{s['addr']:<8x} size={s['size']/1024:8.1f}KB  "
                  f"entropy={entropy(body):.3f}")

    syms = find_syms(data, is64, end, secs, ["ToHS", "SHA1", "Rengis", "ByteToHexStr"])
    print("\n  相关符号：")
    if not syms:
        print("    （无 symtab 符号，可能已 strip）")
    for nm, val, size in syms[:12]:
        print(f"    {nm:<45} 0x{val:<10x} size={size}")
    print()
