#!/usr/bin/env python3
"""
Hermes Claude Code MCP Server

Provides multi-turn Claude Code session management as MCP tools.
Run with: uvx --python 3.11 mcp_claude_server.py

Or configure in ~/.hermes/config.yaml:
```yaml
mcp_servers:
  claude_code:
    command: "uvx"
    args: ["--python", "3.11", "/path/to/mcp_claude_server.py"]
    env:
      ANTHROPIC_BASE_URL: "https://api.minimaxi.com/anthropic"
      ANTHROPIC_AUTH_TOKEN: "sk-cp-..."
      ANTHROPIC_MODEL: "MiniMax-M2.7-highspeed"
```

Tools provided:
- claude_session_start: Start a new Claude Code session
- claude_session_send: Send a message to the running session
- claude_session_poll: Get accumulated output
- claude_session_stop: Stop the session
- claude_session_status: Check if session is running/waiting
"""

import os
import sys
import json
import asyncio
import threading
import tempfile
import atexit
import time
from typing import Any, Optional, Callable
from pathlib import Path

# Import MCP server SDK
from mcp.server import Server
from mcp.server.stdio import stdio_server
from mcp.types import Tool, TextContent


# Claude Session Implementation
# Protocol: JSON lines over stdout/stderr
# All messages: {"type":"...", ...}
# Types: session_started, text, tool, wait, done, error, ack
NODE_SERVER_TEMPLATE = '''
const { query } = require('@anthropic-ai/claude-agent-sdk');

class MessageStream {
  constructor() { this.q = []; this.r = null; this.done = false; }
  push(t, s) {
    this.q.push({type:"user",message:{role:"user",content:t},parent_tool_use_id:null,session_id:s});
    if(this.r){this.r();this.r=null;}
  }
  end() { this.done=true; if(this.r){this.r();this.r=null;} }
  async *[Symbol.asyncIterator]() {
    while(true){
      while(this.q.length) yield this.q.shift();
      if(this.done) return;
      await new Promise(r=>{this.r=r;});
    }
  }
}

let stream = null;
let sessionId = null;
let waitingForInput = false;

function send(msg) {
  // JSON line with proper encoding - all content is JSON-safe
  process.stdout.write(JSON.stringify(msg) + "\\n");
}

async function startSession(params) {
  const { prompt, workdir, model, plugins, hooks, agents, settings } = params;
  console.error("[DEBUG Node] startSession called, workdir=" + workdir + ", model=" + model);
  try {
    console.error("[DEBUG Node] SDK loaded successfully");
  } catch(e) {
    console.error("[DEBUG Node] SDK require error: " + e.message);
    send({type: "error", message: "SDK require failed: " + e.message});
    return;
  }
  stream = new MessageStream();
  stream.push(prompt, "");
  // Don't call stream.end() here - let query() complete all messages first
  // stream.end() will be called when user sends 'stop'
  waitingForInput = false;

  const q = query({
    prompt: stream,
    options: {
      cwd: workdir || process.cwd(),
      model: model || process.env.ANTHROPIC_MODEL || "MiniMax-M2.7-highspeed",
      permissionMode: "bypassPermissions",
      includePartialMessages: true,
      maxTurns: 200,
      plugins: plugins || undefined,
      hooks: hooks || undefined,
      agents: agents || undefined,
      settings: settings || undefined
    }
  });

  for await (const msg of q) {
    if (msg.type === "system" && msg.subtype === "hook_started") {
      // hook_started is the first message and has session_id
      if (!sessionId && msg.session_id) {
        sessionId = msg.session_id;
        send({type: "session_started", session_id: sessionId});
      }
    } else if (msg.type === "system" && msg.subtype === "init") {
      // init may also have session_id, update if we got a new one
      if (!sessionId && msg.session_id) {
        sessionId = msg.session_id;
        send({type: "session_started", session_id: sessionId});
      }
    } else if (msg.type === "assistant") {
      const content = msg.message?.content || [];
      for (const b of content) {
        if (b.type === "text") send({type: "text", content: b.text});
        if (b.type === "tool_use") send({type: "tool", name: b.name, input: b.input});
      }
    } else if (msg.type === "result") {
      if (msg.subtype === "success" && stream && !stream.done) {
        waitingForInput = true;
        send({type: "wait"});
      } else {
        waitingForInput = false;
        stream = null;
        send({type: "done", subtype: msg.subtype, turns: msg.num_turns, cost: msg.total_cost_usd, result: msg.result});
        break;
      }
    } else if (msg.type === "error") {
      send({type: "error", message: msg.error || "Unknown error", code: msg.code});
      waitingForInput = false;
      stream = null;
      break;
    }
  }
}

const readline = require('readline');
const rl = readline.createInterface({
  input: process.stdin,
  output: process.stdout,
  terminal: false
});

rl.on('line', (line) => {
  try {
    const msg = JSON.parse(line);
    if (msg.method === 'start') {
      startSession(msg.params).catch(e => send({type: "error", message: e.message}));
      send({type: "ack", ack: "started"});
    } else if (msg.method === 'send') {
      if (stream && waitingForInput) {
        waitingForInput = false;
        stream.push(msg.params.text, sessionId||"");
        send({type: "ack", ack: "sent"});
      } else {
        send({type: "ack", ack: "no-session"});
      }
    } else if (msg.method === 'ping') {
      send({type: "ack", ack: "ping", waiting: waitingForInput});
    } else if (msg.method === 'stop') {
      if (stream) stream.end();
      send({type: "ack", ack: "stopped"});
    }
  } catch(e) {
    process.stderr.write("Parse error: " + e.message + "\\n");
  }
});

rl.on('close', () => process.exit(0));
'''


