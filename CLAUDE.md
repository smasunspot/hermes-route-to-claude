# Hermes CC MCP - Claude Code 集成

## 项目目标

在 Hermes 中实现 SDK 模式的 Claude Code 集成，支持多轮会话、插件加载、Hooks 和记忆系统。

## 核心约束（不可变）

- **SDK 模式**：必须用 `@anthropic-ai/claude-agent-sdk` query()，不改成 CLI
- **多轮会话**：支持 follow-up，不改成单次调用
- **实时交互**：能看到中间过程（工具调用、thinking）
- **参数透传**：plugins / hooks / agents / settings 必须能透传到 SDK

## SDK 版本记录

| 日期 | 版本 | 变化 |
|------|------|------|
| 2026-04-20 | 0.2.114 (最新) | 升级自0.2.92，API兼容 |
| 之前 | 0.2.92 | 初始版本 |

**SDK安装位置**：`/Users/zoe/workspace/node_modules/@anthropic-ai/claude-agent-sdk`
**备份位置**：`/Users/zoe/workspace/node_modules_backup_20260420/`

## 项目结构

```
hermes-mcp/
├── CLAUDE.md           # 本文件，项目规范 + 开发准则
├── AGENTS.md           # Agent架构说明
├── README.md           # 项目说明
├── SPEC.md             # 项目宪章
├── CHANGELOG.md       # 版本记录
├── WORKLOG.md         # 工作日志
├── SPRINT.md          # Sprint测试计划
├── src/
│   └── mcp_claude_server.py   # 与 workspace/ 同步（1410L，含 HTTP mode）
├── docs/              # 技术文档
├── plan/              # 项目计划
└── reports/           # 审查报告
```

**注意**：`hermes-mcp/src/mcp_claude_server.py` 和 `/Users/zoe/workspace/mcp_claude_server.py` 已同步（1410L，含 HTTP mode）。开发时改这里，生产环境用 workspace/。

## 开发规范

### 不允许做的事

- ❌ 不改成 CLI 模式
- ❌ 不改单次会话
- ❌ 不大重构 SessionManager
- ❌ 不改 poll 机制
- ❌ 不改 safety timer
- ❌ 不混多个 Phase 在一个 commit

### 每个 Phase 独立验证

1. Phase 0：NODE_SERVER 模板化，参数能透传
2. Phase 1：plugins 加载，Superpowers 能用
3. Phase 2：hooks 支持
4. Phase 3：agents 配置
5. Phase 4：settings 透传

### 验收标准

- 现有功能不 regression
- 新功能有明确测试
- 错误信息明确

---

## Karpathy 开发准则（所有任务必须遵守）

### 1. Think Before Coding

**不要假设。不要隐藏困惑。主动呈现权衡。**

- 不确定时先问清楚，不要猜
- 多个可行方案时，列出对比，不要静默选一个
- 发现更简单的路径时，主动提出
- 遇到不清楚的地方，停下来问

### 2. Simplicity First

**最小代码解决问题。不做 speculative 的设计。**

- 不做需求之外的功能
- 不为单次使用的代码写抽象
- 不加没被要求的"灵活性"
- 如果 200 行可以写成 50 行，重写

自检：**高级工程师会觉得这过度复杂吗？** 如果是，简化。

### 3. Surgical Changes

**只改必须改的。只清理自己造成的垃圾。**

- 不"顺便"改善相邻代码、注释、格式
- 不重构没坏的东西
- 匹配已有风格，即使你会有不同写法
- 发现无关的死代码，提出来但不删

自检：**每行改动的代码都能追溯到用户的需求吗？**

### 4. Goal-Driven Execution

**定义成功标准。循环验证直到达成。**

多步任务，先说计划：
```
1. [步骤] → 验证：[检查点]
2. [步骤] → 验证：[检查点]
3. [步骤] → 验证：[检查点]
```

| 不要说... | 改成... |
|-----------|---------|
| "添加验证" | "写测试覆盖无效输入，然后让测试通过" |
| "修复 bug" | "写测试复现 bug，然后让测试通过" |
| "重构 X" | "确保重构前后测试都通过" |

## 工具使用

- Claude Code CLI + SDK
- Superpowers：代码审查、架构评审
- GStack：辅助分析
- karpathy-guidelines：行为准则
