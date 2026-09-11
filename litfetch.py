#!/usr/bin/env python3
"""
litfetch —— 萝卜图书馆(知网镜像)命令行检索/下载/引用工具（个人会员封装）

用法（agent 友好，输出 JSON）：
  litfetch.py search "城市碳排放" [--page 1] [--size 20]
  litfetch.py download "城市碳排放" --fileid FBSF202608014 [--out DIR]
  litfetch.py fetch    "城市碳排放" --top 3 --out DIR      # 检索+批量下载+参考文献一步完成
  litfetch.py verify   参考文献列表.txt                     # 逐条核验引用真实性（平台防假引用）

并发与风控策略（分层）：
  - 检索（search/verify）：不消耗下载额度，可放心并发，MCP 服务器已支持并行调用。
  - 下载：跨进程限速队列——同账号两次下载启动间隔 >= LITFETCH_DL_MIN_INTERVAL（默认2.5秒），
    进程内并发默认2、硬上限3（LITFETCH_MAX_CONCURRENCY），日上限 LITFETCH_DAILY_CAP（默认300）。
  - 真正要多路并行：同目录放 session-*.json（多张会员卡），fetch 自动按卡分道，互不挤占。

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
import difflib
import fcntl
import hashlib
import json
import os
import re
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor
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
DL_MIN_INTERVAL = float(os.environ.get("LITFETCH_DL_MIN_INTERVAL", "2.5"))
DL_DAILY_CAP = int(os.environ.get("LITFETCH_DAILY_CAP", "300"))
MAX_CONCURRENCY = min(3, int(os.environ.get("LITFETCH_MAX_CONCURRENCY", "3")))
DB_TYPE = {"CAPJ": "J", "CJFQ": "J", "CDFD": "D", "CMFD": "D", "IPFD": "C",
           "CIPF": "C", "CCND": "N", "CCJD": "N", "CJFDb": "J"}


def load_sessions():
    """session.json 为主；同目录 session-*.json 视为额外会员卡（下载多路分道用）。"""
    d = SESSION_FILE.parent
    paths = sorted({SESSION_FILE, *d.glob("session-*.json")})
    out = []
    for p in paths:
        try:
            cfg = json.loads(p.read_text())
            if cfg.get("cookies") and cfg.get("entry"):
                out.append((p, cfg))
        except Exception:
            pass
    if not out:
        raise RuntimeError(f"无可用会话：{SESSION_FILE} 缺失或格式错误")
    return out


class DownloadGate:
    """跨进程下载限速：同账号两次下载启动间隔不小于 min_interval，且有日上限。"""

    def __init__(self, name):
        self.path = SESSION_FILE.parent / f".dlgate_{hashlib.md5(name.encode()).hexdigest()[:10]}.json"

    def acquire(self):
        while True:
            with open(self.path, "a+") as f:
                fcntl.flock(f, fcntl.LOCK_EX)
                f.seek(0)
                raw = f.read().strip()
                try:
                    st = json.loads(raw) if raw else {}
                except Exception:
                    st = {}
                today = time.strftime("%Y-%m-%d")
                if st.get("day") != today:
                    st = {"day": today, "count": 0, "last": 0}
                if st["count"] >= DL_DAILY_CAP:
                    raise RuntimeError(
                        f"已达今日下载上限 {DL_DAILY_CAP}（LITFETCH_DAILY_CAP 可调）")
                wait = st.get("last", 0) + DL_MIN_INTERVAL - time.time()
                if wait <= 0:
                    st.update(last=time.time(), count=st["count"] + 1)
                    f.seek(0)
                    f.truncate()
                    f.write(json.dumps(st))
                    return
            time.sleep(min(wait + 0.05, 5.0))


def provenance(row):
    """引用溯源记录：供论文平台审计参考文献真实性。"""
    return {"engine": "BiXia文献(litfetch)", "fileid": row["fileid"], "dbname": row["dbname"],
            "query": row["query"], "retrieved_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
            "mirror": "萝卜图书馆(shutong2/wenxian.shop)"}


class Client:
    def __init__(self, session=None):
        cfg = session or json.loads(SESSION_FILE.read_text())
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


def run_fetch(kw, top, out_dir, concurrency=2):
    """检索+并行下载+参考文献。下载经跨进程限速门，按可用会员卡分道。"""
    sessions = load_sessions()
    n_workers = max(1, min(int(concurrency), MAX_CONCURRENCY, int(top)))
    gates = {}

    def lane_for(i):
        p, cfg = sessions[i % len(sessions)]
        gate = gates.setdefault(p.name, DownloadGate(p.name))
        return Client(cfg), gate

    rows = Client(sessions[0][1]).search(kw, 1, max(top * 2, 20))[:top]
    out_dir = Path(out_dir)
    done, failed = [], []
    lock = threading.Lock()

    def work(i, row):
        try:
            c, gate = lane_for(i)
            gate.acquire()
            res = c.download(row, out_dir)
            with lock:
                done.append(res)
        except Exception as ex:  # noqa: BLE001
            with lock:
                failed.append({"fileid": row["fileid"], "title": row["title"], "error": str(ex)})

    with ThreadPoolExecutor(max_workers=n_workers) as ex:
        for f in [ex.submit(work, i, r) for i, r in enumerate(rows)]:
            f.result()

    cites = [gbt7714(r) for r in rows]
    cite_path = out_dir / "references.md"
    cite_path.parent.mkdir(parents=True, exist_ok=True)
    cite_path.write_text("# 参考文献（GB/T 7714）\n\n" +
                         "\n".join(f"{i+1}. {c_}" for i, c_ in enumerate(cites)) + "\n",
                         encoding="utf-8")
    prov_path = out_dir / "references-provenance.jsonl"
    with open(prov_path, "a", encoding="utf-8") as f:
        for r in rows:
            f.write(json.dumps({**provenance(r), "title": r["title"],
                                "cite": gbt7714(r)}, ensure_ascii=False) + "\n")
    return {"query": kw, "downloaded": done, "failed": failed,
            "references": str(cite_path), "provenance": str(prov_path), "cites": cites}


def run_download(kw, fileid, out_dir, page=1):
    sessions = load_sessions()
    c = Client(sessions[0][1])
    rows = c.search(kw, page, 50)
    row = next((r for r in rows if r["fileid"] == fileid), None)
    if not row:
        raise RuntimeError(f"fileid {fileid} 未在检索结果中找到")
    DownloadGate("default").acquire()
    res = c.download(row, Path(out_dir))
    res["cite"] = gbt7714(row)
    return res


def _title_from_citation(line):
    """从 GB/T 7714 引用串里取题名；取不到就退化为整行。"""
    line = re.sub(r"^\[?\d+\]?\s*", "", line.strip())
    m = re.match(r"^[^.]+\.(\S.*?)\[(?:J|D|C|N|M|G|Z|DB|OL)\]", line)
    if m:
        return m.group(1).strip()
    parts = [p.strip() for p in re.split(r"[.。;;]", line) if len(p.strip()) >= 4]
    return max(parts, key=len) if parts else line.strip()


def verify_citations(lines, client=None):
    """逐条核验参考文献真实性：回查镜像，题名相似度>=0.75 判实。检索不占下载额度。"""
    c = client or Client(load_sessions()[0][1])
    out = []
    for raw in lines:
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        title = _title_from_citation(line)
        try:
            rows = c.search(title, 1, 10)
        except Exception as ex:  # noqa: BLE001
            out.append({"input": line, "verified": False, "reason": f"检索失败: {ex}"})
            continue
        best, ratio = None, 0.0
        for r in rows:
            rr = difflib.SequenceMatcher(None, r["title"], title).ratio()
            if rr > ratio:
                best, ratio = r, rr
        if best and ratio >= 0.75:
            out.append({"input": line, "verified": True, "confidence": round(ratio, 3),
                        "matched_title": best["title"], "authors": best["authors"],
                        "source": best["source"], "date": best["date"],
                        "fileid": best["fileid"], "dbname": best["dbname"],
                        "standard_cite": gbt7714(best), "provenance": provenance(best)})
        else:
            out.append({"input": line, "verified": False, "confidence": round(ratio, 3),
                        "reason": "未找到足够相似的文献（可能为编造或题名抄错）",
                        "closest": best["title"] if best else None})
    return out


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
    p3.add_argument("--concurrency", type=int, default=2,
                    help=f"进程内下载并发，默认2，硬上限{MAX_CONCURRENCY}（跨进程另有间隔限速与日上限）")
    p4 = sub.add_parser("verify", help="核验参考文献列表真实性（每行一条引用；防编造引用）")
    p4.add_argument("file", nargs="?", help="引用列表文件；缺省读stdin")
    p4.add_argument("--out", help="可选：把核验报告另存为 markdown")
    args = ap.parse_args()

    if args.cmd == "search":
        rows = Client(load_sessions()[0][1]).search(args.keyword, args.page, args.size)
        out_json({"count": len(rows), "query": args.keyword,
                  "results": [{**r, "cite": gbt7714(r), "provenance": provenance(r),
                               "abstract_query": r["abstract_query"][:24] + "…"}
                              for r in rows]})
        return
    if args.cmd == "download":
        out_json(run_download(args.keyword, args.fileid, args.out, args.page))
        return
    if args.cmd == "fetch":
        out_json(run_fetch(args.keyword, args.top, args.out, args.concurrency))
        return
    if args.cmd == "verify":
        lines = (Path(args.file).read_text(encoding="utf-8").splitlines()
                 if args.file else sys.stdin.read().splitlines())
        results = verify_citations(lines)
        ok = sum(1 for r in results if r["verified"])
        if args.out:
            p = Path(args.out)
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text("# 参考文献核验报告\n\n" + "\n".join(
                f"- {'✅' if r['verified'] else '❌'} {r['input']}" +
                (f"\n  - 匹配：{r['matched_title']}（{r['source']}，{r['date']}，相似度{r['confidence']}）"
                 if r["verified"] else
                 f"\n  - 原因：{r.get('reason', '')} 最接近：{r.get('closest') or '无'}")
                for r in results) + "\n", encoding="utf-8")
        out_json({"total": len(results), "verified": ok,
                  "unverified": len(results) - ok, "results": results})


if __name__ == "__main__":
    main()