def _load_hermes_env():
    """Load environment from ~/.hermes/.env file."""
    env_path = Path.home() / ".hermes" / ".env"
    env_vars = {}
    if env_path.exists():
        with open(env_path) as f:
            for line in f:
                line = line.strip()
                if "=" in line and not line.startswith("#"):
                    key, _, value = line.partition("=")
                    env_vars[key] = value
    return env_vars


def _load_installed_plugins():
    """Scan ~/.claude/plugins/installed_plugins.json and return list of local plugin configs."""
    plugins_json = Path.home() / ".claude" / "plugins" / "installed_plugins.json"
    if not plugins_json.exists():
        return []

    try:
        with open(plugins_json) as f:
            data = json.load(f)

        local_plugins = []
        for plugin_name, installations in data.get("plugins", {}).items():
            for install in installations:
                install_path = install.get("installPath")
                scope = install.get("scope", "user")
                # Include both local and user-scoped plugins (not project-specific)
                if install_path and scope in ("local", "user"):
                    local_plugins.append({
                        "type": "local",
                        "path": install_path
                    })
        return local_plugins
    except (json.JSONDecodeError, IOError):
        return []

def _extract_first_sk_cp_key(text):
    """Extract first sk-cp- key from potentially corrupted .env value."""
    import re
    match = re.search(r'sk-cp-[a-zA-Z0-9\-]+', text)
    return match.group(0) if match else None


class AsyncWriter:
    """Async-compatible wrapper for blocking stdin write + drain."""
    def __init__(self, pipe):
        self._pipe = pipe
    def write(self, data):
        self._pipe.write(data)
    async def drain(self):
        # flush() can block if pipe buffer is full, run in thread to avoid deadlock
        await asyncio.to_thread(self._pipe.flush)
    def close(self):
        self._pipe.close()


