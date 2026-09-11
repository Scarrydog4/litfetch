#!/usr/bin/env python3
"""
litfetch —— 萝卜图书馆(知网镜像)命令行检索/下载/引用工具（个人会员封装）

用法（agent 友好，输出 JSON）：
  litfetch.py search "城市碳排放" [--page 1] [--size 20] [--json]
  litfetch.py download "城市碳排放" --fileid FBSF202608014 [--out DIR]
  litfetch.py fetch    "城市碳排放" --top 3 --out DIR      # 检索+批量下载+参考文献一步完成

链路（全部离线复刻，无需浏览器）：
  会员Cookie → 入口页(l999.php)签发JWT → apiXX.wenxian.shop/token换会话 → kns8检索接口
  → AES-128-ECB(key=Q5vGEmoCW59MW4Qc)签名abstract参数 → apiXX下载页 → docdown.cnki.net真文件

安全边界（重要）：
  - 会员按卡计费，共享镜像对批量下载有风控：默认串行+随机延时，并发硬上限3。
  - 工具会跳过镜像站180秒广告倒计时，请勿因此提高频率；下载量保持人手同级。
  - 会话文件含个人凭据，只在 ~/.paperforge/litfetch/ 内使用，不入仓。
"""
import argparse
import base64
import json
import random
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from http.cookiejar import Cookie, CookieJar
from pathlib import Path

UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 "
      "(KHTML, like Gecko) Version/17.4 Safari/605.1.15")
AES_KEY = b"Q5vGEmoCW59MW4Qc"


# ---- 纯标准库 AES-128-ECB（仅加密），免去第三方依赖 ----
def _gf_mul(a, b):
    r = 0
    while b:
        if b & 1:
            r ^= a
        b >>= 1
        a <<= 1
        if a & 0x100:
            a ^= 0x11B
    return r


def _make_sbox():
    exp, log, v = [0] * 255, [0] * 256, 1
    for i in range(255):
        exp[i], log[v] = v, i
        v = _gf_mul(v, 3)
    box = []
    for a in range(256):
        b = 0 if a == 0 else exp[(255 - log[a]) % 255]
        c = b
        for _ in range(4):
            b = ((b << 1) | (b >> 7)) & 0xFF
            c ^= b
        box.append(c ^ 0x63)
    return box


_SBOX = _make_sbox()
_RCON = [0x01, 0x02, 0x04, 0x08, 0x10, 0x20, 0x40, 0x80, 0x1B, 0x36]


def _xtime(a):
    return ((a << 1) ^ 0x1B) & 0xFF if a & 0x80 else (a << 1)


