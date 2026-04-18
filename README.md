# Hermes Route to Claude Code

A Hermes plugin that delegates coding tasks to Claude Code via MCP (Model Context Protocol).

## What It Does

When Hermes detects a coding task (writing, modifying, debugging code), it automatically delegates to Claude Code for execution. Results are returned directly to the user.

## Features

- **Automatic routing**: Hermes LLM triggers `route_to_claude_code` when user asks for code work
- **MCP integration**: Uses `@anthropic-ai/claude-agent-sdk` for multi-round sessions
- **Real-time output**: Claude Code output returned via MCP polling mechanism
- **Plugin system**: Works with existing Hermes installation, no core modifications needed

## Architecture

```
User message → Hermes Agent → route_to_claude_code tool
                                    ↓
                            MCP Server (port 8765)
                                    ↓
                            Claude Code SDK
                                    ↓
                            Claude API
```

## Requirements

- Python 3.11+
- Node.js (for Claude Code SDK)
- Hermes Gateway running
- MCP Server: `http://127.0.0.1:8765/mcp`

## Quick Install

```bash
# 1. Clone this repo
git clone https://github.com/smasunspot/hermes-route-to-claude.git
cd hermes-route-to-claude

# 2. Copy tool to Hermes tools directory
cp src/route_to_claude_tool.py ~/.hermes/hermes-agent/tools/

# 3. Start MCP server
python mcp/mcp_claude_server.py --http --port 8765 &

# 4. Configure Hermes (add to ~/.hermes/config.yaml)
mcp_servers:
  claude_code:
    url: http://127.0.0.1:8765/mcp
    enabled: true
    timeout: 300
```

## Configuration

In `~/.hermes/config.yaml`:

```yaml
mcp_servers:
  claude_code:
    url: http://127.0.0.1:8765/mcp
    enabled: true
    timeout: 300
```

## Troubleshooting

```bash
# Check MCP server is running
ps aux | grep mcp_claude_server | grep -v grep

# Test MCP endpoint
curl -s -X POST http://127.0.0.1:8765/mcp/ \
  -H 'Content-Type: application/json' \
  -H 'Accept: application/json, text/event-stream' \
  -d '{"jsonrpc":"2.0","id":1,"method":"initialize","params":{...}}'

# Check Hermes logs
tail -50 ~/.hermes/logs/agent.log | grep -i mcp
```

## License

MIT
