"""用程序头（段）分析被破坏节区表的 .so，判断代码段是否加密。"""
import struct, math, os

APK = r"C:\Users\vindacation\Documents\deepseek-harness\lejianPE\captures\apk"
FILES = [r"lib\armeabi-v7a\libsportaction.so",
         r"lib\arm64-v8a\libsportaction.so",
         r"lib\armeabi-v7a\libsecurity.so"]
PT_NAMES = {1: "PT_LOAD", 2: "PT_DYNAMIC", 6: "PT_PHDR", 7: "PT_TLS"}


def entropy(b):
    if not b:
        return 0.0
    freq = [0] * 256
    for x in b:
        freq[x] += 1
    n = len(b)
    return -sum((c / n) * math.log2(c / n) for c in freq if c)


def profile(seg, step=8192):
    out = []
    for i in range(0, len(seg), step):
        chunk = seg[i:i + step]
        if len(chunk) < 1024:
            break
        out.append(entropy(chunk))
    return out


for rel in FILES:
    p = os.path.join(APK, rel)
    d = open(p, "rb").read()
    is64 = d[4] == 2
    end = "<" if d[5] == 1 else ">"
    if is64:
        phoff, = struct.unpack_from(end + "Q", d, 0x20)
        phentsize, phnum = struct.unpack_from(end + "HH", d, 0x36)
    else:
        phoff, = struct.unpack_from(end + "I", d, 0x1C)
        phentsize, phnum = struct.unpack_from(end + "HH", d, 0x2A)

    print("=" * 76)
    print(f"{rel}  ({len(d)/1024:.0f} KB, {'64' if is64 else '32'} 位, {phnum} 个段)")
    print("=" * 76)

    if not (0 < phoff < len(d)) or phnum > 40:
        print("  程序头不可信")
        continue

    for i in range(phnum):
        o = phoff + i * phentsize
        try:
            if is64:
                typ, flags, off, vaddr, paddr, filesz, memsz, align = \
                    struct.unpack_from(end + "IIQQQQQQ", d, o)
            else:
                typ, off, vaddr, paddr, filesz, memsz, flags, align = \
                    struct.unpack_from(end + "IIIIIIII", d, o)
        except struct.error:
            continue
        if typ != 1 or filesz < 4096:
            continue
        seg = d[off:off + filesz]
        e = entropy(seg)
        prof = profile(seg)
        hi = sum(1 for x in prof if x > 7.2)
        flagstr = ("R" if flags & 4 else "-") + ("W" if flags & 2 else "-") + ("X" if flags & 1 else "-")
        print(f"  PT_LOAD off=0x{off:<8x} vaddr=0x{vaddr:<10x} size={filesz/1024:7.1f}KB "
              f"perm={flagstr} entropy={e:.3f}")
        if prof:
            print(f"            窗口最高熵 {max(prof):.2f}，高熵窗口 {hi}/{len(prof)}")
    print()