class ClaudeSession:
    """Manages a Claude Code session via Node.js subprocess."""
    
    # Track all server script paths for cleanup
    _server_paths = set()
    
    # Safety timer: if no output for this many seconds, session is considered stuck
    SAFETY_TIMEOUT_SECONDS = 15
    
    def __init__(self, workdir="/Users/zoe/workspace", model="MiniMax-M2.7-highspeed",
                 api_key=None, api_base=None):
        self.workdir = workdir
        self.model = model
        
        # Load env from ~/.hermes/.env since hermes-agent doesn't pass env vars to child processes
        hermes_env = _load_hermes_env()
        
        # MiniMax API key: prefer explicit param, then env vars, then .env file
        # The .env file may have corrupted values, so extract just the sk-cp- key
        self.api_key = api_key
        if not self.api_key:
            self.api_key = os.environ.get("ANTHROPIC_API_KEY", "") or os.environ.get("ANTHROPIC_AUTH_TOKEN", "")
            if not self.api_key and "MINIMAX_API_KEY" in hermes_env:
                raw = hermes_env["MINIMAX_API_KEY"]
                self.api_key = _extract_first_sk_cp_key(raw) or raw
        
        self.api_base = api_base or os.environ.get("ANTHROPIC_BASE_URL", "https://api.minimaxi.com/anthropic")
        
        self.env = os.environ.copy()
        self.env["ANTHROPIC_BASE_URL"] = self.api_base
        # Use ANTHROPIC_API_KEY as the primary key (MiniMax Token Plan uses this)
        self.env["ANTHROPIC_API_KEY"] = self.api_key
        if "ANTHROPIC_AUTH_TOKEN" in self.env:
            del self.env["ANTHROPIC_AUTH_TOKEN"]

        # Ensure @anthropic-ai/claude-agent-sdk can be found when running
        # the temp Node script from /tmp. Add workspace node_modules to NODE_PATH.
        workspace_node_modules = os.path.join(os.path.dirname(workdir or self.workdir), "node_modules")
        if os.path.isdir(workspace_node_modules):
            node_path = self.env.get("NODE_PATH", "")
            self.env["NODE_PATH"] = f"{workspace_node_modules}{os.pathsep}{node_path}" if node_path else workspace_node_modules
        self.env["ANTHROPIC_MODEL"] = self.model
        # Set NODE_PATH so Node can find @anthropic-ai/claude-agent-sdk
        # SDK is at /Users/zoe/workspace/node_modules (installed there)
        # Use the directory this file is in as the workspace root
        server_dir = os.path.dirname(os.path.abspath(__file__))
        node_modules_path = os.path.join(server_dir, "node_modules")
        if os.path.isdir(node_modules_path):
            self.env["NODE_PATH"] = node_modules_path
        
        self.proc: Optional[asyncio.subprocess.Process] = None
        self.running = False
        self.waiting = False
        self.session_id: Optional[str] = None
        self.output: list = []
        self.output_lock = threading.Lock()
        self._reader_task: Optional[asyncio.Task] = None
        self._stderr_task: Optional[asyncio.Task] = None
        self._stderr_buffer: list = []  # Capture stderr for debugging (max 100 lines)
        self._stderr_max_lines = 100
        self._startup_event: Optional[asyncio.Event] = None
        
        # Safety timer: reset on each output line, fires callback if stuck
        self._safety_timer: Optional[threading.Timer] = None
        self._safety_callback: Optional[callable] = None
        self._last_output_time: float = 0
        
        # Instance variables for session parameters (used by _create_server_script)
        self._plugins = None
        self._hooks = None
        self._agents = None
        self._settings = None
        
        # Write server script to a unique temp file
        self._create_server_script()
    
    def _create_server_script(self):
        """Create the Node.js server script in a temp file."""
        temp_dir = tempfile.gettempdir()
        script_name = f"mcp_claude_server_{id(self)}.js"
        self.server_path = os.path.join(temp_dir, script_name)
        ClaudeSession._server_paths.add(self.server_path)
        
        # Format template with default values (None -> null in JS)
        plugins_js = "null" if self._plugins is None else json.dumps(self._plugins)
        hooks_js = "null" if self._hooks is None else json.dumps(self._hooks)
        agents_js = "null" if self._agents is None else json.dumps(self._agents)
        settings_js = "null" if self._settings is None else json.dumps(self._settings)
        
        # Use str.replace() instead of .format() to avoid {query} conflict with JS syntax
        server_script = NODE_SERVER_TEMPLATE
        server_script = server_script.replace('{plugins}', plugins_js)
        server_script = server_script.replace('{hooks}', hooks_js)
        server_script = server_script.replace('{agents}', agents_js)
        server_script = server_script.replace('{settings}', settings_js)
        
        with open(self.server_path, "w") as f:
            f.write(server_script)
    
    def _reset_safety_timer(self):
        """Reset the safety timer. Call on every output line received."""
        self._last_output_time = time.time()
        if self._safety_timer:
            self._safety_timer.cancel()
        if self._safety_callback and self.running:
            self._safety_timer = threading.Timer(
                self.SAFETY_TIMEOUT_SECONDS,
                self._safety_timer_fired
            )
            self._safety_timer.daemon = True
            self._safety_timer.start()
    
    def _safety_timer_fired(self):
        """Called when safety timer expires (no output for SAFETY_TIMEOUT_SECONDS)."""
        if self.running and self._safety_callback:
            print(f"[SafetyTimer] No output for {self.SAFETY_TIMEOUT_SECONDS}s — triggering timeout callback", file=sys.stderr)
            try:
                self._safety_callback(self)
            except Exception as e:
                print(f"[SafetyTimer] Callback error: {e}", file=sys.stderr)
    
    def _cancel_safety_timer(self):
        """Cancel the safety timer."""
        if self._safety_timer:
            self._safety_timer.cancel()
            self._safety_timer = None
    
    def set_safety_callback(self, callback: callable):
        """Set a callback to fire when safety timer expires (no output for 15s)."""
        self._safety_callback = callback
        if self.running:
            self._reset_safety_timer()
    
    @classmethod
    def cleanup_all_scripts(cls):
        """Clean up all server scripts. Called on exit."""
        for path in cls._server_paths:
            try:
                if os.path.exists(path):
                    os.unlink(path)
            except OSError:
                pass
        cls._server_paths.clear()
    
    def _read_stdout_thread(self):
        """Read stdout in background THREAD (not asyncio task). Parses JSON-line protocol.

        Using a thread instead of asyncio task avoids event loop lifecycle issues
        when the MCP server is called from hermes-agent's registry.dispatch()
        which uses asyncio.run() for each call.
        """
        buffer = b""  # Buffer to hold incomplete lines
        import errno
        try:
            while self.running and self.proc and self.proc.stdout:
                try:
                    stdout_fd = self.proc.stdout.fileno()
                    # Direct read - no select/poll (macOS PTY incompatibility)
                    data = os.read(stdout_fd, 4096)
                except OSError as e:
                    if e.errno == errno.EAGAIN:
                        import time; time.sleep(0.01)
                        continue
                    break

                if not data:
                    # EOF
                    break

                # Add new data to buffer
                buffer += data

                # Extract complete lines (split on newline)
                while b"\n" in buffer:
                    line_bytes, buffer = buffer.split(b"\n", 1)
                    decoded_line = line_bytes.decode("utf-8", errors="replace").strip()
                    if not decoded_line:
                        continue

                    # Reset safety timer on every meaningful output line
                    self._reset_safety_timer()

                    # Parse JSON-line protocol
                    parsed = self._parse_json_line(decoded_line)

                    with self.output_lock:
                        self.output.append(parsed)

                    if parsed["type"] == "wait":
                        self.waiting = True
                        if self._startup_event:
                            self._startup_event.set()
                    elif parsed["type"] == "session_started":
                        self.session_id = parsed.get("session_id")
                        if self._startup_event:
                            self._startup_event.set()
                    elif parsed["type"] == "done":
                        self.waiting = False
                        self.running = False
                        self._cancel_safety_timer()
                        if self._startup_event:
                            self._startup_event.set()
                    elif parsed["type"] == "error":
                        self.waiting = False
                        self.running = False
                        self._cancel_safety_timer()
                        if self._startup_event:
                            self._startup_event.set()
                    elif parsed["type"] == "ack":
                        # Don't set startup event on ack - session_started must arrive first
                        pass

        except Exception as e:
            print(f"[_read_stdout_thread] Error in stdout reader: {e}", file=sys.stderr)

    def _parse_json_line(self, line: str) -> dict:
        """Parse a JSON output line from Node process.
        
        New protocol: all messages are JSON objects with a 'type' field.
        Example: {"type":"text","content":"hello"}
                 {"type":"wait"}
                 {"type":"done","subtype":"success","turns":3,"cost":0.05}
        
        Returns dict with 'type' key.
        """
        try:
            obj = json.loads(line)
            if isinstance(obj, dict) and "type" in obj:
                return obj
        except json.JSONDecodeError:
            pass
        
        # Fallback: old text-line protocol for backwards compatibility
        if line.startswith("WAIT"):
            return {"type": "wait"}
        elif line.startswith("SESSION:"):
            return {"type": "session_started", "session_id": line[8:]}
        elif line.startswith("DONE:"):
            try:
                data = json.loads(line[5:])
                # Use standard field names (subtype/turns/cost) consistent with Node protocol
                return {"type": "done", **data}
            except json.JSONDecodeError:
                return {"type": "done", "raw": line[5:]}
        elif line.startswith("ERROR:"):
            try:
                data = json.loads(line[6:])
                return {"type": "error", "message": data.get("m", "Unknown")}
            except json.JSONDecodeError:
                return {"type": "error", "message": line[6:]}
        elif line.startswith("ACK:"):
            return {"type": "ack", "ack": line[4:]}
        elif line.startswith("TXT:"):
            return {"type": "text", "content": line[4:]}
        elif line.startswith("TOOL:"):
            return {"type": "tool", "name": line[5:]}
        
        return {"type": "unknown", "raw": line}
    
    async def _read_stdout(self):
        """Async wrapper that runs _read_stdout_thread in a thread pool."""
        await asyncio.to_thread(self._read_stdout_thread)

    async def _read_stderr(self):
        """Read stderr (debug log)."""
        try:
            while self.running and self.proc and self.proc.stderr:
                try:
                    line = await asyncio.wait_for(
                        self.proc.stderr.readline(),
                        timeout=1.0
                    )
                except asyncio.TimeoutError:
                    continue

                if not line:
                    break

                # Capture stderr for debugging (with size limit to prevent OOM)
                decoded = line.decode("utf-8", errors="replace").strip()
                if decoded:
                    if len(self._stderr_buffer) >= self._stderr_max_lines:
                        self._stderr_buffer.pop(0)  # Remove oldest
                    self._stderr_buffer.append(decoded)
        except asyncio.CancelledError:
            pass
        except Exception:
            pass
    
    async def start(self, prompt: str, workdir: str = None, model: str = None,
                    plugins: list = None, hooks: dict = None,
                    agents: dict = None, settings: dict = None) -> dict:
        """Start a new session."""
        # Stop any existing session first
        if self.proc:
            await self.stop()
        
        # Override model if provided
        if model:
            self.model = model
        
        # Save parameters as instance variables for _create_server_script
        self._plugins = plugins
        self._hooks = hooks
        self._agents = agents
        self._settings = settings

        # Re-generate server script with the actual plugins/agents/settings parameters
        self._create_server_script()

        wd = workdir or self.workdir

        try:
            import subprocess
            self._sync_proc = subprocess.Popen(
                ["node", self.server_path],
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                env=self.env,
                cwd=wd
            )
            # Wrap in async-compatible interface for stdin/stdout access
            class AsyncProcessWrapper:
                def __init__(self, proc):
                    self._proc = proc
                    self.stdin = AsyncWriter(proc.stdin)
                    self.stdout = proc.stdout
                    self.stderr = proc.stderr
                @property
                def returncode(self):
                    return self._proc.returncode
                def terminate(self):
                    self._proc.terminate()
                async def wait(self):
                    self._proc.wait()
            self.proc = AsyncProcessWrapper(self._sync_proc)
        except FileNotFoundError:
            return {
                "status": "error",
                "error": "Node.js not found. Please install Node.js to use Claude Code sessions."
            }
        except PermissionError as e:
            return {
                "status": "error",
                "error": f"Permission denied when trying to run Node.js: {e}"
            }
        except Exception as e:
            return {
                "status": "error",
                "error": f"Failed to start Node.js process: {e}"
            }
        
        self.running = True
        self.waiting = False
        self.output = []
        self.session_id = None
        self._startup_event = asyncio.Event()
        
        # Start reader tasks using the CURRENT event loop (not a new one)
        self._reader_task = asyncio.create_task(self._read_stdout())
        self._stderr_task = asyncio.create_task(self._read_stderr())
        
        # Start safety timer
        self._reset_safety_timer()
        
        # Send start command
        msg = json.dumps({
            "method": "start",
            "params": {
                "prompt": prompt,
                "workdir": wd,
                "model": self.model,
                "plugins": plugins,
                "hooks": hooks,
                "agents": agents,
                "settings": settings
            }
        }) + "\n"
        self.proc.stdin.write(msg.encode())
        await self.proc.stdin.drain()

        # Wait for session_started (not ack) with timeout
        try:
            await asyncio.wait_for(self._startup_event.wait(), timeout=30.0)
        except asyncio.TimeoutError:
            pass

        # Check if process exited early (crashed)
        if self.proc and self.proc.returncode is not None:
            stderr_output = "\n".join(self._stderr_buffer[-20:])  # Last 20 lines
            return {
                "status": "error",
                "error": f"Node process exited early (code {self.proc.returncode}). Stderr: {stderr_output[:500]}"
            }

        return {"status": "started", "session_id": self.session_id}
    
    async def send(self, text: str) -> dict:
        """Send a follow-up message.
        
        Note: Does NOT consume output. Caller should poll with get_new_output()
        to collect the response. Use the session's is_waiting() to detect when
        the new turn has produced its WAIT signal.
        """
        if not self.running:
            return {"error": "Session not running"}
        
        if not self.proc or not self.proc.stdin:
            return {"error": "Session process not available"}
        
        try:
            msg = json.dumps({"method": "send", "params": {"text": text}}) + "\n"
            self.proc.stdin.write(msg.encode())
            await self.proc.stdin.drain()
            return {"status": "sent"}
        except Exception as e:
            return {"error": f"Failed to send message: {e}"}
    
    async def stop(self):
        """Stop the session."""
        self._cancel_safety_timer()
        self.running = False
        
        # Cancel reader tasks first to prevent them from processing more output
        if self._reader_task:
            self._reader_task.cancel()
            # Allow the task to clean up - ignore if it was a mock or already done
            try:
                await asyncio.wait_for(asyncio.shield(self._reader_task), timeout=0.1)
            except (asyncio.CancelledError, asyncio.TimeoutError, TypeError):
                pass
            self._reader_task = None
        if self._stderr_task:
            self._stderr_task.cancel()
            try:
                await asyncio.wait_for(asyncio.shield(self._stderr_task), timeout=0.1)
            except (asyncio.CancelledError, asyncio.TimeoutError, TypeError):
                pass
            self._stderr_task = None
        
        if self.proc:
            process = self.proc
            self.proc = None
            
            try:
                try:
                    msg = json.dumps({"method": "stop", "params": {}}) + "\n"
                    process.stdin.write(msg.encode())
                    await asyncio.wait_for(process.stdin.drain(), timeout=1.0)
                except asyncio.TimeoutError:
                    # Drain timeout means pipe buffer is full - stop message may not have been sent
                    # This is non-fatal since we'll terminate the process anyway
                    import sys
                    print("[stop] drain() timeout - stop message may be lost, will force terminate", file=sys.stderr)
                except Exception:
                    pass
                finally:
                    try:
                        process.stdin.close()
                    except Exception:
                        pass
                
                try:
                    await asyncio.wait_for(process.wait(), timeout=2.0)
                    return
                except asyncio.TimeoutError:
                    pass
                except Exception:
                    pass
                
                try:
                    process.terminate()
                    await asyncio.wait_for(process.wait(), timeout=1.0)
                except asyncio.TimeoutError:
                    # Graceful termination failed - force kill
                    try:
                        process.kill()
                        await asyncio.wait_for(process.wait(), timeout=1.0)
                    except Exception:
                        pass
                except Exception:
                    pass

            finally:
                process = None
        
        self._cleanup_server_script()
    
    def _cleanup_server_script(self):
        """Clean up the server script file."""
        try:
            if os.path.exists(self.server_path):
                os.unlink(self.server_path)
                ClaudeSession._server_paths.discard(self.server_path)
        except OSError:
            pass
    
    def is_running(self) -> bool:
        """Check if the session process is running."""
        if not self.running or self.proc is None:
            return False
        if self.proc.returncode is not None:
            return False
        return True
    
    def is_waiting(self) -> bool:
        """Check if the session is waiting for input."""
        return self.waiting and self.is_running()
    
    def get_new_output(self) -> list:
        """Get new output lines since last call. Returns list of parsed dicts."""
        with self.output_lock:
            out = list(self.output)
            self.output = []
            return out
    
    def get_all_output(self) -> list:
        """Get all accumulated output without clearing. Returns list of parsed dicts."""
        with self.output_lock:
            return list(self.output)

    def get_stderr(self) -> list:
        """Get captured stderr output."""
        return list(self._stderr_buffer)


