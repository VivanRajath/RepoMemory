"""PTY-backed bash over a websocket, with test-run capture.

THIS IS A LOCAL DEMO SHELL. It gives whoever can reach the websocket a real shell on this machine.
Only ever run the backend bound to 127.0.0.1; never deploy this publicly.

Test capture: bash's PROMPT_COMMAND emits an invisible OSC escape sequence before each prompt carrying the exit
status and the last history line. We strip it from the output, and when the last command was a test command we
hand (command, exit code, output) to `on_command`.
"""
import asyncio
import os
import re
import shutil
import sys
import tempfile
import threading
from pathlib import Path

from fastapi import WebSocket, WebSocketDisconnect

if sys.platform == "win32":
    from winpty import PtyProcess
else:
    from ptyprocess import PtyProcessUnicode as PtyProcess

MARK_RE = re.compile(r"\x1b\]777;rm;(\d+);([^\x07]*)\x07")
TEST_CMD_RE = re.compile(r"^\s*(pytest|python3? -m pytest|npm (run )?test|npx (jest|vitest))\b")

RC = r"""
[ -f ~/.bashrc ] && source ~/.bashrc
[ -f .venv/Scripts/activate ] && source .venv/Scripts/activate
[ -f .venv/bin/activate ] && source .venv/bin/activate
export HISTCONTROL=
unset HISTTIMEFORMAT
__rm_mark() { local s=$?; local h; h=$(history 1); h="${h#*[0-9]  }"; printf '\033]777;rm;%s;%s\007' "$s" "$h"; }
PROMPT_COMMAND=__rm_mark
PS1='\[\e[36m\]repomemory\[\e[0m\]:\[\e[33m\]\W\[\e[0m\]$ '
"""


def find_bash() -> str:
    if sys.platform == "win32":
        for c in (r"C:\Program Files\Git\bin\bash.exe", r"C:\Program Files (x86)\Git\bin\bash.exe"):
            if Path(c).exists():
                return c
    return shutil.which("bash") or "/bin/bash"


async def terminal_session(ws: WebSocket, repo_dir: Path, on_command):
    await ws.accept()
    rc = Path(tempfile.gettempdir()) / "repomemory_bashrc"
    rc.write_text(RC, newline="\n")
    env = {**os.environ, "TERM": "xterm-256color"}
    proc = PtyProcess.spawn([find_bash(), "--rcfile", rc.as_posix(), "-i"], cwd=str(repo_dir), env=env,
                            dimensions=(30, 120))
    loop = asyncio.get_running_loop()
    out_q: asyncio.Queue = asyncio.Queue()

    def reader():
        while True:
            try:
                data = proc.read(4096)
            except EOFError:
                break
            except Exception:
                break
            if data:
                loop.call_soon_threadsafe(out_q.put_nowait, data)
        loop.call_soon_threadsafe(out_q.put_nowait, None)

    threading.Thread(target=reader, daemon=True).start()

    async def pump_out():
        captured = ""
        pending = ""
        while True:
            data = await out_q.get()
            if data is None:
                await ws.close()
                return
            data = pending + data
            pending = ""
            # an OSC marker can be split across reads; hold back a trailing partial one
            cut = data.rfind("\x1b]777;")
            if cut != -1 and "\x07" not in data[cut:]:
                data, pending = data[:cut], data[cut:]
            pos = 0
            for m in MARK_RE.finditer(data):
                captured += data[pos:m.start()]
                code, cmd = int(m[1]), m[2].strip()
                if TEST_CMD_RE.match(cmd):
                    out = captured[-6000:]
                    loop.run_in_executor(None, on_command, cmd, code, out)
                captured = ""
                pos = m.end()
            captured = (captured + data[pos:])[-20000:]
            clean = MARK_RE.sub("", data)
            if clean:
                await ws.send_text(clean)

    out_task = asyncio.create_task(pump_out())
    try:
        while True:
            msg = await ws.receive_text()
            if msg.startswith("\x00resize:"):  # control message: \x00resize:<cols>x<rows>
                cols, rows = msg[8:].split("x")
                proc.setwinsize(int(rows), int(cols))
            else:
                proc.write(msg)
    except (WebSocketDisconnect, RuntimeError):
        pass
    finally:
        out_task.cancel()
        try:
            proc.terminate(force=True)
        except Exception:
            pass
