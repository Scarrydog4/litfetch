#!/usr/bin/env python3
"""
BiXia文献 MCP 服务器（stdio, 零依赖）——把知网检索/下载/引用暴露为 MCP 工具。

多设备部署：把 ~/.paperforge/litfetch/ 整个目录复制到目标机器相同路径，
装好 python3 + cryptography，再在目标机器 ~/.zcode/cli/config.json 的
mcp.servers 里注册本文件即可（README 有完整配置块）。

安全边界与 CLI 相同：top 上限5、串行下载、随机延时，不做高并发。
"""
import json
import sys
import traceback
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import litfetch  # noqa: E402

PROTOCOL = "2024-11-05"
MAX_TOP = 5
MAX_SIZE = 50

TOOLS = [
    {
        "name": "search",
        "description": "检索知网文献（萝卜图书馆会员）。返回题名/作者/期刊/日期/fileid/GB7714引用。",
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
        "description": ("检索+下载前N篇PDF+生成参考文献（GB/T 7714），一步完成。"
                       "top 上限5；串行下载带随机延时，勿频繁调用。"),
        "inputSchema": {
            "type": "object",
            "properties": {
                "keyword": {"type": "string"},
                "top": {"type": "integer", "default": 3, "maximum": MAX_TOP},
                "out": {"type": "string", "description": "输出目录（绝对路径）"},
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
]


def _do_search(args):
    kw = args["keyword"]
    page = min(int(args.get("page", 1)), 50)
    size = min(int(args.get("size", 20)), MAX_SIZE)
    c = litfetch.Client()
    rows = c.search(kw, page, size)
    return {"count": len(rows), "query": kw, "page": page,
            "results": [{**r, "cite": litfetch.gbt7714(r),
                         "abstract_query": r["abstract_query"][:24] + "…"} for r in rows]}


def _do_fetch(args):
    kw = args["keyword"]
    top = min(int(args.get("top", 3)), MAX_TOP)
    out = Path(args["out"]).expanduser()
    c = litfetch.Client()
    rows = c.search(kw, 1, max(top * 2, 20))[:top]
    if not rows:
        return {"query": kw, "downloaded": [], "failed": [], "error": "无检索结果"}
    done, failed = [], []
    for i, row in enumerate(rows):
        try:
            done.append(c.download(row, out))
        except Exception as ex:  # noqa: BLE001
            failed.append({"fileid": row["fileid"], "error": str(ex)})
        if i < len(rows) - 1:
            import random
            import time
            time.sleep(random.uniform(1.5, 3.5))
    cites = [litfetch.gbt7714(r) for r in rows]
    cite_path = out / "references.md"
    cite_path.parent.mkdir(parents=True, exist_ok=True)
    cite_path.write_text("# 参考文献（GB/T 7714）\n\n" +
                         "\n".join(f"{i+1}. {c_}" for i, c_ in enumerate(cites)) + "\n",
                         encoding="utf-8")
    return {"query": kw, "downloaded": done, "failed": failed,
            "references": str(cite_path), "cites": cites}


def _do_download(args):
    c = litfetch.Client()
    rows = c.search(args["keyword"], int(args.get("page", 1)), 50)
    row = next((r for r in rows if r["fileid"] == args["fileid"]), None)
    if not row:
        raise RuntimeError(f"fileid {args['fileid']} 未在检索结果中找到")
    res = c.download(row, Path(args["out"]).expanduser())
    res["cite"] = litfetch.gbt7714(row)
    return res


def call_tool(name, args):
    if name == "search":
        return _do_search(args)
    if name == "fetch":
        return _do_fetch(args)
    if name == "download":
        return _do_download(args)
    raise RuntimeError(f"unknown tool: {name}")


def send(obj):
    sys.stdout.write(json.dumps(obj, ensure_ascii=False) + "\n")
    sys.stdout.flush()


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
        is_request = msg_id is not None
        if method == "initialize":
            pv = (msg.get("params") or {}).get("protocolVersion", PROTOCOL)
            send({"jsonrpc": "2.0", "id": msg_id, "result": {
                "protocolVersion": pv,
                "capabilities": {"tools": {}},
                "serverInfo": {"name": "BiXia文献", "version": "1.0.0"},
            }})
        elif method == "notifications/initialized":
            pass
        elif method == "ping":
            send({"jsonrpc": "2.0", "id": msg_id, "result": {}})
        elif method == "tools/list":
            send({"jsonrpc": "2.0", "id": msg_id, "result": {"tools": TOOLS}})
        elif method == "tools/call":
            params = msg.get("params") or {}
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
        elif is_request:
            send({"jsonrpc": "2.0", "id": msg_id,
                  "error": {"code": -32601, "message": f"method not found: {method}"}})


if __name__ == "__main__":
    main()
