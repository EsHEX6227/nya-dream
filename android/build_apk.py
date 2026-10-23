# -*- coding: utf-8 -*-
"""一键打安卓外壳 APK（不装 Android Studio、不用 Gradle）

流程（全用便携工具链）：
    aapt2 compile  ->  res.zip
    aapt2 link     ->  base.apk（含 R.java）
    javac          ->  classes
    d8             ->  classes.dex
    合并 dex + zipalign + apksigner 签名
产出：
    android/build/NyaDREAM_安卓外壳.apk
    交付/NyaDREAM_安卓外壳.apk（万一没有这个目录就跳过）

用法：
    python build_apk.py                 # 自动取本机局域网 IP 作为默认地址
    python build_apk.py --ip 192.168.0.102 --port 8788
    python build_apk.py --no-copy       # 不往交付目录拷
"""
import argparse
import hashlib
import os
import re
import shutil
import socket
import struct
import subprocess
import sys
import zipfile
import zlib
from pathlib import Path

HERE = Path(__file__).resolve().parent                  # 喵梦/android
PROJ = HERE.parent                                      # 喵梦
BUILD = HERE / 'build'
TOOLS = Path(r'C:\Users\17478\Desktop\EsDSHWork\apk-toolchain')
KEYSTORE = HERE / 'nyadream-shell.jks'
KS_PASS = 'nyadream2026'
KS_ALIAS = 'nyadream'
APK_NAME = 'NyaDREAM_安卓外壳.apk'


# ---------------------------------------------------------------- 小工具
def find(pat: str, root: Path = TOOLS):
    hits = sorted(root.glob(pat))
    return hits[0] if hits else None


def run(cmd, cwd=None, env=None, quiet=False):
    if not quiet:
        print('   $', cmd if isinstance(cmd, str) else ' '.join(str(c) for c in cmd))
    r = subprocess.run(cmd, cwd=str(cwd) if cwd else None, env=env, shell=isinstance(cmd, str),
                       capture_output=True, text=True, encoding='utf-8', errors='replace')
    if r.returncode != 0:
        print('   !! 退出码', r.returncode)
        print((r.stdout or '')[-2000:])
        print((r.stderr or '')[-2000:])
        raise SystemExit(1)
    if r.stdout and not quiet:
        tail = r.stdout.strip().splitlines()[-4:]
        for line in tail:
            print('     ', line)
    return r


def local_ip() -> str:
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(('8.8.8.8', 80))
        ip = s.getsockname()[0]
        s.close()
        return ip
    except Exception:
        return '127.0.0.1'


