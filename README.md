# terminal recorder（`tr_start` / `tr_end`）

用 Python 标准库（`pty`、`termios`、`select`、`json` 等，无第三方依赖）实现的终端录制工具：

1. 在终端执行 `tr_start xxx.json` 开始录制；
2. 随后在打开的 shell 中正常执行 bash 命令，所有键入的输入和屏幕输出都会按顺序记录；
3. 在 shell 中执行 `tr_end` 结束录制；
4. 结束时生成一个 `xxx.json` 文件。

## 为什么是一个嵌套 bash

`tr_start` 无法截获"启动它的那个 shell"此前的输出，因此录制会话是一个运行在独立 pty 上的**嵌套交互式 bash**（与 `script` 工具的思路相同）：

- 用户的 `~/.bashrc`、提示符、别名、补全、作业控制都正常工作；
- 录制从 `tr_start` 之后开始，`tr_start` 命令本身不在录制范围内；
- 执行 `tr_end`（或 `exit` / `Ctrl-D`）后回到启动 `tr_start` 的外层 shell。

## 快速开始（无需安装）

```bash
cd /home/alpaca/termial_recoder
export PATH="$PWD/bin:$PATH"

tr_start ~/session.json
# 现在这是一个被录制的嵌套 bash，正常敲命令，例如：
#   echo hello
#   ls -la
tr_end
# 回到外层 shell，生成 ~/session.json
python3 -m json.tool ~/session.json
```

也可以把 `bin/` 永久加入 `PATH`（写进 `~/.bashrc` 或 `/etc/profile.d/` 均可）：

```bash
export PATH="/home/alpaca/termial_recoder/bin:$PATH"
```

可选安装（当前 Python 被标记为 PEP 668 externally-managed，建议用 venv）：

```bash
python3 -m venv .venv
. .venv/bin/activate
pip install -e .
tr_start --help
```

## 命令说明

### `tr_start [-f] [--shell PATH] TARGET`

| 参数 | 说明 |
| --- | --- |
| `TARGET` | 要生成的 JSON 文件路径 |
| `-f, --force` | 覆盖已存在的目标文件，并丢弃残留的 `.partial.jsonl` |
| `--shell` | 指定要录制的 bash（默认取 PATH 上的 `bash`） |
| `--version` | 显示版本 |

退出码：`0` 录制完成且文件已保存；`1` 运行时/保存失败；`2` 调用错误（参数、非 tty、目标已存在、嵌套会话等）。

### `tr_end`

`tr_end` 在录制会话中由录制器通过环境导出的 bash 函数提供，行为等同于"结束录制并退出这个嵌套 shell"。同时仓库也提供 `bin/tr_end` 可执行文件作为兜底（例如函数被用户 rc 覆盖时，可用 `command tr_end` 或直接调用它），两者都会通知录制器结束会话。

也可以直接输入 `exit` 或按 `Ctrl-D` 结束：此时 `end_reason` 为 `shell_exit`。

## 输出 JSON 结构

```json
{
  "format": "termrec",
  "version": 1,
  "session": {
    "id": "20251120T000454-092a64",
    "started_at": "2025-11-20T00:04:54.003575+08:00",
    "ended_at": "2025-11-20T00:04:54.627922+08:00",
    "duration_ms": 624,
    "end_reason": "tr_end",
    "shell_exit_status": 0,
    "target": "/home/alpaca/session.json",
    "argv": ["tr_start", "/home/alpaca/session.json"],
    "cwd": "/home/alpaca",
    "user": "alpaca",
    "hostname": "host",
    "uname": "Linux-6.18.0-x86_64-with-glibc2.43",
    "python": "3.14.4",
    "shell": "/usr/bin/bash",
    "shell_argv": ["/usr/bin/bash", "-i"],
    "shell_pid": 12345,
    "recorder_pid": 12340,
    "terminal": {"term": "xterm-256color", "columns": 120, "rows": 32}
  },
  "stats": {
    "events": 17,
    "input_events": 4,
    "output_events": 13,
    "input_bytes": 64,
    "output_bytes": 1450
  },
  "events": [
    {"seq": 1, "ts": 0.021, "dir": "output", "data": "alpaca@host:~$ "},
    {"seq": 2, "ts": 0.412, "dir": "input",  "data": "echo hello\r"},
    {"seq": 3, "ts": 0.413, "dir": "output", "data": "echo hello\r\n"},
    {"seq": 4, "ts": 0.415, "dir": "output", "data": "hello\r\nalpaca@host:~$ "}
  ]
}
```

