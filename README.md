# BiXia文献 —— 知网文献检索/下载/引用（MCP + 命令行，仓库名 litfetch）

零第三方依赖（纯 python3 标准库），支持任何 MCP 客户端（ZCode / Claude Desktop /
Claude Code / Cursor / Codex 及其他），也支持纯命令行使用。

## 快速开始（拿到包的机器上）

方式一，一行命令（GitHub 仓库直装，代码仓库不含任何凭据）：

```bash
curl -fsSL https://cdn.jsdelivr.net/gh/Scarrydog4/litfetch@main/install.sh | bash
# 国内若拉不动，换备用源：
LITFETCH_BASE=https://raw.githubusercontent.com/Scarrydog4/litfetch/main \
  bash -c 'curl -fsSL https://raw.githubusercontent.com/Scarrydog4/litfetch/main/install.sh | bash'
```

方式二，zip 包解压后：

```bash
bash setup.sh --test     # 装到 ~/.litfetch，自动注册检测到的客户端，并跑一次真实检索自检
```

两种方式装完后，都需要一个 session.json（会员凭据，仓库/安装源里没有）：
zip 包里自带；一行命令方式由发包人私下发你，放进 `~/.litfetch/` 即可（安装器会自动弹出该文件夹）。

git 用户也可以：`git clone https://github.com/Scarrydog4/litfetch.git && cd litfetch && cp session.json . 2>/dev/null; bash setup.sh`

只装部分客户端：`bash setup.sh --only zcode,claude-desktop,cursor,codex,claude-code`
（不改配置只装文件：都不指定时默认全检测；注册前会自动备份原配置为 .bak-litfetch）

MCP 注册名为 BiXia文献（工具形如 mcp__BiXia文献__search）。装完重启对应客户端即可看到工具：**search**（检索）/ **fetch**（检索+下载+参考文献）/
**download**（单篇下载）。命令行直接用：`~/.litfetch/litfetch.py search "关键词"`。若个别客户端对中文工具名支持不佳（表现为工具不出或调用报错），把 setup.sh 顶部 NAME 改成 bixia-wenxian 重装即可。

## 这个包"即用"的原理与代价

包里的 session.json 带着会员登录凭据——**谁的凭据，下载就记在谁的账上**。
发给谁用，就是和谁共享这张会员卡。请注意：

- 共享镜像有风控，多台机器、多个 IP 同时高频下载，最容易触发封卡；
- 工具已在代码层限制：单次 fetch 最多 5 篇、串行下载、篇间随机延时。请勿改造解除；
- 建议只在少数熟人设备间共享，下载量保持单人手速量级；
- 若对方有自己的会员卡，把他们的 session.json 换进包里即可，各自走各自额度。

会员凭据过期时：浏览器登录 wenxian.shop → 取 xy.shutong2.com 的 yiffaml* 五个
Cookie 更新 session.json（详细步骤见下"会话续期"）。

## 各客户端手工注册（setup.sh 已代劳，备查）

ZCode `~/.zcode/cli/config.json`：
```json
{"mcp": {"servers": {"litfetch": {"type": "stdio", "command": "/绝对路径/python3",
  "args": ["~/.litfetch/mcp_server.py 的绝对路径"], "timeoutMs": 300000, "enabled": true}}}}
```
Claude Desktop `~/Library/Application Support/Claude/claude_desktop_config.json` 与
Cursor `~/.cursor/mcp.json`：顶层 `mcpServers.litfetch = {"command": ..., "args": [...]}`
（不带 type 字段）。Claude Code：`claude mcp add --scope user litfetch -- python3 ~/.litfetch/mcp_server.py`。
Codex `~/.codex/config.toml`：
```toml
[mcp_servers.litfetch]
command = "/绝对路径/python3"
args = ["/绝对路径/mcp_server.py"]
```
注意：各配置不展开 `~` 和模板变量，一律写绝对路径；ZCode 的 schema 严格，多余键会被丢弃。

## 会话续期（session.json）

浏览器登录 wenxian.shop（会跳到 xy.shutong2.com）→ F12 → Application → Cookies →
xy.shutong2.com，把这五个值更新进 session.json：yiffamlusername / yiffamluserid /
yiffamlgroupid / yiffamlrnd / yiffamlauth。更新后无需重启，下次调用自动生效。
api88 短期会话（1 小时 JWT）工具每次自动重签，无需理会。

## 并发与真实引用（论文平台接入指南）

**分层并发策略**（风控只盯下载，检索不占额度）：

| 操作 | 并发 | 限制 |
|---|---|---|
| search / verify | 随意并发（MCP 已线程化，agent 可同时发多个） | 无 |
| 下载（fetch/download） | 进程内默认2、硬上限3 | 跨进程限速：同账号两次启动间隔≥2.5s（`LITFETCH_DL_MIN_INTERVAL`），日上限300（`LITFETCH_DAILY_CAP`） |
| 真要多路并行 | 加会员卡即可 | 同目录放 `session-*.json`（多张卡），fetch 自动按卡分道 |

**真实引用三件套**（防编造引用，供论文引擎调用）：

1. 生产时用 search 收集引用——每条结果自带 `provenance` 溯源记录（fileid/库名/检索词/取回时间）；
2. fetch 落盘 `references.md`（人读）+ `references-provenance.jsonl`（机器审计，可进评审包）；
3. **定稿前用 verify 逐条回查**：把参考文献列表（每行一条，GB/T 7714 或裸题名）交给它，
   题名相似度≥0.75 判实、否则报编造嫌疑，输出匹配到的真实元数据与修正后的标准引用。

注意：verify 输入必须是**具体引用条目或文献题名**，拿主题词去查会判不实（主题词不是文献）。

## 命令行用法

```bash
~/.litfetch/litfetch.py search "城市碳排放" [--page 1] [--size 20]
~/.litfetch/litfetch.py fetch "都市圈 碳排放" --top 3 --out 输出目录/ [--concurrency 2]
~/.litfetch/litfetch.py download "城市碳排放" --fileid FBSF202608014 --out 目录/
~/.litfetch/litfetch.py verify 参考文献列表.txt --out 核验报告.md   # 防编造引用
```

## 排障

- `入口页未签发跳转` / `尚未授权` → 会员 Cookie 过期，按上面"会话续期"处理。
- `非PDF响应` → 该篇镜像暂不可得（常见于网络首发），跳过即可。
- 检索结果为空但关键词明显热门 → 检查关键词里是否有奇怪字符；分页大小只支持 10/20/50（工具已自动钳制）。
- 网络首发(CAPJ)类文献下载偶发为空，属镜像侧覆盖问题。

## 技术链路（仅供排障参考）

会员Cookie → 入口 l999.php（须带 Referer）→ 签发 JWT → apiXX.wenxian.shop/token 换
会话 → kns8 检索接口（POST /kns8s/brief/grid）→ 结果行内 abstract 参数经
AES-128-ECB（key=Q5vGEmoCW59MW4Qc，Pkcs7，纯标准库实现）签名 → apiXX 下载中转页 →
docdown.cnki.net 直链（**不带 Referer** 才返回 PDF）。加密逻辑出自检索页 /c_data.js
（jsjiami v5 混淆）；若镜像换 key，重新反混淆取 `encrypt` 内密钥串。
