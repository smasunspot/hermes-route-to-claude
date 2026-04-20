# Hermes CC MCP - 完整架构文档

> 本文档记录 Hermes Claude Code MCP 集成的完整架构、依赖关系、安装步骤和风险分析。
> 生成时间：2026-04-20
> 版本：v2026.4.20-safety-fix-v2

---

## 一、项目概述

Hermes CC MCP 是一个将 Claude Code（通过 `@anthropic-ai/claude-agent-sdk`）集成到 Hermes Gateway 的插件系统。

**核心约束（不可改变）**：
- SDK 模式：必须用 `query()`，不改成 CLI
- 多轮会话：支持 follow-up，不改成单次调用
- 实时交互：能看到中间过程（工具调用、thinking）
- 参数透传：plugins / hooks / agents / settings 必须能透传到 SDK

---

## 二、三仓结构

### 2.1 仓库定位

```
┌─────────────────────────────────────────────────────────────────────────┐
│  仓库 1: hermes-claude-mcp (workspace/)                                │
│  路径: /Users/zoe/workspace/                                          │
│  远程: https://github.com/smasunspot/hermes-claude-mcp                 │
│  用途: MCP Server 生产运行目录（独立进程，port 8765）                  │
│  内容: mcp_claude_server.py (1476+ 行)                                │
│  生命周期: Hermes 不更新此目录，独立进程运行                            │
│  版本标签: v2026.4.20-safety-fix                                      │
└─────────────────────────────────────────────────────────────────────────┘

┌─────────────────────────────────────────────────────────────────────────┐
│  仓库 2: hermes-route-to-claude (projects/)                           │
│  路径: /Users/zoe/projects/hermes-route-to-claude/                     │
│  远程: https://github.com/smasunspot/hermes-route-to-claude          │
│  用途: 独立插件仓库（可发布给第三方用户）                              │
│  内容:                                                                │
│    mcp/mcp_claude_server.py    ← MCP Server 源码                      │
│    src/route_to_claude_tool.py ← Hermes 工具插件源码                   │
│    docs/                      ← 技术文档                                │
│    README.md                  ← 用户安装说明                            │
│    ARCHITECTURE.md            ← 本文档                                 │
│  版本标签: v2026.4.20-safety-fix-v2                                   │
└─────────────────────────────────────────────────────────────────────────┘

┌─────────────────────────────────────────────────────────────────────────┐
│  仓库 3: hermes-agent (fork)                                          │
│  路径: /Users/zoe/.hermes/hermes-agent/                                │
│  远程: git@github.com:smasunspot/hermes-agent.git                     │
│        (fork of https://github.com/NousResearch/hermes-agent)           │
│  用途: Hermes 核心框架（本地部署）                                      │
│  当前分支: route-to-claude-code (3c530ff7)                            │
│  状态: Ahead upstream 1 commit, Behind NousResearch/main 179 commits     │
└─────────────────────────────────────────────────────────────────────────┘
```

### 2.2 三仓关系图

```
┌──────────────────────────────────────────────────────────────────┐
│  用户消息 (Feishu / Telegram / API)                               │
└────────────────────────────┬───────────────────────────────────┘
                             ↓
┌──────────────────────────────────────────────────────────────────┐
│  Hermes Gateway (hermes-agent, port 8642)                         │
│  gateway/run.py                                                   │
│  - 接收消息，创建 AIAgent                                        │
│  - AIAgent 调用 route_to_claude_code 工具                         │
└────────────────────────────┬───────────────────────────────────┘
                             ↓
┌──────────────────────────────────────────────────────────────────┐
│  Hermes Tools (hermes-agent/tools/)                              │
│  - route_to_claude_tool.py (我们添加的工具)                      │
│  - mcp_tool.py (MCP 客户端，支持 HTTP Streamable transport)      │
│  - registry.py (工具注册表)                                       │
└────────────────────────────┬───────────────────────────────────┘
                             ↓
┌──────────────────────────────────────────────────────────────────┐
│  MCP Server (workspace/mcp_claude_server.py, port 8765)          │
│  - FastMCP + StreamableHTTP transport                           │
│  - ClaudeSession (管理 Claude Code SDK 会话)                    │
│  - SafetyTimer (120s，防止 Hermes 同步等待死锁)                  │
│  - 独立进程，Hermes 不重启不更新                                │
└────────────────────────────┬───────────────────────────────────┘
                             ↓
┌──────────────────────────────────────────────────────────────────┐
│  Claude Code SDK (@anthropic-ai/claude-agent-sdk)               │
│  - Node.js subprocess (server_script.js)                         │
│  - query() 多轮会话                                             │
│  - 工具调用结果流式返回                                          │
└────────────────────────────┬───────────────────────────────────┘
                             ↓
                    Claude API (MiniMax)

┌──────────────────────────────────────────────────────────────────┐
│  hermes-route-to-claude (插件仓库)                               │
│  - mcp/mcp_claude_server.py → 同步到 workspace/                │
│  - src/route_to_claude_tool.py → 同步到 hermes-agent/tools/    │
│  - docs/ → 用户文档                                             │
└──────────────────────────────────────────────────────────────────┘
```