def _expand_key(key16):
    w = [list(key16[i * 4:(i + 1) * 4]) for i in range(4)]
    for i in range(4, 44):
        t = list(w[i - 1])
        if i % 4 == 0:
            t = [_SBOX[b] for b in (t[1:] + t[:1])]
            t[0] ^= _RCON[i // 4 - 1]
        w.append([w[i - 4][j] ^ t[j] for j in range(4)])
    return [sum((w[r * 4 + c] for c in range(4)), []) for r in range(11)]


def _encrypt_block(block, rks):
    s = [block[i] ^ rks[0][i] for i in range(16)]
    for rnd in range(1, 10):
        t = [_SBOX[x] for x in s]
        n = [t[((c + r) % 4) * 4 + r] for c in range(4) for r in range(4)]
        m = []
        for c in range(4):
            a = n[c * 4:c * 4 + 4]
            m.append(_xtime(a[0]) ^ _xtime(a[1]) ^ a[1] ^ a[2] ^ a[3])
            m.append(a[0] ^ _xtime(a[1]) ^ _xtime(a[2]) ^ a[2] ^ a[3])
            m.append(a[0] ^ a[1] ^ _xtime(a[2]) ^ _xtime(a[3]) ^ a[3])
            m.append(_xtime(a[0]) ^ a[0] ^ a[1] ^ a[2] ^ _xtime(a[3]))
        s = [m[i] ^ rks[rnd][i] for i in range(16)]
    t = [_SBOX[x] for x in s]
    n = [t[((c + r) % 4) * 4 + r] for c in range(4) for r in range(4)]
    return bytes(n[i] ^ rks[10][i] for i in range(16))


def _aes_ecb_encrypt(key, data):
    rks = _expand_key(key)
    out = bytearray()
    for off in range(0, len(data), 16):
        out += _encrypt_block(data[off:off + 16], rks)
    return bytes(out)
SESSION_FILE = Path(__file__).resolve().parent / "session.json"  # 跟随脚本所在目录，便于整目录搬运
MAX_CONCURRENCY = 3
DB_TYPE = {"CAPJ": "J", "CJFQ": "J", "CDFD": "D", "CMFD": "D", "IPFD": "C",
           "CIPF": "C", "CCND": "N", "CCJD": "N", "CJFDb": "J"}


class Client:
    def __init__(self):
        cfg = json.loads(SESSION_FILE.read_text())
        self.entry = cfg["entry"]
        self.member = cfg["cookies"]
        self.jar = CookieJar()
        self.opener = urllib.request.build_opener(
            urllib.request.HTTPCookieProcessor(self.jar))
        self.nr = urllib.request.build_opener(
            _NoRedirect(), urllib.request.HTTPCookieProcessor(self.jar))
        self.landing = None
        self.dl_base = None

    def _req(self, url, referer=None, data=None, ct=None, opener=None):
        h = {"User-Agent": UA}
        if referer:
            h["Referer"] = referer
        if ct:
            h["Content-Type"] = ct
        return (opener or self.opener).open(
            urllib.request.Request(url, data=data, headers=h), timeout=90)

    def auth(self):
        """入口→JWT→镜像会话。apiXX 下载域名与 kns8 落地地址均从响应动态取。"""
        for k, v in self.member.items():
            self.jar.set_cookie(Cookie(0, k, v, None, False, "xy.shutong2.com",
                                       True, False, "/", True, False, None, False,
                                       None, None, {}, False))
        html = self._req(self.entry, referer="https://xy.shutong2.com/zhongwenku/"
                         ).read().decode("utf-8", "replace")
        m = re.search(r"location\.href='([^']+)'", html)
        if not m:
            raise RuntimeError("入口页未签发跳转（会员Cookie可能过期）: " + html[:120])
        token_url = m.group(1)
        self.dl_base = "https://" + urllib.parse.urlsplit(token_url).netloc
        try:
            self.nr.open(urllib.request.Request(
                token_url, headers={"User-Agent": UA,
                                    "Referer": self.entry}), timeout=90)
            raise RuntimeError("入口未重定向到检索页")
        except urllib.error.HTTPError as e:
            self.landing = e.headers.get("Location")
        if not self.landing:
            raise RuntimeError("未取得检索页落地地址")
        self._req(self.landing, referer=token_url).read()

    def search(self, kw, page=1, size=20):
        if not self.landing:
            self.auth()
        # 镜像只认10/20/50档位的pageSize，其他值（如5）返回空结果
        size = min(max(size, 10), 50)
        qj = {"Platform": "", "Resource": "CROSSDB", "Classid": "WD0FTY92",
              "Products": "",
              "QNode": {"QGroup": [{"Key": "Subject", "Title": "", "Logic": 0,
                                    "Items": [{"Field": "SU", "Value": kw,
                                               "Operator": "TOPRANK", "Logic": 0,
                                               "Title": "主题"}],
                                    "ChildItems": []}]},
              "ExScope": 1, "SearchType": 2, "Rlang": "CHINESE",
              "KuaKuCode": ("YSTT4HG0,LSTPFY1C,JUP3MUPD,MPMFIG1A,EMRPGLPA,"
                            "WQ0UVIAA,BLZOG7CK,PWFIRAGL,NN3FJMUV,NLBO1Z6R"),
              "Expands": {}, "SearchFrom": 1}
        body = urllib.parse.urlencode({
            "boolSearch": "true",
            "QueryJson": json.dumps(qj, ensure_ascii=False, separators=(",", ":")),
            "pageNum": str(page), "pageSize": str(size), "dstyle": "listmode",
            "boolSortSearch": "false",
            "productStr": ("YSTT4HG0,LSTPFY1C,RMJLXHZ3,JQIRZIYA,JUP3MUPD,"
                           "1UR4K4HZ,BPBAFJ5S,R79MZMCB,MPMFIG1A,EMRPGLPA,"
                           "J708GVCE,ML4DRIDX,WQ0UVIAA,NB3BWEHK,XVLO76FD,"
                           "HR1YT1Z9,BLZOG7CK,PWFIRAGL,NN3FJMUV,NLBO1Z6R,"),
            "aside": "主题：" + kw, "searchFrom": "资源范围：总库",
            "subject": "", "language": "", "uniplatform": "",
            "CurPage": str(page),
        }, quote_via=urllib.parse.quote).encode()
        origin = urllib.parse.urlsplit(self.landing)
        base = f"{origin.scheme}://{origin.netloc}"
        grid = self._req(base + "/kns8s/brief/grid",
                         referer=self.landing, data=body,
                         ct="application/x-www-form-urlencoded; charset=UTF-8"
                         ).read().decode("utf-8", "replace")
        return _parse_rows(grid, kw, page)

    def sign_download_url(self, row):
        if not self.dl_base:
            self.auth()
        plaintext = row["abstract_query"]
        body = plaintext.encode()
        pad = 16 - len(body) % 16
        v = base64.b64encode(_aes_ecb_encrypt(AES_KEY, body + bytes([pad]) * pad)).decode()
        ts = str(int(time.time() * 1000))
        t = ts[:-1] + str(sum(int(d) for d in ts[-4:-1]) % 10)
        pd = row["date"].replace(" ", "%20")
        return (f"{self.dl_base}/v1/api/download?dflag=pdfdown&v={v}"
                f"&fileid={row['fileid']}&dataDbname={row['dbname']}"
                f"&pd={pd}&t={t}")

    def download(self, row, out_dir: Path):
        if not self.landing:
            self.auth()
        out_dir.mkdir(parents=True, exist_ok=True)
        signed = self.sign_download_url(row)
        page = self._req(signed, referer=self.landing).read().decode("utf-8", "replace")
        if "尚未授权" in page:
            raise RuntimeError("镜像会话失效，请重试（会重新走入口）；仍失败则更新session.json")
        m = re.search(r'href="(https://docdown\.cnki\.net/[^"]+)"', page)
        if not m:
            raise RuntimeError("下载中转页未包含真实文件地址: " +
                               re.sub(r"<[^>]+>", " ", page)[:150])
        docdown = m.group(1).replace("&amp;", "&")
        # 注意：docdown 必须不带 Referer，带了会返回HTML错误页
        r = self._req(docdown)
        magic = r.read(5)
        if magic != b"%PDF-":
            raise RuntimeError(f"非PDF响应: {r.status} {r.headers.get('Content-Type')} {magic!r}")
        data = magic + r.read()
        fname = re.sub(r"[^\w\u4e00-\u9fff-]", "_", row["title"])[:60]
        path = out_dir / f"{row['fileid']}_{fname}.pdf"
        path.write_bytes(data)
        return {"path": str(path), "bytes": len(data), "fileid": row["fileid"]}


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *a, **k):
        return None


