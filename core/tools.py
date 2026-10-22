import datetime
import requests
import subprocess
import os
from core.permission import decide,DENY,ALLOW,ROOT,ASK,user_root
from core.context import get_identity
from dataclasses import dataclass
from typing import Callable
from langchain_core.tools import StructuredTool


UI_MODE = "webui"    # 默认 CLI，webui.py 启动时改成 "webui"


def get_weather(city):
    """获取指定城市的天气信息"""
    url = f"https://wttr.in/{city}?format=%C+%t"
    try:
        response = requests.get(url, timeout=5)
        return response.text
    except requests.RequestException as e:
        return f"请求天气信息失败：{e}"


def get_current_time():
    """获取当前时间"""
    return datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def _resolve_path(file_path):
    """解析路径，返回绝对路径。

    默认基于【当前用户】的根目录（``ROOT/{user_id}``），实现租户文件隔离；
    无用户上下文时（CLI / 调试）退化为全局 ROOT。
    绝对路径仍走 os.path.abspath 保持旧行为，但后续 decide() 会拦截越界访问。
    """
    root = user_root()
    if os.path.isabs(file_path):
        abs_path = os.path.abspath(file_path)
        return abs_path
    if file_path =="workspace":
        return root
    return os.path.join(root, file_path)


def read_file(file_path):
    """读取指定文件的内容"""
    file_path = _resolve_path(file_path)
    if not os.path.exists(file_path):
        return f"文件不存在：{file_path}"
    if decide("read_file", {"file_path": file_path}) == DENY:
        return "拒绝访问：文件路径不在允许的根目录下"
    if os.path.isdir(file_path):
        return f"{file_path} 是一个目录，请使用 list_dir 工具查看目录内容"
    try:
        with open(file_path, 'r', encoding='utf-8') as f:
            return f.read()
    except Exception as e:
        return f"读取文件失败：{e}"

def write_file(file_path,content):
    """写入内容到指定文件"""
    file_path = _resolve_path(file_path)
    if decide("write_file", {"file_path": file_path}) == DENY:
        return "拒绝访问：文件路径不在允许的根目录下"
    parent_dir=os.path.dirname(file_path)
    os.makedirs(parent_dir, exist_ok=True)  # 确保父目录存在
    try:
        with open(file_path, 'w', encoding='utf-8') as f:
            f.write(content)
        return "写入文件成功"
    except Exception as e:
        return f"写入文件失败：{e}"

def execute_command (command ,approve=False):
    """执行系统命令"""
    verdict=decide("execute_command",{"command":command})
    if verdict==DENY:
        return "拒绝执行：命令不在白名单或包含危险字符"
    elif verdict==ASK and not approve:
        if UI_MODE=="cli":
            user_input=input(f"命令 '{command}' 不在白名单，是否允许执行？(y/n): or 总是允许请输入always: ")
            if user_input.lower() == 'always':
                return execute_command(command, approve=True)
            elif user_input.lower() != 'y':
                return "用户拒绝执行命令"
        else:
            return f"NEED_CONFIRM:{command}"     
    try: 
        '''
        subprocess.run() 方法用于执行系统命令，并且可以捕获输出和错误信息。
        设置了超时时间为 10 秒，工作目录为 ROOT。 他是一个子进程执行命令的方式，
        返回一个 CompletedProcess 对象，其中包含了命令的输出和错误信息。
        这是多线程环境下执行命令的安全方式，避免阻塞主线程。
        '''
        result=subprocess.run(
            command, 
            shell=True, 
            check=True, 
            stdout=subprocess.PIPE, 
            stderr=subprocess.PIPE,
            text=True,
            timeout=10,
            cwd=ROOT  # 设置工作目录为 ROOT
            )
        return f"输出:\n{result.stdout}\n错误输出:\n{result.stderr}"
    except subprocess.TimeoutExpired:
        return "命令执行超时(10s)"
    except subprocess.CalledProcessError as e:
        return f"命令执行失败，返回码: {e.returncode}\n错误输出:\n{e.stderr}"


def list_dir(file_path):
    """列出指定目录下的所有文件和子目录。路径以 workspace 为根，例如 workspace、workspace/note、note/sert.txt。当用户提到的路径可能是目录时，优先使用本工具而不是 read_file。"""

    abs_path = _resolve_path(file_path)
    if decide("list_dir", {"file_path": abs_path}) == ALLOW:
        try:
            entries = os.listdir(abs_path)
            if not entries:
                return "目录为空"
            rel_entries = [os.path.relpath(os.path.join(abs_path, e), ROOT).replace("\\", "/") for e in entries]
            return "\n".join(rel_entries)
        except Exception as e:
            return f"列出目录失败：{e}"
    else:
        return "拒绝访问：目录不在允许的根目录下"


@dataclass    
class Tool:                   
    name: str
    fn: Callable
    description: str
    params: dict
    read_only: bool = False
    takes_slot: bool = False
TOOLS = [
        Tool(name="read_file", fn=read_file,
            description="读取指定文件的内容",
            params={"file_path": "以 workspace 为根的路径，如 workspace/note/sert.txt"},
            read_only=True, takes_slot=False),

        Tool(name="write_file", fn=write_file,
            description="写入内容到指定文件",
            params={"file_path": "以 workspace 为根的路径，如 workspace/note/sert.txt", "content": "要写入的内容"},
            read_only=False, takes_slot=False),

        Tool(name="execute_command", fn=execute_command,
            description="执行系统命令",
            params={"command": "要执行的命令，如 dir C:\\"},
            read_only=False, takes_slot=False),
        Tool(name="list_dir", fn=list_dir,
                description="列出指定目录下的所有文件和子目录",
                params={"file_path": "以 workspace 为根的路径，如 workspace/note"},
                read_only=True, takes_slot=False),
        Tool(name="get_weather", fn=get_weather,
            description="获取指定城市的天气信息",
            params={"city": "城市名称，如 beijing"},
            read_only=True, takes_slot=False),
        Tool(name="get_current_time", fn=get_current_time,
            description="获取当前时间",
            params={},
            read_only=True, takes_slot=False)
    ]

def execute_command_tool(command: str) -> str:
    """执行系统命令。仅当明确要执行命令时使用。"""
    return execute_command(command)

langchain_tools = [
    StructuredTool.from_function(func=read_file),
    StructuredTool.from_function(func=write_file),
    StructuredTool.from_function(func=execute_command_tool),
    StructuredTool.from_function(func=list_dir),
    StructuredTool.from_function(func=get_weather),
    StructuredTool.from_function(func=get_current_time),
]