---

## 三、各组件详解

### 3.1 route_to_claude_tool.py（Hermes 工具插件）

**位置**: `hermes-agent/tools/route_to_claude_tool.py`（已归档到 `src/route_to_claude_tool.py`）

**作用**: Hermes LLM（MiniMax）决定需要 Claude Code 执行时的路由入口。

**调用链**:
```
Hermes LLM → route_to_claude_code tool → registry.dispatch()
                                            ↓
                                   mcp_tool.py MCPServerTask
                                            ↓
                                   HTTP StreamableHTTP (port 8765)
```

**关键代码路径**:
```python
# route_to_claude_tool.py:30
def route_to_claude_code(task: str, task_id: str = None) -> str:
    # 检查 gateway 上下文（有 chat_id 走异步，无则同步直接调用）
    # 同步调用: registry.dispatch("mcp_claude_code_claude_session_start", {...})
    # 异步调用: loop.create_task(_start_claude_session_async(...))
```

**注册方式**: `registry.register()` 在模块加载时自动注册（通过 `discover_builtin_tools()`）

**风险**:
- 位于 `hermes-agent/tools/` 内，Hermes 大版本更新可能重置此目录
- 防护：每次 Hermes 重启后检查文件是否存在

### 3.2 mcp_tool.py（MCP 客户端）

**位置**: `hermes-agent/tools/mcp_tool.py`

**作用**: hermes-agent 的 MCP 客户端实现，连接外部 MCP Server。

**关键类**: `MCPServerTask` — 管理单个 MCP 服务器连接的生命周期

**关键函数**: `_should_register_server()` — 重试逻辑
```python
# 当 session is None 时重试（连接失败后）
def _should_register_server(name: str, cfg: dict) -> bool:
    if name not in _servers:
        return True
    return _servers[name].session is None
```

**传输方式**: HTTP StreamableHTTP（`streamable_http_client`）

**工具发现**: `discover_mcp_tools()` → `_discover_and_register_server()` → `_register_server_tools()`

**重试机制**: `_MAX_INITIAL_CONNECT_RETRIES = 5`，初始重试 5 次，每次 backoff 翻倍（最大 32s）

### 3.3 mcp_claude_server.py（MCP Server）

**位置**: `/Users/zoe/workspace/mcp_claude_server.py`（生产）
         `hermes-route-to-claude/mcp/mcp_claude_server.py`（源码）

**作用**: FastMCP 实现，提供 5 个 MCP 工具给 hermes-agent 调用。

**5 个 MCP 工具**:
| 工具名 | 功能 |
|--------|------|
| `claude_session_start` | 启动新会话，传入 prompt |
| `claude_session_send` | 发送 follow-up 消息 |
| `claude_session_poll` | 获取累积输出 |
| `claude_session_status` | 检查会话状态 |
| `claude_session_stop` | 停止会话 |

**核心类**: `ClaudeSession`（会话管理）

**SafetyTimer**:
- 超时：120 秒（修复 Hermes 同步等待死锁问题）
- 触发条件：120 秒无任何输出时停止 session
- 原因：Hermes 的 `route_to_claude_code` 是同步阻塞等待，不是异步 poll
- 15s 原值会在结果返回前就杀死 session

