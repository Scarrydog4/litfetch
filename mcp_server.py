#!/usr/bin/env python3
"""
BiXia文献 MCP 服务器（stdio, 零依赖）——把知网检索/下载/引用/核验暴露为 MCP 工具。

多设备部署：把本目录（litfetch.py/mcp_server.py/session.json）复制到目标机器，
运行 setup.sh 或按 README 注册即可。

并发模型：
  - 请求线程化：search/verify 可并行调用（检索不占下载额度）。
  - 下载走 litfetch 的跨进程限速门（同账号启动间隔>=LITFETCH_DL_MIN_INTERVAL，
    并发<=3，日上限 LITFETCH_DAILY_CAP）；多张会员卡（session-*.json）自动分道。
"""
import json
import sys
import threading
import traceback
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import litfetch  # noqa: E402

PROTOCOL = "2024-11-05"
MAX_TOP = 5
MAX_SIZE = 50

TOOLS = [
    {
        "name": "search",
        "description": "检索知网文献（萝卜图书馆会员）。返回题名/作者/期刊/日期/fileid/GB7714引用/溯源记录。可并行调用。",
        "inputSchema": {
            "type": "object",
            "properties": {
                "keyword": {"type": "string", "description": "检索词，如 '城市碳排放'"},
                "page": {"type": "integer", "default": 1},
                "size": {"type": "integer", "default": 20, "maximum": MAX_SIZE},
            },
            "required": ["keyword"],
        },
    },
    {
        "name": "fetch",
        "description": ("检索+下载前N篇PDF+生成参考文献（GB/T 7714，含溯源jsonl）。"
                       "top 上限5；下载经跨进程限速（间隔/并发/日上限）防风控，勿绕过。"),
        "inputSchema": {
            "type": "object",
            "properties": {
                "keyword": {"type": "string"},
                "top": {"type": "integer", "default": 3, "maximum": MAX_TOP},
                "out": {"type": "string", "description": "输出目录（绝对路径）"},
                "concurrency": {"type": "integer", "default": 2,
                                "maximum": 3, "description": "下载并发，默认2，上限3"},
            },
            "required": ["keyword", "out"],
        },
    },
    {
        "name": "download",
        "description": "按 fileid 下载单篇 PDF（内部会重新检索定位该篇）。",
        "inputSchema": {
            "type": "object",
            "properties": {
                "keyword": {"type": "string", "description": "该篇所在检索词"},
                "fileid": {"type": "string"},
                "out": {"type": "string", "description": "输出目录（绝对路径）"},
                "page": {"type": "integer", "default": 1},
            },
            "required": ["keyword", "fileid", "out"],
        },
    },
    {
        "name": "search_en",
        "description": ("英文文献检索：Crossref（正式期刊，带DOI，免费公开API）或 arXiv（预印本）。"
                       "返回题名/作者/期刊/年份/DOI/GB7714引用/溯源。与中文通道互补。"),
        "inputSchema": {
            "type": "object",
            "properties": {
                "query": {"type": "string"},
                "size": {"type": "integer", "default": 10, "maximum": 30},
                "source": {"type": "string", "enum": ["crossref", "arxiv"], "default": "crossref"},
            },
            "required": ["query"],
        },
    },
    {
        "name": "verify",
        "description": ("核验参考文献真实性：逐条回查知网镜像，题名相似度>=0.75 判实。"
                       "用于论文定稿前防编造引用；只检索不下载，可放心批量。"),
        "inputSchema": {
            "type": "object",
            "properties": {
                "citations": {"type": "array", "items": {"type": "string"},
                              "description": "引用条目列表（GB/T 7714 或裸题名均可）"},
            },
            "required": ["citations"],
        },
    },
]


def _do_search(args):
    kw = args["keyword"]
    page = min(int(args.get("page", 1)), 50)
    size = min(int(args.get("size", 20)), MAX_SIZE)
    c = litfetch.Client(litfetch.load_sessions()[0][1])
    rows = c.search(kw, page, size)
    return {"count": len(rows), "query": kw, "page": page,
            "results": [{**r, "cite": litfetch.gbt7714(r),
                         "provenance": litfetch.provenance(r),
                         "abstract_query": r["abstract_query"][:24] + "…"} for r in rows]}


def _do_fetch(args):
    return litfetch.run_fetch(args["keyword"], min(int(args.get("top", 3)), MAX_TOP),
                              args["out"], min(int(args.get("concurrency", 2)), 3))


def _do_download(args):
    return litfetch.run_download(args["keyword"], args["fileid"], args["out"],
                                 int(args.get("page", 1)))


def _do_search_en(args):
    kw = args["query"]
    rows = litfetch.search_en(kw, min(int(args.get("size", 10)), 30),
                              args.get("source", "crossref"))
    return {"count": len(rows), "query": kw, "source": args.get("source", "crossref"),
            "results": rows}


def _do_verify(args):
    results = litfetch.verify_citations(args["citations"])
    ok = sum(1 for r in results if r["verified"])
    return {"total": len(results), "verified": ok,
            "unverified": len(results) - ok, "results": results}


def call_tool(name, args):
    if name == "search":
        return _do_search(args)
    if name == "fetch":
        return _do_fetch(args)
    if name == "download":
        return _do_download(args)
    if name == "search_en":
        return _do_search_en(args)
    if name == "verify":
        return _do_verify(args)
    raise RuntimeError(f"unknown tool: {name}")


_wlock = threading.Lock()
_pool = ThreadPoolExecutor(max_workers=6)


def send(obj):
    with _wlock:
        sys.stdout.write(json.dumps(obj, ensure_ascii=False) + "\n")
        sys.stdout.flush()


def handle_call(msg):
    params = msg.get("params") or {}
    msg_id = msg.get("id")
    try:
        result = call_tool(params.get("name", ""), params.get("arguments") or {})
        send({"jsonrpc": "2.0", "id": msg_id, "result": {
            "content": [{"type": "text", "text": json.dumps(result, ensure_ascii=False, indent=1)}],
            "isError": False}})
    except Exception as ex:  # noqa: BLE001
        traceback.print_exc(file=sys.stderr)
        send({"jsonrpc": "2.0", "id": msg_id, "result": {
            "content": [{"type": "text", "text": f"BiXia文献 error: {ex}"}],
            "isError": True}})


def main():
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            msg = json.loads(line)
        except json.JSONDecodeError:
            continue
        method = msg.get("method", "")
        msg_id = msg.get("id")
        if method == "initialize":
            pv = (msg.get("params") or {}).get("protocolVersion", PROTOCOL)
            send({"jsonrpc": "2.0", "id": msg_id, "result": {
                "protocolVersion": pv,
                "capabilities": {"tools": {}},
                "serverInfo": {"name": "BiXia文献", "version": "1.1.0"},
            }})
        elif method == "notifications/initialized":
            pass
        elif method == "ping":
            send({"jsonrpc": "2.0", "id": msg_id, "result": {}})
        elif method == "tools/list":
            send({"jsonrpc": "2.0", "id": msg_id, "result": {"tools": TOOLS}})
        elif method == "tools/call":
            _pool.submit(handle_call, msg)
        elif msg_id is not None:
            send({"jsonrpc": "2.0", "id": msg_id,
                  "error": {"code": -32601, "message": f"method not found: {method}"}})


if __name__ == "__main__":
    main()
