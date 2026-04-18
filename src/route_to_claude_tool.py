"""
route_to_claude_code tool — model-driven routing to Claude Code.

When the LLM (MiniMax) decides a user message warrants Claude Code execution,
it calls this tool. The tool routes the task to the Claude Code MCP session
via the gateway's routing context (chat_id, platform) set before the agent ran.

MCP tools used:
  - mcp_claude_code_claude_session_start: Start Claude Code with the task
  - mcp_claude_code_claude_session_poll: Get accumulated output
  - mcp_claude_code_claude_session_send: Send user response to running session
  - mcp_claude_code_claude_session_stop: Stop session
"""

import json
import os
import asyncio
import threading
from datetime import datetime
from typing import Optional

from tools.registry import registry


def check_requirements() -> bool:
    """Claude Code MCP server must be configured and reachable."""
    return True  # Always available; MCP tool handles its own availability


def route_to_claude_code(task: str, task_id: str = None) -> str:
    """
    Route a coding task to Claude Code via MCP.

    Args:
        task: The task description for Claude Code to execute.
        task_id: Optional task identifier.

    Returns:
        JSON status string with task_id and routing info.
    """
    if not task or not task.strip():
        return json.dumps({"error": "task is required and cannot be empty"})

    task = task.strip()

    # Get routing context from gateway message processing
    try:
        from gateway.run import get_routing_context, get_gateway_runner
        ctx = get_routing_context()
        runner = get_gateway_runner()
    except Exception:
        ctx = {}
        runner = None

    chat_id = ctx.get("chat_id") or os.environ.get("HERMES_CURRENT_CHAT_ID", "")
    platform = ctx.get("platform") or os.environ.get("HERMES_CURRENT_PLATFORM", "cli")
    thread_id = ctx.get("thread_id")

    if not chat_id:
        # No active gateway session — fall back to spawning Claude Code directly
        return _start_direct_session(task)

    # Generate a unique task_id for tracking
    task_id_str = task_id or datetime.now().strftime("%H%M%S") + "_" + os.urandom(3).hex()

    # Use the gateway's running event loop to schedule Claude Code session start
    # This works because gateway message processing runs in an async context
    if runner is not None:
        try:
            loop = asyncio.get_event_loop()
            if loop.is_running():
                # We're in an async context — schedule the MCP session start
                loop.create_task(_start_claude_session_async(
                    task=task,
                    chat_id=chat_id,
                    platform=platform,
                    thread_id=thread_id,
                    task_id=task_id_str,
                    runner=runner,
                ))
                return json.dumps({
                    "status": "routed",
                    "task_id": task_id_str,
                    "message": f"Claude Code task queued for chat {chat_id}. "
                               f"Use claude_session_poll to get output.",
                })
        except RuntimeError:
            pass

    # Fallback: start session directly via registry dispatch
    return _start_direct_session(task, task_id_str)


async def _start_claude_session_async(
    task: str,
    chat_id: str,
    platform: str,
    thread_id: Optional[str],
    task_id: str,
    runner,
) -> None:
    """Start a Claude Code session asynchronously within the gateway event loop.

    Uses registry.dispatch via asyncio.to_thread to avoid blocking the event loop.
    With wait_for_ready=True, waits for initial output before returning.
    """
    try:
        # Use asyncio.to_thread to call synchronous registry.dispatch
        # This prevents blocking the event loop during the MCP call
        result = await asyncio.to_thread(
            registry.dispatch,
            "mcp_claude_code_claude_session_start",
            {"prompt": task, "wait_for_ready": True},
            task_id=task_id,
        )
        runner.logger.info("Claude Code session started: %s", str(result)[:200])
    except Exception as e:
        runner.logger.warning("Failed to start Claude Code session: %s", e)


def _start_direct_session(task: str, task_id: Optional[str] = None) -> str:
    """Start Claude Code session directly via registry dispatch (non-gateway path).

    This is the fallback when the gateway async path is unavailable.
    Waits for initial output and returns it directly to the user.
    """
    task_id_str = task_id or datetime.now().strftime("%H%M%S") + "_" + os.urandom(3).hex()

    try:
        # Start session with wait_for_ready=True to get initial output
        # This blocks until Claude Code produces first output or 5 min timeout
        result = registry.dispatch(
            "mcp_claude_code_claude_session_start",
            {"prompt": task, "wait_for_ready": True},
            task_id=task_id_str,
        )

        # Parse the result - return actual output, not "routed" status
        if isinstance(result, str):
            try:
                parsed = json.loads(result)
                # If there's an error, return it
                if "error" in parsed:
                    return json.dumps({
                        "error": parsed.get("error"),
                        "hint": "Ensure the Claude Code MCP server is running at http://127.0.0.1:8765/mcp "
                                "and that mcp_servers.claude_code is configured in ~/.hermes/config.yaml",
                    })
                # Return the actual result text
                return json.dumps({
                    "result": parsed.get("result", ""),
                })
            except json.JSONDecodeError:
                # Raw string result
                return json.dumps({"result": result})
        else:
            return json.dumps({"result": str(result)})

    except Exception as e:
        return json.dumps({
            "error": f"Failed to start Claude Code session: {e}",
            "hint": "Ensure the Claude Code MCP server is running at http://127.0.0.1:8765/mcp "
                    "and that mcp_servers.claude_code is configured in ~/.hermes/config.yaml",
        })


# ── Registry registration ──────────────────────────────────────────────────────

SCHEMA = {
    "name": "route_to_claude_code",
    "description": (
        "将任务转交给 Claude Code（Cloud Code）执行。\n\n"
        "【明确触发】满足任一即调用：\n"
        "1. 用户明确说「用 Claude Code / Cloud Code 做...」\n"
        "2. 用户说「请帮我写代码」「帮我新建...」「帮我实现...」\n"
        "3. 用户说「帮我改 bug」「帮我 debug这个问题」\n"
        "4. 用户说「帮我重构这段代码」\n"
        "5. 用户说「帮我用 Claude Code」或类似显式要求\n\n"
        "【不触发】即使提到代码也不调用：\n"
        "- 「帮我看看这个项目/代码」→ Hermes 自己分析\n"
        "- 「帮我检查有什么 bug」→ Hermes 自己检查\n"
        "- 「安装/更新/升级」→ Hermes 自己处理\n"
        "- 「这个代码是什么意思」→ Hermes 自己解释\n"
        "- 任何没有明确要求 Claude Code 参与的请求\n\n"
        "原则：Claude Code 是重型武器，用于真正的代码编写/重构/debug。"
        "简单分析、解释、检查类任务，Hermes 自身足以完成，不要浪费 Claude Code。"
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "task": {
                "type": "string",
                "description": "用户要求 Claude Code 执行的任务描述。",
            },
            "task_id": {
                "type": "string",
                "description": "可选的任务标识符，用于跟踪。",
            },
        },
        "required": ["task"],
    },
}


def _handler(args: dict, **kwargs) -> str:
    """Sync wrapper — dispatches to the main function."""
    return route_to_claude_code(
        task=args.get("task", ""),
        task_id=args.get("task_id"),
    )


registry.register(
    name="route_to_claude_code",
    toolset="delegation",
    schema=SCHEMA,
    handler=_handler,
    description=SCHEMA["description"],
    check_fn=check_requirements,
)