**关键流程**:
```
start() → 启动 Node.js subprocess
       → 发送 JSON-RPC start 命令
       → 等待 session_started（30s 超时）
       → SafetyTimer 开始计时
       → get_new_output() 返回结果
```

**server_script.js**: Node.js 子进程脚本，位于 `/tmp/hermes_claude_server_{uuid}.js`

### 3.4 gateway/run.py（Hermes 网关入口）

**位置**: `hermes-agent/gateway/run.py`

**作用**: Hermes Gateway 主入口，管理消息路由、Session 创建、工具调度。

**我们添加的内容**（+42 行）:
1. `_thread_local` 路由上下文（chat_id, platform, thread_id）
2. `set_routing_context()` / `get_routing_context()` — 让 `route_to_claude_tool.py` 知道是谁在调用
3. `_handle_reload_mcp_command()` — `/reload-mcp` 命令实现

**为什么需要路由上下文**: 当 Hermes 同时处理多个 chat（Feishu/Telegram 不同用户）时，`route_to_claude_tool` 需要知道当前请求来自哪个 chat，以便正确路由。

---

## 四、哪些会被 Hermes 更新覆盖？哪些不会？

| 组件 | 路径 | 会被 Hermes 更新覆盖？ | 风险等级 | 防护措施 |
|------|------|------------------------|----------|---------|
| `mcp_claude_server.py` | workspace/ | **不会** | 无 | 独立进程，不在 hermes-agent 目录 |
| `route_to_claude_tool.py` | hermes-agent/tools/ | **可能** | 中 | 每次 Hermes 重启后检查，文件已归档到 hermes-route-to-claude |
| `gateway/run.py` | hermes-agent/ | **可能** | 低 | 我们只添加了函数，未修改已有代码 |
| `mcp_tool.py` | hermes-agent/ | **不会** | 无 | Hermes 核心代码，我们未修改 |
| `tools_config.py` | hermes-agent/ | **可能** | 低 | +2 行追加，可能被覆盖 |
| `toolsets.py` | hermes-agent/ | **可能** | 低 | +2 行追加，可能被覆盖 |
| `~/.hermes/config.yaml` | HOME | **不会** | 无 | 用户配置，Hermes 不覆盖 |

**风险说明**:
- "可能" = Hermes 大版本更新时可能重置这些文件
- Hermes 每次 `pip install -U hermes-agent` 不会覆盖 `tools/` 目录
- 但全新安装（删除 ~/.hermes 后重新安装）会丢失

---

## 五、版本与依赖关系

### 5.1 SDK 版本记录

| 日期 | 版本 | 变化 |
|------|------|------|
| 2026-04-20 | 0.2.114 (最新) | 升级自0.2.92 |
| 之前 | 0.2.92 | 初始版本 |

**SDK 安装位置**: `/Users/zoe/workspace/node_modules/@anthropic-ai/claude-agent-sdk`
**备份位置**: `/Users/zoe/workspace/node_modules_backup_20260420/`

### 5.2 关键版本标签

**hermes-claude-mcp (workspace/)**:
```
v2026.4.20-safety-fix = f635630
  fix: SafetyTimer 120s (from 15s)
```

**hermes-route-to-claude (projects/)**:
```
v2026.4.20-safety-fix-v2 = 06e5af0
  fix: SafetyTimer 120s + CallToolResult import
```

**hermes-agent (fork)**:
```
route-to-claude-code = 3c530ff7
  feat: route_to_claude_code tool for Claude Code delegation
```

### 5.3 Python 依赖

```
fire          # route_to_claude_tool.py 需要
anthropic     # hermes-agent API 调用需要
mcp>=1.26.0  # MCP Server/Client
fastmcp       # MCP Server 实现
uvicorn       # HTTP server
```

### 5.4 Node.js

Claude Code SDK 需要 Node.js subprocess 运行 `server_script.js`。

---

## 六、完整安装配置步骤

### 6.1 从零开始安装