# Register cleanup on exit
atexit.register(ClaudeSession.cleanup_all_scripts)

# Global session instance
_session: Optional[ClaudeSession] = None
_session_lock: asyncio.Lock = asyncio.Lock()  # Protect async session operations
_session_threading_lock: threading.Lock = threading.Lock()  # Protect sync get_session()


def get_session() -> ClaudeSession:
    global _session
    with _session_threading_lock:
        if _session is None:
            _session = ClaudeSession()
        return _session


async def reset_session_async():
    """Reset the global session asynchronously (useful for testing)."""
    global _session
    if _session is not None:
        await _session.stop()
    _session = None


def reset_session():
    """Reset the global session (useful for testing).
    
    Note: This only works when called from within an async context.
    For proper async cleanup, use reset_session_async() instead.
    """
    global _session
    if _session is not None:
        try:
            loop = asyncio.get_running_loop()
            # Schedule cleanup if we have a running loop
            asyncio.create_task(reset_session_async())
        except RuntimeError:
            # No running loop - try to get the event loop (may fail)
            try:
                loop = asyncio.get_event_loop()
                if loop.is_running():
                    # Loop is running but we're not in an async context
                    # This is a limitation - caller should use reset_session_async
                    pass
                else:
                    loop.run_until_complete(reset_session_async())
            except RuntimeError:
                # No event loop available at all - do best-effort cleanup
                old_session = _session
                _session = None
                # Best-effort cleanup of old session subprocess/tasks
                if old_session is not None:
                    try:
                        loop = asyncio.new_event_loop()
                        loop.run_until_complete(old_session.stop())
                    except Exception:
                        pass
                    finally:
                        loop.close()
                _session = ClaudeSession()


