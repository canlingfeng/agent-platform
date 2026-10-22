import os
import re
from storage.audit import record_audit_log
from core.context import get_identity

ALLOW,DENY,ASK="allow","deny","ask"
ROOT=os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),"workspace")
EXTRA_ROOT=[]
WHITELIST = [
    "dir",     
    "echo hello",
    "python --version",
    "cd",   
]
_SHELL_META = re.compile(r"[<>|;&\n`]|\$\(")

def _subject(tool,args)->str:
    """生成 subject 字符串"""
    if tool in ("read_file", "write_file"):
        return f"{tool}:{args.get('file_path','')}"
    elif tool == "execute_command":
        return f"{tool}:{args.get('command','')}"

def _in_specific_root(abs_path:str,root:str)->bool:
    """判断 abs_path 是否在 root 涵盖范围内（用 commonpath 一并支持'子目录'和'精确等于'两种姿势）"""
    try:
        # 注意括号位置：先算出 commonpath，再拿结果和 root 比较
        return os.path.commonpath([abs_path, os.path.realpath(root)]) == os.path.realpath(root)
    except ValueError:
        return False


def user_root() -> str:
    """当前用户的文件根目录。

    有用户上下文时返回 ``ROOT/{user_id}``——实现租户文件隔离，
    用户 A 无法读写用户 B 目录下的文件；
    无用户上下文时（CLI / 调试）退化为全局 ROOT，保持旧行为。
    """
    ident = get_identity()
    if ident is not None and ident.user_id is not None:
        return os.path.join(ROOT, str(ident.user_id))
    return ROOT


def _in_root(file_path:str)->bool:
    """检查文件路径是否在【当前用户】根目录或任一 EXTRA_ROOT 涵盖范围内"""
    abs_path=os.path.realpath(file_path)
    if _in_specific_root(abs_path, user_root()):
        return True
    for extra in EXTRA_ROOT:
        if _in_specific_root(abs_path,extra):
            return True
    return False

def _match(head,command:str)->bool:
    if command.startswith(head):
        if command==head:
            return True
        nxt=command[len(head)]# head 后的第一个字符
        return not(nxt.isalnum() or nxt == "_") #边界判断
    return False

def _has_shell_meta(command)->bool:
    """检查命令中是否包含 shell 元字符"""
    return _SHELL_META.search(command) is not None


def decide(tool, args) -> str:
    """
    裁决:这个工具是否允许执行，返回 ALLOW、DENY 或 ASK
    规则：
    1. read_file/write_file/list_dir: 只允许访问 ROOT 目录下的文件
    2. execute_command: 只允许白名单命令，且不含 shell 元字符
    3. 其他工具: 默认拒绝
    4. ASK 是 execute_command 遇到非白名单命令时，交由用户确认
    5. 每次裁决同步落 audit_logs（旁路，失败不影响裁决）
    """
    if tool in ("read_file", "write_file", "list_dir"):
        file_path = args.get("file_path", "")
        verdict = ALLOW if _in_root(file_path) else DENY
    elif tool == "execute_command":
        command = args.get("command", "")
        if _has_shell_meta(command):
            verdict = DENY
        else:
            verdict = ASK
            for white_item in WHITELIST:
                if _match(white_item, command):
                    verdict = ALLOW
                    break
    else:
        verdict = DENY  

    # 审计归属：从请求上下文取用户身份。CLI 下无上下文，对应列留 NULL。
    ident = get_identity()
    record_audit_log(
        tool, _subject(tool, args), verdict,
        user_id=ident.user_id if ident else None,
        session_id=ident.session_id if ident else None,
        client_ip=ident.client_ip if ident else None,
    )
    return verdict