# ---------------------------------------------------------------- 画图标（不依赖任何库）
class Canvas:
    """超采样画布：先按 4 倍画，再缩回来，边缘就不毛了"""
    def __init__(self, size, ss=4):
        self.ss = ss
        self.n = size * ss
        self.px = bytearray(self.n * self.n * 4)
        self.size = size

    def put(self, x, y, rgba):
        if x < 0 or y < 0 or x >= self.n or y >= self.n:
            return
        i = (y * self.n + x) * 4
        r, g, b, a = rgba
        if a >= 255:
            self.px[i:i + 4] = bytes((r, g, b, 255))
        elif a > 0:
            sr, sg, sb, sa = self.px[i], self.px[i + 1], self.px[i + 2], self.px[i + 3]
            na = a + sa * (255 - a) // 255
            if na:
                self.px[i] = (r * a + sr * sa * (255 - a) // 255) // na
                self.px[i + 1] = (g * a + sg * sa * (255 - a) // 255) // na
                self.px[i + 2] = (b * a + sb * sa * (255 - a) // 255) // na
            self.px[i + 3] = na

    def f(self, v):                     # 逻辑坐标 -> 画布坐标
        return v * self.ss

    def rect(self, x0, y0, x1, y1, rgba, radius=0.0):
        X0, Y0, X1, Y1 = self.f(x0), self.f(y0), self.f(x1), self.f(y1)
        R = self.f(radius)
        for y in range(int(Y0), int(Y1) + 1):
            for x in range(int(X0), int(X1) + 1):
                if R > 0:
                    cx = min(max(x, X0 + R), X1 - R)
                    cy = min(max(y, Y0 + R), Y1 - R)
                    if (x - cx) ** 2 + (y - cy) ** 2 > R * R:
                        continue
                self.put(x, y, rgba)

    def circle(self, cx, cy, r, rgba):
        CX, CY, R = self.f(cx), self.f(cy), self.f(r)
        for y in range(int(CY - R), int(CY + R) + 1):
            for x in range(int(CX - R), int(CX + R) + 1):
                if (x - CX) ** 2 + (y - CY) ** 2 <= R * R:
                    self.put(x, y, rgba)

    def tri(self, ax, ay, bx, by, cx, cy, rgba):
        ax, ay, bx, by, cx, cy = (self.f(v) for v in (ax, ay, bx, by, cx, cy))
        x0, x1 = int(min(ax, bx, cx)), int(max(ax, bx, cx))
        y0, y1 = int(min(ay, by, cy)), int(max(ay, by, cy))
        area = (bx - ax) * (cy - ay) - (by - ay) * (cx - ax)
        if area == 0:
            return
        for y in range(y0, y1 + 1):
            for x in range(x0, x1 + 1):
                w0 = ((bx - ax) * (y - ay) - (by - ay) * (x - ax)) / area
                w1 = ((cx - bx) * (y - by) - (cy - by) * (x - bx)) / area
                w2 = ((ax - cx) * (y - cy) - (ay - cy) * (x - cx)) / area
                if w0 >= -0.02 and w1 >= -0.02 and w2 >= -0.02:
                    self.put(x, y, rgba)

    def down(self):
        n, ss = self.n, self.ss
        out = bytearray(self.size * self.size * 4)
        for y in range(self.size):
            for x in range(self.size):
                r = g = b = a = 0
                for dy in range(ss):
                    for dx in range(ss):
                        i = ((y * ss + dy) * n + (x * ss + dx)) * 4
                        r += self.px[i]; g += self.px[i + 1]; b += self.px[i + 2]; a += self.px[i + 3]
                k = ss * ss
                j = (y * self.size + x) * 4
                out[j] = r // k; out[j + 1] = g // k; out[j + 2] = b // k; out[j + 3] = a // k
        return bytes(out)


def write_png(path: Path, size: int, rgba: bytes):
    def chunk(tag, data):
        c = struct.pack('>I', len(data)) + tag + data
        return c + struct.pack('>I', zlib.crc32(tag + data) & 0xffffffff)

    raw = b''
    stride = size * 4
    for y in range(size):
        raw += b'\x00' + rgba[y * stride:(y + 1) * stride]
    png = (b'\x89PNG\r\n\x1a\n'
           + chunk(b'IHDR', struct.pack('>IIBBBBB', size, size, 8, 6, 0, 0, 0))
           + chunk(b'IDAT', zlib.compress(raw, 9))
           + chunk(b'IEND', b''))
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(png)


def cat_mark(c: Canvas, color, bg=None, pad=0.16, eye=None):
    """猫头：圆脸 + 两只三角耳朵（+ 可选两只眼睛）"""
    S = c.size
    ccx, ccy = S / 2, S / 2 + S * 0.03
    r = S * (0.5 - pad)
    c.tri(ccx - r * 0.86, ccy - r * 0.50, ccx - r * 1.02, ccy - r * 1.30,
          ccx - r * 0.14, ccy - r * 0.92, color)
    c.tri(ccx + r * 0.86, ccy - r * 0.50, ccx + r * 1.02, ccy - r * 1.30,
          ccx + r * 0.14, ccy - r * 0.92, color)
    c.circle(ccx, ccy, r, color)
    if eye:
        c.circle(ccx - r * 0.36, ccy - r * 0.06, r * 0.13, eye)
        c.circle(ccx + r * 0.36, ccy - r * 0.06, r * 0.13, eye)


def make_icons(res_dir: Path):
    pink = (245, 194, 231, 255)
    dark = (16, 17, 24, 255)
    white = (255, 255, 255, 255)
    made = 0
    for folder, size in (('mipmap-mdpi', 48), ('mipmap-hdpi', 72), ('mipmap-xhdpi', 96),
                         ('mipmap-xxhdpi', 144), ('mipmap-xxxhdpi', 192)):
        _src = Path(__file__).resolve().parent / 'assets' / 'icon_src.png'
        if _src.exists():
            from PIL import Image as _Im, ImageDraw as _ID
            _im = _Im.open(_src).convert('RGBA').resize((size, size), _Im.LANCZOS)
            _mk = _Im.new('L', (size, size), 0)
            _ID.Draw(_mk).rounded_rectangle([0, 0, size - 1, size - 1],
                                            radius=int(size * 0.225), fill=255)
            _out = _Im.new('RGBA', (size, size), (0, 0, 0, 0))
            _out.paste(_im, (0, 0), _mk)
            (res_dir / folder).mkdir(parents=True, exist_ok=True)
            _out.save(res_dir / folder / 'ic_launcher.png')
        else:
            c = Canvas(size)
            c.rect(0, 0, size, size, dark, radius=size * 0.22)
            cat_mark(c, pink, eye=dark, pad=0.20)
            write_png(res_dir / folder / 'ic_launcher.png', size, c.down())
        made += 1
    for folder, size in (('drawable-mdpi', 24), ('drawable-hdpi', 36),
                         ('drawable-xhdpi', 48), ('drawable-xxhdpi', 72)):
        c = Canvas(size)                                        # 通知栏图标：纯白 + 透明底
        cat_mark(c, white, eye=None, pad=0.06)
        write_png(res_dir / folder / 'ic_stat.png', size, c.down())
        made += 1
    return made


# ---------------------------------------------------------------- 构建
def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--ip', default='')
    ap.add_argument('--port', default='8787')
    ap.add_argument('--no-copy', action='store_true')
    args = ap.parse_args()

    ip = args.ip or local_ip()
    home = 'https://%s:%s/' % (ip, args.port)
    print('== 默认地址：', home)

    javac = find('jdk*/bin/javac.exe')
    keytool = find('jdk*/bin/keytool.exe')
    aapt2 = find('build-tools/**/aapt2.exe')
    d8 = find('build-tools/**/d8.bat')
    zipalign = find('build-tools/**/zipalign.exe')
    apksigner = find('build-tools/**/apksigner.bat')
    android_jar = find('platform/**/android.jar')
    missing = [n for n, v in (('javac', javac), ('keytool', keytool), ('aapt2', aapt2), ('d8', d8),
                              ('zipalign', zipalign), ('apksigner', apksigner),
                              ('android.jar', android_jar)) if not v]
    if missing:
        print('缺少工具：', missing)
        print('先跑一次： python get_toolchain.py')
        return 1
    jdk_bin = javac.parent
    env = dict(os.environ, JAVA_HOME=str(jdk_bin.parent), PATH=str(jdk_bin) + os.pathsep + os.environ.get('PATH', ''))

    for d in ('res', 'gen', 'classes', 'dex'):
        p = BUILD / d
        if p.exists():
            shutil.rmtree(p)
    BUILD.mkdir(parents=True, exist_ok=True)
    (BUILD / 'dex').mkdir(exist_ok=True)          # d8 要求输出目录已存在

    # 1) 资源：把 android/res 复制出来，再补上生成的图标
    res = BUILD / 'res'
    shutil.copytree(HERE / 'res', res)
    shutil.copyfile(HERE / 'AndroidManifest.xml', BUILD / 'AndroidManifest.xml')
    print('== 画图标：', make_icons(res), '个')

    # 2) 生成 ShellConfig（默认地址）
    cfg = HERE / 'src' / 'com' / 'nyadream' / 'shell' / 'ShellConfig.java'
    cfg.write_text('package com.nyadream.shell;\n\n'
                   '/** 打包时写死一个默认地址，用户第一次打开可以直接用，也能在菜单里改 */\n'
                   'public class ShellConfig {\n'
                   '    public static final String DEFAULT_HOME = "%s";\n'
                   '}\n' % home, encoding='utf-8')

    # aapt2 / zipalign 是原生程序，Windows 下不认中文绝对路径：
    # 统一在 build\ 里用相对路径跑（相对路径由系统按 cwd 解析，命令里就没有中文了）
    print('== aapt2 compile')
    run('"%s" compile --dir res -o res.zip' % aapt2, cwd=BUILD)
    print('== aapt2 link')
    run('"%s" link -o base.apk -I "%s" --manifest AndroidManifest.xml -R res.zip --auto-add-overlay '
        '--java gen --min-sdk-version 26 --target-sdk-version 34 --version-code 1 --version-name 1.0'
        % (aapt2, android_jar), cwd=BUILD)

    # 4) javac
    java_files = [str(p) for p in (HERE / 'src').rglob('*.java')] + \
                 [str(p) for p in (BUILD / 'gen').rglob('*.java')]
    print('== javac：%d 个源文件' % len(java_files))
    run('"%s" -J-Duser.language=en -encoding UTF-8 -source 8 -target 8 -nowarn -classpath "%s" -d "%s" %s'
        % (javac, android_jar, BUILD / 'classes', ' '.join('"%s"' % f for f in java_files)))

    class_files = [str(p.relative_to(BUILD)) for p in (BUILD / 'classes').rglob('*.class')]
    print('== d8：%d 个 class' % len(class_files))
    run('"%s" --release --lib "%s" --min-api 26 --output dex %s'
        % (d8, android_jar, ' '.join('"%s"' % f for f in class_files)), cwd=BUILD, env=env)

    # 5) 合并 dex
    dex = BUILD / 'dex' / 'classes.dex'
    if not dex.exists():
        print('d8 没产出 classes.dex')
        return 1
    with zipfile.ZipFile(BUILD / 'base.apk', 'a', zipfile.ZIP_DEFLATED) as z:
        z.write(dex, 'classes.dex')
    print('== 合入 classes.dex（%.1f KB）' % (dex.stat().st_size / 1024))

    # 6) 签名（第一次会生成 keystore）
    if not KEYSTORE.exists():
        print('== 生成签名密钥：', KEYSTORE.name)
        run('"%s" -genkeypair -v -keystore %s -alias %s -keyalg RSA -keysize 2048 -validity 10000 '
            '-storepass %s -keypass %s -dname "CN=Nya-DREAM, OU=Campus, O=YujieSGN, L=Xian, C=CN"'
            % (keytool, KEYSTORE.name, KS_ALIAS, KS_PASS, KS_PASS), cwd=HERE, env=env)

    run('"%s" -f -p 4 base.apk aligned.apk' % zipalign, cwd=BUILD)
    apk_ascii = 'nyadream-shell.apk'
    run('"%s" sign --ks ..\\%s --ks-key-alias %s --ks-pass pass:%s --key-pass pass:%s '
        '--v1-signing-enabled true --v2-signing-enabled true --out %s aligned.apk'
        % (apksigner, KEYSTORE.name, KS_ALIAS, KS_PASS, KS_PASS, apk_ascii), cwd=BUILD, env=env)
    print('== 校验签名')
    run('"%s" verify --print-certs %s' % (apksigner, apk_ascii), cwd=BUILD, env=env)

    out = BUILD / APK_NAME                      # 中文名放最后用 Python 拷（批处理不认中文参数）
    shutil.copyfile(BUILD / apk_ascii, out)
    size = out.stat().st_size
    sha = hashlib.sha256(out.read_bytes()).hexdigest()
    print('\n== 出包：%s（%.0f KB）' % (out, size / 1024))
    print('   sha256:', sha[:32], '…')
    badging = subprocess.run('"%s" dump badging %s' % (aapt2, apk_ascii), shell=True, cwd=str(BUILD),
                             capture_output=True, text=True, encoding='utf-8', errors='replace')
    for line in (badging.stdout or '').splitlines():
        if line.startswith(('package:', 'launchable-activity:', 'uses-permission:')):
            print('   ', line)

    if not args.no_copy:
        dest_dir = PROJ / '交付'
        if dest_dir.exists():
            dest = dest_dir / APK_NAME
            shutil.copyfile(out, dest)
            print('== 已拷到交付：', dest)
    return 0


if __name__ == '__main__':
    sys.exit(main())