# Create MCP Server
server = Server("claude-code")


@server.list_tools()
async def list_tools() -> list[Tool]:
    """List available Claude Code session tools."""
    return [
        Tool(
            name="claude_session_start",
            description="Start a new Claude Code multi-turn session. The session stays open for follow-up messages until stopped.",
            inputSchema={
                "type": "object",
                "properties": {
                    "prompt": {
                        "type": "string",
                        "description": "The task prompt to execute"
                    },
                    "workdir": {
                        "type": "string",
                        "description": "Working directory (defaults to /Users/zoe/workspace)"
                    },
                    "model": {
                        "type": "string",
                        "description": "Model name (defaults to MiniMax-M2.7-highspeed)"
                    },
                    "wait_for_ready": {
                        "type": "boolean",
                        "description": "If true, wait up to 60s for the session to reach 'waiting' state before returning. If false (default), returns immediately after starting."
                    },
                    "plugins": {
                        "type": ["array", "string"],
                        "items": {
                            "type": "object",
                            "properties": {
                                "type": {"type": "string", "enum": ["local"]},
                                "path": {"type": "string"}
                            },
                            "required": ["type", "path"]
                        },
                        "description": "Local plugins to load (e.g. Superpowers). Can be array or JSON-stringified array (for clients that double-serialize)."
                    },
                    "agents": {
                        "type": "object",
                        "description": "Custom agents configuration. Key is agent name, value is agent definition with prompt, description, tools, model, etc."
                    },
                    "hooks": {
                        "type": "object",
                        "description": "Hooks for customizing agent behavior. Key is hook name, value is hook function definition."
                    },
                    "settings": {
                        "type": "object",
                        "description": "Settings for customizing agent execution. E.g. temperature, max_tokens, system_prompt."
                    }
                },
                "required": ["prompt"]
            }
        ),
        Tool(
            name="claude_session_send",
            description="Send a follow-up message to the running Claude Code session.",
            inputSchema={
                "type": "object",
                "properties": {
                    "text": {
                        "type": "string",
                        "description": "The message to send to the session"
                    },
                    "wait_for_response": {
                        "type": "boolean",
                        "description": "If true, wait up to 60s for the session to produce output and reach 'waiting' state. If false (default), returns immediately."
                    }
                },
                "required": ["text"]
            }
        ),
        Tool(
            name="claude_session_poll",
            description="Get accumulated output from the running session. Returns text output, tool calls, and status.",
            inputSchema={
                "type": "object",
                "properties": {}
            }
        ),
        Tool(
            name="claude_session_status",
            description="Check if a session is running and waiting for input.",
            inputSchema={
                "type": "object",
                "properties": {}
            }
        ),
        Tool(
            name="claude_session_stop",
            description="Stop the running Claude Code session.",
            inputSchema={
                "type": "object",
                "properties": {}
            }
        ),
    ]