def _strip(s):
    return re.sub(r"\s+", " ", re.sub("<[^>]+>", "", s)).strip()


def _parse_rows(grid, kw, page):
    rows = []
    for tr in re.findall(r"<tr>.*?</tr>", grid, re.S):
        nm = re.search(r'<td class="name">\s*<a[^>]*href="([^"]+)"', tr)
        if not nm:
            continue
        href = nm.group(1).replace("&amp;", "&")
        if "abstract?v=" not in href:
            continue
        fin = re.search(r'data-filename="([^"]*)"', tr)
        dbn = re.search(r'data-dbname="([^"]*)"', tr)
        dt = re.search(r'<td class="date">\s*(.*?)\s*</td>', tr, re.S)
        au = re.search(r"<td class=.author.>(.*?)</td>", tr, re.S)
        src = re.search(r'<td class="source">(.*?)</td>', tr, re.S)
        ttl = re.search(r'<td class="name">\s*<a[^>]*>(.*?)</a>', tr, re.S)
        if not (fin and dbn and dt and ttl):
            continue
        authors = [a for a in (_strip(x) for x in re.findall(
            r"<a[^>]*>(.*?)</a>", au.group(1), re.S)) if a] if au else []
        rows.append({
            "query": kw, "page": page,
            "title": _strip(ttl.group(1)),
            "authors": authors,
            "source": _strip(src.group(1)) if src else "",
            "date": _strip(dt.group(1)),
            "fileid": fin.group(1), "dbname": dbn.group(1),
            "abstract_query": href.split("abstract?v=", 1)[1],
        })
    return rows