约定：

- `events` 按真实读写顺序排列；`seq` 从 1 递增；`ts` 为相对会话开始的秒数，单调不减。
- `dir` 为 `input`（用户键入，含控制字符）或 `output`（shell/程序输出，stdout 与 stderr 已在 pty 中合并）。
- `data` 是 UTF-8 文本；跨块的多字节字符会被正确拼接，非法字节替换为 `U+FFFD`。
- `session.terminal` 记录会话开始时的终端尺寸；大小变化会同步给被录制的 shell，但不单独记录为事件。
- `end_reason` 取值：`tr_end`、`shell_exit`、`stdin_eof`、`stdout_eof`、`pty_eof`、`signal:SIGTERM`（及类似信号）、`error:<异常名>`。
- `session.ended_at` / `duration_ms` 在结束时写入；`stats` 与 `events` 数量一致。

## 删除与恢复

- 正常结束时，录制过程中的临时 spool 文件 `TARGET.partial.jsonl` 会被自动删除。
- 如果录制器被 `SIGKILL` 或机器掉电，最终 JSON 可能不存在，但 `TARGET.partial.jsonl` 会保留：它是每行一个事件的 NDJSON，可以直接 `head` 查看或自行转换为 JSON 数组。
- 若目标文件或 `.partial.jsonl` 已存在，`tr_start` 会拒绝（退出码 2）；使用 `-f/--force` 覆盖目标并丢弃残留 spool。

## 限制与注意事项

- **安全提示**：录制器会记录会话中键入的**一切**，包括密码、token 等敏感内容，且 JSON 为明文存储。请勿在不受信任的环境中录制敏感操作。
- 只支持 bash（`--shell` 也需为 bash 系）；zsh/fish 不在支持范围内，因为 `tr_end` 依赖 bash 的导出函数。
- stdout 与 stderr 在 pty 层面已合并，无法区分。
- 不解析"命令级"结构，只提供原始输入/输出事件流。
- 非法 UTF-8 字节会以 `U+FFFD` 记录，不保留原始字节。
- shell 退出后，之前用 `&` 放到后台的任务不再被录制；若后台任务仍占用 pty，录制器会短暂等待后关闭。
- 嵌套录制会被拒绝（录制器会设置 `TERMREC_ACTIVE`）。

## 测试

```bash
python3 -m unittest discover -s tests -t . -v
```

测试通过 pty 驱动真实的 `bin/tr_start`，覆盖：输入输出记录、`tr_end`/`exit` 结束、spool 生命周期、`--force`、非 tty 拒绝、嵌套拒绝、Ctrl-C 转发、非法 UTF-8、窗口尺寸传播、`tr_end` 可执行兜底，以及解码器和 JSON 组装单元测试。

## 目录结构

```
bin/tr_start, bin/tr_end   # 免安装启动脚本（也提供 pyproject 控制台脚本）
termrec/cli.py             # argparse 入口、前置检查、退出码
termrec/recorder.py        # fork/exec shell、pty 双向转发、关闭流程
termrec/terminal.py        # raw 模式、窗口尺寸
termrec/events.py          # 增量 UTF-8 解码、NDJSON spool、统计
termrec/document.py        # 由 spool 组装最终 JSON（原子写入）
tests/                     # pty 测试辅助 + 单元/集成测试
```