def _format_output(parsed: dict) -> str:
    """Format a parsed output dict into a human-readable string."""
    t = parsed.get("type", "unknown")
    if t == "text":
        content = parsed.get("content", "")
        # Strip ALL tool metadata prefixes that SDK includes in text content
        # e.g., "[Tool: Skill(...)]text[Tool: Bash(...)]more" -> "textmore"
        import re
        # Use loop to handle nested brackets (e.g. [Tool: [nested]])
        prev = None
        while prev != content:
            prev = content
            content = re.sub(r'\[Tool: [^\]]*\]', '', content)
        return content.strip()
    elif t == "tool":
        # Don't show tool metadata to user - tool calls are already processed
        # and displayed through their output. This is internal debug info.
        return ""
    elif t == "session_started":
        # Internal message - don't show to user
        return ""
    elif t == "done":
        result_data = parsed.get("result")
        if result_data:
            # Extract text from result
            if isinstance(result_data, str):
                text = result_data
            elif isinstance(result_data, dict):
                text = result_data.get("text") or result_data.get("content") or result_data.get("message") or str(result_data)
            else:
                text = str(result_data)
            return f"[Result: {text}]\n"
        data = parsed.get("raw") or f"subtype={parsed.get('subtype')}, turns={parsed.get('turns')}, cost={parsed.get('cost')}"
        return f"[Done: {data}]\n"
    elif t == "error":
        return f"[Error: {parsed.get('message', 'unknown')}]\n"
    elif t == "ack":
        # Internal message - don't show to user
        return ""
    elif t == "wait":
        return ""
    elif t == "unknown":
        return f"[Unknown: {parsed.get('raw', '')}]"
    return ""


def _parse_output_line(line: bytes | str) -> dict:
    """Parse a single output line from the Node process.
    
    Args:
        line: Raw output line as bytes or string, or already a parsed dict
        
    Returns:
        dict with 'type' key: 'text', 'tool', 'session', 'wait', 'done', 'error', 'ack', or 'unknown'
        and additional relevant keys.
    """
    # Already parsed dict (from get_new_output)
    if isinstance(line, dict):
        return line
    
    # Decode once at the start instead of in each branch
    if isinstance(line, str):
        decoded = line
        # For string prefix checks, use string prefixes
        if line.startswith("TXT:"):
            return {"type": "text", "content": decoded[4:]}
        elif line.startswith("TOOL:"):
            return {"type": "tool", "name": decoded[5:]}
        elif line.startswith("SESSION:"):
            return {"type": "session", "id": decoded[8:]}
        elif line.startswith("WAIT"):
            return {"type": "wait"}
        elif line.startswith("DONE:"):
            try:
                data = json.loads(decoded[5:])
                # Spread data fields to top level so dedup can access result/data
                return {"type": "done", **data}
            except json.JSONDecodeError:
                return {"type": "done", "raw": decoded[5:]}
        elif line.startswith("ERROR:"):
            try:
                data = json.loads(decoded[6:])
                return {"type": "error", "message": data.get("m", "Unknown error"), "code": data.get("c")}
            except json.JSONDecodeError:
                return {"type": "error", "message": decoded[6:]}
        elif line.startswith("ACK:"):
            return {"type": "ack", "content": decoded[4:]}
        else:
            return {"type": "unknown", "raw": decoded}
    else:
        # Handle bytes input (for backwards compatibility and tests)
        decoded = line.decode('utf-8', errors='replace')
        if line.startswith(b"TXT:"):
            return {"type": "text", "content": decoded[4:]}
        elif line.startswith(b"TOOL:"):
            return {"type": "tool", "name": decoded[5:]}
        elif line.startswith(b"SESSION:"):
            return {"type": "session", "id": decoded[8:]}
        elif line.startswith(b"WAIT"):
            return {"type": "wait"}
        elif line.startswith(b"DONE:"):
            try:
                data = json.loads(decoded[5:])
                # Spread data fields to top level so dedup can access result/data
                return {"type": "done", **data}
            except json.JSONDecodeError:
                return {"type": "done", "raw": decoded[5:]}
        elif line.startswith(b"ERROR:"):
            try:
                data = json.loads(decoded[6:])
                return {"type": "error", "message": data.get("m", "Unknown error"), "code": data.get("c")}
            except json.JSONDecodeError:
                return {"type": "error", "message": decoded[6:]}
        elif line.startswith(b"ACK:"):
            return {"type": "ack", "content": decoded[4:]}
        else:
            return {"type": "unknown", "raw": decoded}