```bash
# ========== 第一步：克隆插件仓库 ==========
git clone https://github.com/smasunspot/hermes-route-to-claude.git
cd hermes-route-to-claude

# ========== 第二步：安装 Python 依赖 ==========
pip install fire anthropic

# ========== 第三步：复制工具插件 ==========
# 方式 A: 安装到 hermes-agent
cp src/route_to_claude_tool.py ~/.hermes/hermes-agent/tools/

# 方式 B: 如果 hermes-agent 有版本控制，可以从 git 恢复
# git checkout HEAD -- tools/route_to_claude_tool.py

# ========== 第四步：确保 MCP Server 运行 ==========
# 检查是否已运行
ps aux | grep mcp_claude_server | grep -v grep

# 如果未运行，启动它
nohup python mcp/mcp_claude_server.py --http --port 8765 \
  >> /tmp/mcp_claude_server.log 2>&1 &
echo "MCP Server PID: $!"

# 等待启动
sleep 3

# ========== 第五步：配置 Hermes ==========
# 在 ~/.hermes/config.yaml 中添加或确认 mcp_servers 配置
cat >> ~/.hermes/config.yaml << 'EOF'
mcp_servers:
  claude_code:
    url: http://127.0.0.1:8765/mcp
    enabled: true
    timeout: 300
EOF

# ========== 第六步：重启 Hermes Gateway ==========
# 如果通过 launchd 管理
launchctl bootout gui/$(id -u)/ai.hermes.gateway
sleep 2
launchctl kickstart -p gui/$(id -u)/ai.hermes.gateway

# 或者直接运行
cd ~/.hermes/hermes-agent
python -m hermes_cli.main gateway run --replace
```

### 6.2 验证安装

```bash
# 1. 检查 MCP Server
ps aux | grep mcp_claude_server | grep -v grep
# 期望输出: Python ... mcp_claude_server.py --http --port 8765

# 2. 检查 Hermes 运行
ps aux | grep hermes_cli | grep -v grep
# 期望输出: Python ... hermes_cli.main gateway run

# 3. 检查 MCP 工具注册
tail -30 ~/.hermes/logs/agent.log | grep -i "mcp.*registered"
# 期望输出: MCP server 'claude_code' (HTTP): registered 9 tool(s)...

# 4. 端到端测试
curl -s -X POST http://127.0.0.1:8642/v1/chat/completions \
  -H "Content-Type: application/json" \
  -d '{
    "model": "claude-3-5-sonnet-20241022",
    "messages": [{"role": "user", "content": "用 Claude Code 帮我 echo hello"}],
    "max_tokens": 100
  }'
# 期望输出: {"content": "hello\n"}
```

### 6.3 升级步骤

```bash
# 1. 停止 MCP Server
kill $(ps aux | grep mcp_claude_server | grep -v grep | awk '{print $2}')

# 2. 更新插件仓库
cd /path/to/hermes-route-to-claude
git pull origin main

# 3. 更新 route_to_claude_tool.py
cp src/route_to_claude_tool.py ~/.hermes/hermes-agent/tools/

# 4. 更新 MCP Server
cp mcp/mcp_claude_server.py /path/to/workspace/mcp_claude_server.py

# 5. 重启 MCP Server
nohup python /path/to/workspace/mcp_claude_server.py --http --port 8765 \
  >> /tmp/mcp_claude_server.log 2>&1 &

# 6. 重启 Hermes Gateway
# (根据你的部署方式)
```

---

## 七、调试与故障排查

### 7.1 日志位置

```bash
# MCP Server 日志
tail -f ~/.hermes/logs/mcp-claude-server.log

# Hermes Gateway 日志
tail -f ~/.hermes/logs/agent.log

# MCP Server stdout/stderr (如果用 nohup)
tail -f /tmp/mcp_claude_server.log
```

### 7.2 常见问题

**Q: MCP 工具未注册**
```
现象: agent.log 中没有 "registered 9 tool(s)" 日志
原因: hermes-agent 启动时 discover_mcp_tools() 未被调用
解决: 发送任意消息触发 AIAgent 创建，或重启 hermes-agent
```

**Q: SafetyTimer 提前杀死 session**
```
现象: session 返回 "done, processing" 但 Hermes 拿不到结果
原因: SafetyTimer 15s 太短，Hermes 同步等待超过 15s
解决: mcp_claude_server.py 中 SAFETY_TIMEOUT_SECONDS = 120
```