def gbt7714(row):
    tag = DB_TYPE.get(re.sub(r"[0-9].*", "", row["dbname"] or ""), "Z")
    authors = row["authors"] or ["佚名"]
    astr = "，".join(authors[:3]) + ("，等" if len(authors) > 3 else "")
    year = (row["date"] or "")[:4] or "n.d."
    src = row["source"]
    if tag == "J":
        return f"{astr}. {row['title']}[J]. {src}, {year}."
    if tag == "D":
        return f"{astr}. {row['title']}[D]. {src}, {year}."
    return f"{astr}. {row['title']}[{tag}]. {src}, {year}."


def out_json(obj):
    print(json.dumps(obj, ensure_ascii=False, indent=1))


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    p1 = sub.add_parser("search", help="检索，输出JSON结果含引用条目")
    p1.add_argument("keyword")
    p1.add_argument("--page", type=int, default=1)
    p1.add_argument("--size", type=int, default=20)
    p2 = sub.add_parser("download", help="按fileid下载单篇（内部会重新检索定位）")
    p2.add_argument("keyword")
    p2.add_argument("--fileid", required=True)
    p2.add_argument("--out", default=".")
    p2.add_argument("--page", type=int, default=1, help="目标所在页码")
    p3 = sub.add_parser("fetch", help="检索+批量下载+参考文献一步完成")
    p3.add_argument("keyword")
    p3.add_argument("--top", type=int, default=3)
    p3.add_argument("--out", required=True)
    p3.add_argument("--concurrency", type=int, default=1,
                    help=f"并发下载数，硬上限{MAX_CONCURRENCY}；建议保持1")
    args = ap.parse_args()

    c = Client()
    if args.cmd == "search":
        rows = c.search(args.keyword, args.page, args.size)
        out_json({"count": len(rows), "query": args.keyword,
                  "results": [{**r, "cite": gbt7714(r),
                               "abstract_query": r["abstract_query"][:24] + "…"}
                              for r in rows]})
        return
    if args.cmd == "download":
        rows = c.search(args.keyword, args.page, 50)
        row = next((r for r in rows if r["fileid"] == args.fileid), None)
        if not row:
            sys.exit(f"fileid {args.fileid} 未在检索结果中找到")
        res = c.download(row, Path(args.out))
        res["cite"] = gbt7714(row)
        out_json(res)
        return
    if args.cmd == "fetch":
        conc = max(1, min(args.concurrency, MAX_CONCURRENCY))
        rows = c.search(args.keyword, 1, max(args.top * 2, 20))[: args.top]
        out_dir = Path(args.out)
        done, failed = [], []
        for i, row in enumerate(rows):
            try:
                done.append(c.download(row, out_dir))
            except Exception as ex:
                failed.append({"fileid": row["fileid"], "error": str(ex)})
            if conc == 1 and i < len(rows) - 1:
                time.sleep(random.uniform(1.5, 3.5))  # 人手级节奏，防风控
        cites = [gbt7714(r) for r in rows]
        cite_path = out_dir / "references.md"
        cite_path.parent.mkdir(parents=True, exist_ok=True)
        cite_path.write_text("# 参考文献（GB/T 7714）\n\n" +
                             "\n".join(f"{i+1}. {c_}" for i, c_ in enumerate(cites)) + "\n",
                            encoding="utf-8")
        out_json({"query": args.keyword, "downloaded": done, "failed": failed,
                  "references": str(cite_path), "cites": cites})


if __name__ == "__main__":
    main()
