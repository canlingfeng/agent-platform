import os
from core.permission import decide, ALLOW, DENY, ASK, _subject, ROOT

# 用 ROOT 动态推导路径，避免写死绝对路径（项目换目录/换机器也不会失效）
PROJECT_ROOT = os.path.dirname(ROOT)

def test_workspace内文件放行():
    # 在 workspace 内的路径 → 应该 ALLOW
    assert decide("read_file", {"file_path": os.path.join(ROOT, "a.txt")}) == ALLOW

def test_workspace外文件拒绝():
    # 项目根下的文件（workspace 之外）→ 应该 DENY
    assert decide("write_file", {"file_path": os.path.join(PROJECT_ROOT, "outside.txt")}) == DENY

def test_路径穿越攻击():
    # workspace/../../secret.txt → 应该 DENY
    assert decide("read_file", {"file_path": os.path.join(ROOT, "..", "..", "secret.txt")}) == DENY

def test_危险命令转询问():
    # format d: 无元字符、不在白名单 → 应该 ASK（选择权交还用户）
    assert decide("execute_command", {"command": "format d:"}) == ASK

def test_普通命令放行():
    # dir C:\ 是白名单 dir 的前缀扩展 → 应该 ALLOW（验证前缀匹配）
    assert decide("execute_command", {"command": "dir C:\\"}) == ALLOW

def test_词边界误伤防护():
    # directory 不因以 dir 开头就被放行 → 掉出白名单 → 应该 ASK
    assert decide("execute_command", {"command": "directory.bat"}) == ASK

def test_重定向借道拦截():
    # 含 > → 元字符闸直接 DENY（明确危险，不问人）
    assert decide("execute_command", {"command": "echo hello > a.txt"}) == DENY

def test_管道串联拦截():
    # 含 | → 元字符闸直接 DENY（明确危险，不问人）
    assert decide("execute_command", {"command": "echo a | more"}) == DENY

def test_其他工具默认拒绝():
    # 未知工具 → 应该 DENY
    assert decide("unknown_tool", {}) == DENY

def test_read跨盘符路径拒绝():
    # C 盘路径与 ROOT(D 盘) 不同盘符 → commonpath 抛 ValueError → 应该 DENY
    assert decide("read_file", {"file_path": "C:/Windows/system32/drivers/etc/hosts"}) == DENY

def test_write跨盘符路径拒绝():
    # C 盘路径与 ROOT(D 盘) 不同盘符 → commonpath 抛 ValueError → 应该 DENY
    assert decide("write_file", {"file_path": "C:/Windows/system32/drivers/etc/hosts"}) == DENY

def test_白名单外命令转询问():
    # ls 不在白名单、无元字符 → 应该 ASK（默认问人而非拒绝）
    assert decide("execute_command", {"command": "ls"}) == ASK

def test_subject生成提示语():
    # _subject 生成 ask 确认弹窗上给用户看的那句话
    p = os.path.join(ROOT, "a.txt")
    assert _subject("read_file", {"file_path": p}) == f"read_file:{p}"
    assert _subject("write_file", {"file_path": "b.txt"}) == "write_file:b.txt"