@server.call_tool()
async def call_tool(name: str, arguments: Any) -> list[TextContent]:
    """Handle tool calls."""
    session = get_session()
    
    if name == "claude_session_start":
        prompt = arguments["prompt"]
        workdir = arguments.get("workdir")
        model = arguments.get("model")
        plugins = arguments.get("plugins")
        hooks = arguments.get("hooks")
        agents = arguments.get("agents")
        settings = arguments.get("settings")
        wait_for_ready = arguments.get("wait_for_ready", False)

        # Handle double-serialized JSON strings from MCP client
        # If plugins/hooks/agents/settings are strings, try to parse them
        if isinstance(plugins, str):
            try:
                plugins = json.loads(plugins)
            except json.JSONDecodeError:
                pass
        if isinstance(hooks, str):
            try:
                hooks = json.loads(hooks)
            except json.JSONDecodeError:
                pass
        if isinstance(agents, str):
            try:
                agents = json.loads(agents)
            except json.JSONDecodeError:
                pass
        if isinstance(settings, str):
            try:
                settings = json.loads(settings)
            except json.JSONDecodeError:
                pass

        # Auto-load installed plugins if none provided
        if not plugins:
            plugins = _load_installed_plugins()

        # Serialize session operations to prevent concurrent start() races
        print(f"[DEBUG claude_session_start] calling session.start(), is_running={session.is_running()}", file=sys.stderr)
        async with _session_lock:
            result = await session.start(prompt, workdir, model, plugins, hooks, agents, settings)
        print(f"[DEBUG claude_session_start] result={result}, is_running={session.is_running()}", file=sys.stderr)
        
        if result.get("status") == "error":
            return [TextContent(type="text", text=f"Error: {result.get('error', 'Unknown error')}")]
        
        accumulated = []
        if wait_for_ready:
            # Poll until we see a 'wait' signal (Claude ready for next input).
            # This is the most reliable indicator - it means Claude has finished
            # generating its response for this turn.
            start = time.time()
            
            while session.is_running() and time.time() - start < 300:  # up to 5 min
                await asyncio.sleep(0.5)
                outputs = session.get_new_output()
                accumulated.extend([o for o in outputs if o.get('type') not in ('ack', 'session_started')])
                
                # Check if we've seen a WAIT signal for this turn
                if any(o.get('type') == 'wait' for o in accumulated):
                    break
                
                if not session.is_running():
                    break
        else:
            # wait_for_ready=False: return immediately.
            # The caller will use send() which waits for response separately.
            # We MUST NOT wait here - doing so causes a deadlock because the reader
            # thread tries to write to stdout while call_tool is blocked on asyncio.to_thread.
            pass
        
        text_outputs = []
        seen_text_contents = set()  # Track actual text content for deduplication
        for item in accumulated:
            parsed = _parse_output_line(item)
            t = parsed.get("type")

            # Deduplication check BEFORE append
            dedup_key = None
            if t == "done":
                result_data = parsed.get("result")
                if result_data:
                    if isinstance(result_data, str):
                        dedup_key = result_data
                    elif isinstance(result_data, dict):
                        dedup_key = result_data.get("text") or result_data.get("content") or result_data.get("message") or str(result_data)
                    else:
                        dedup_key = str(result_data)
            elif t == "text":
                dedup_key = parsed.get("content", "")

            if dedup_key and dedup_key in seen_text_contents:
                continue

            formatted = _format_output(parsed)
            if formatted:
                text_outputs.append(formatted)
                if dedup_key:
                    seen_text_contents.add(dedup_key)
        
        status = "running" if session.is_running() else "done"
        waiting = "waiting" if session.is_waiting() else "processing"
        
        response = f"Session started: {status}, {waiting}\n"
        if text_outputs:
            response += "Output:\n" + "".join(text_outputs)
        
        if wait_for_ready and waiting == "processing" and session.is_running():
            response += "\nNote: Session still processing, use claude_session_poll to check status."
        
        return [TextContent(type="text", text=response)]
    
    elif name == "claude_session_send":
        if not session.is_running():
            return [TextContent(type="text", text="No active session. Use claude_session_start first.")]

        text = arguments["text"]
        wait_for_response = arguments.get("wait_for_response", False)

        # Send without waiting - just write to stdin like the CLI does.
        # The query loop processes asynchronously.
        result = await session.send(text)

        if result.get("error"):
            return [TextContent(type="text", text=f"Error: {result['error']}")]

        if wait_for_response:
            # Poll until we see a 'wait' signal from the CURRENT turn.
            # The WAIT from the previous turn is already consumed.
            # We identify a NEW wait by: we've collected some content AND
            # the session has re-entered waiting state.
            accumulated = []
            start = time.time()
            last_wait_seen = None  # track which WAIT we're waiting for
            
            while session.is_running() and time.time() - start < 600:
                await asyncio.sleep(0.5)
                outputs = session.get_new_output()
                accumulated.extend([o for o in outputs if o.get('type') not in ('ack', 'session_started')])
                
                # Check for new WAIT signal
                for o in outputs:
                    if o.get('type') == 'wait':
                        last_wait_seen = o.get('timestamp', time.time())
                        break
                
                # Break if we got real content AND we've seen a new WAIT
                if accumulated and last_wait_seen is not None:
                    break
                
                if not session.is_running():
                    break
            
            outputs = accumulated
        else:
            # Brief wait for response
            await asyncio.sleep(2)
            outputs = session.get_new_output()

        text_outputs = []
        for item in outputs:
            parsed = _parse_output_line(item)
            formatted = _format_output(parsed)
            if formatted:
                text_outputs.append(formatted)

        status = "running" if session.is_running() else "done"
        waiting = "waiting" if session.is_waiting() else "processing"
        response = f"Status: {status}, {waiting}\n"
        if text_outputs:
            response += "".join(text_outputs)
        else:
            response += "(waiting for output...)"
        
        return [TextContent(type="text", text=response)]
    
    elif name == "claude_session_poll":
        # Get output first (regardless of running state - content may exist after session ends)
        outputs = session.get_new_output()

        text_outputs = []
        seen_text_contents = set()  # Track actual text content for deduplication
        for item in outputs:
            parsed = _parse_output_line(item)
            t = parsed.get("type")

            # Deduplication check BEFORE append
            dedup_key = None
            if t == "done":
                result_data = parsed.get("result")
                if result_data:
                    if isinstance(result_data, str):
                        dedup_key = result_data
                    elif isinstance(result_data, dict):
                        dedup_key = result_data.get("text") or result_data.get("content") or result_data.get("message") or str(result_data)
                    else:
                        dedup_key = str(result_data)
            elif t == "text":
                dedup_key = parsed.get("content", "")

            if dedup_key and dedup_key in seen_text_contents:
                continue

            formatted = _format_output(parsed)
            if formatted:
                text_outputs.append(formatted)
                if dedup_key:
                    seen_text_contents.add(dedup_key)

        # If we have output, return it
        if text_outputs:
            return [TextContent(type="text", text="".join(text_outputs))]

        # No output - check if session is still active or really dead
        if not session.is_running():
            return [TextContent(type="text", text="No active session.")]

        return [TextContent(type="text", text="(no new output)")]
    
    elif name == "claude_session_status":
        if not session.is_running():
            return [TextContent(type="text", text="No active session")]
        
        status = "running" if session.is_running() else "done"
        waiting = "waiting" if session.is_waiting() else "processing"
        sid = session.session_id or "unknown"
        
        return [TextContent(type="text", text=f"Session: {sid}\nStatus: {status}, {waiting}")]
    
    elif name == "claude_session_stop":
        await session.stop()
        return [TextContent(type="text", text="Session stopped")]
    
    else:
        return [TextContent(type="text", text=f"Unknown tool: {name}")]