**Q: Session terminated 错误**
```
现象: hermes-agent 日志显示 "Session terminated"
原因: MCP Server session 已被 SafetyTimer 停止
解决: 延长 SAFETY_TIMEOUT_SECONDS，或等待 Hermes 主动 poll
```

**Q: 多个 hermes-agent 进程**
```
现象: ps aux 显示多个 hermes_cli 进程
原因: launchd 未正确关闭，或手动启动了多个
解决:
  launchctl bootout gui/$(id -u)/ai.hermes.gateway
  killall -9 hermes_cli
  # 只保留一个
```

### 7.3 调试命令

```bash
# 检查 MCP Server 是否响应
curl -s -X POST http://127.0.0.1:8765/mcp \
  -H 'Content-Type: application/json' \
  -H 'Accept: application/json, text/event-stream' \
  -H 'McpSessionId: test-123' \
  -d '{"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":"2025-11-25","capabilities":{},"clientInfo":{"name":"test","version":"1.0"}}}'

# 检查 hermes-agent MCP 状态
curl -s http://127.0.0.1:8642/health

# 检查 route_to_claude_tool 是否存在
ls -la ~/.hermes/hermes-agent/tools/route_to_claude_tool.py

# 检查 MCP Server 进程
lsof -i :8765
```

---

## 八、已知问题与解决方案记录

### 8.1 SafetyTimer 15s 死锁问题（已修复）

**问题**: Hermes 的 `route_to_claude_code` 是同步阻塞等待，SafetyTimer 在 15 秒触发时 session 可能已完成但 Hermes 还没 poll，导致结果丢失。

**根因**: Hermes 同步等 SafetyTimer 触发才返回，不是主动 poll。

**修复**: `SAFETY_TIMEOUT_SECONDS = 120`（在 `mcp_claude_server.py` 中）

### 8.2 hermes-agent 启动时 MCP 工具未发现（已修复）

**问题**: `discover_mcp_tools()` 只在 `model_tools` 被导入时调用，hermes-agent gateway 不直接导入 model_tools。

**修复**: 发送任意 chat completions 请求触发 AIAgent 创建，或发送 `/reload-mcp` 命令。

### 8.3 缺少 fire 模块（已修复）

**问题**: `run_agent.py` 导入 `fire` 但未安装。

**修复**: `pip install fire`

---

## 九、文件对应关系

```
hermes-route-to-claude/           →  部署位置
├── mcp/
│   └── mcp_claude_server.py     →  /Users/zoe/workspace/mcp_claude_server.py
├── src/
│   └── route_to_claude_tool.py  →  ~/.hermes/hermes-agent/tools/route_to_claude_tool.py
├── docs/                        →  用户文档
├── README.md                     →  快速开始
└── ARCHITECTURE.md               →  本文档
```

**同步命令**:
```bash
# MCP Server: projects → workspace
cp hermes-route-to-claude/mcp/mcp_claude_server.py /Users/zoe/workspace/mcp_claude_server.py

# route_to_claude_tool: projects → hermes-agent
cp hermes-route-to-claude/src/route_to_claude_tool.py ~/.hermes/hermes-agent/tools/
```

---

## 十、未来注意事项

1. **Hermes 大版本更新前**：务必先备份 `~/.hermes/hermes-agent/tools/route_to_claude_tool.py`
2. **hermes-route-to-claude 优先更新**：所有代码修改先在插件仓库进行，再同步到生产目录
3. **SafetyTimer 不要改回 15s**：除非 Hermes 改为异步 poll 机制
4. **监控 MCP Server 进程**：建议用 launchd 或 systemd 管理，避免进程消失

---

## 十一、相关链接

- Hermes Agent (fork): https://github.com/smasunspot/hermes-agent
- Hermes CC MCP (plugin repo): https://github.com/smasunspot/hermes-route-to-claude
- Hermes MCP (upstream): https://github.com/smasunspot/hermes-claude-mcp
- Claude Agent SDK: `@anthropic-ai/claude-agent-sdk` v0.2.114