async def main():
    """Run the MCP server."""
    async with stdio_server() as (read_stream, write_stream):
        await server.run(read_stream, write_stream, server.create_initialization_options())


# =============================================================================
# HTTP Mode (FastMCP)
# =============================================================================

def run_http_mode(host: str = "127.0.0.1", port: int = 8765):
    """Run MCP server in HTTP mode using FastMCP."""
    import uvicorn
    from mcp.server import FastMCP

    mcp = FastMCP(
        "claude-code",
        host=host,
        port=port,
        streamable_http_path="/mcp",
    )

    # Get the session instance - use closure to store session
    _session_holder = [None]  # Mutable container for session reference

    def get_http_session():
        # Use the same global session as call_tool for consistency
        if _session_holder[0] is None:
            _session_holder[0] = get_session()
        return _session_holder[0]

    # Wrapper functions that delegate to our existing session management
    # We use the call_tool dispatch internally

    async def claude_session_start(
        prompt: str,
        workdir: str = None,
        model: str = None,
        plugins: list = None,
        hooks: dict = None,
        agents: dict = None,
        settings: dict = None,
        wait_for_ready: bool = False,
    ) -> str:
        """Start a new Claude Code session."""
        session = get_http_session()
        arguments = {
            "prompt": prompt,
            "workdir": workdir,
            "model": model,
            "plugins": plugins,
            "hooks": hooks,
            "agents": agents,
            "settings": settings,
            "wait_for_ready": wait_for_ready,
        }
        results = await call_tool("claude_session_start", arguments)
        # Debug: log the result
        result_text = "\n".join([r.text for r in results])
        print(f"[HTTP claude_session_start] response length={len(result_text)}, first 300 chars: {result_text[:300]!r}", file=sys.stderr)
        return result_text

    async def claude_session_send(
        text: str,
        wait_for_response: bool = False,
    ) -> str:
        """Send a message to the running session."""
        session = get_http_session()
        arguments = {
            "text": text,
            "wait_for_response": wait_for_response,
        }
        results = await call_tool("claude_session_send", arguments)
        return "\n".join([r.text for r in results])

    async def claude_session_poll() -> str:
        """Get accumulated output."""
        session = get_http_session()
        arguments = {}
        results = await call_tool("claude_session_poll", arguments)
        return "\n".join([r.text for r in results])

    async def claude_session_status() -> str:
        """Check if session is running/waiting."""
        session = get_http_session()
        arguments = {}
        results = await call_tool("claude_session_status", arguments)
        return "\n".join([r.text for r in results])

    async def claude_session_stop() -> str:
        """Stop the session."""
        session = get_http_session()
        arguments = {}
        results = await call_tool("claude_session_stop", arguments)
        return "\n".join([r.text for r in results])

    # Register tools with FastMCP
    mcp.add_tool(
        claude_session_start,
        name="claude_session_start",
        description="Start a new Claude Code session",
    )
    mcp.add_tool(
        claude_session_send,
        name="claude_session_send",
        description="Send a message to the running session",
    )
    mcp.add_tool(
        claude_session_poll,
        name="claude_session_poll",
        description="Get accumulated output",
    )
    mcp.add_tool(
        claude_session_status,
        name="claude_session_status",
        description="Check if session is running/waiting",
    )
    mcp.add_tool(
        claude_session_stop,
        name="claude_session_stop",
        description="Stop the session",
    )

    import uvicorn

    starlette_app = mcp.streamable_http_app()

    config = uvicorn.Config(
        starlette_app,
        host=host,
        port=port,
        log_level="warning",
    )
    server = uvicorn.Server(config)

    # Use asyncio.run() so Starlette lifespan (which calls session_manager.run())
    # is properly executed before handling requests. Daemon thread approach
    # bypasses lifespan, causing "Task group is not initialized" errors.
    import asyncio
    asyncio.run(server.serve())


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser(description="Claude Code MCP Server")
    parser.add_argument("--http", action="store_true", help="Run in HTTP mode")
    parser.add_argument("--host", default="127.0.0.1", help="HTTP host (default: 127.0.0.1)")
    parser.add_argument("--port", type=int, default=8765, help="HTTP port (default: 8765)")
    args = parser.parse_args()

    if args.http:
        run_http_mode(host=args.host, port=args.port)
    else:
        asyncio.run(main())
