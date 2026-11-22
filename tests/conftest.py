"""pytest 全局夹具：为离线测试补齐 import 期必需的环境变量。

api/deps.py 在模块顶层强校验 AGENT_JWT_SECRET（fail-fast，防上线漏配密钥），
但测试只用 TestClient + dependency_overrides 假身份，根本不走真实 JWT 校验。
这里在【任何测试模块被收集/import 之前】注入一个测试专用值，
保证 tests/test_stream_frames.py 这类要 import api.* 的用例能离线收集。

注意用 setdefault：真实环境若已配好密钥，绝不覆盖。

另外：core.permission.decide() 每次裁决都会旁路调用 record_audit_log 写
audit_logs 表。审计是「旁路、失败不影响裁决」，但离线跑测试时每次写审计
都要等一次 MySQL 连接失败（实测每次约 2s，13 个权限用例拖慢 26s）。
autouse 夹具把它替换成 no-op —— 权限单元测试断言的是裁决结果，
不是审计落库；真实审计链路属于联调/集成测试（C-2/C-4）的覆盖范围。
"""
import os

import pytest

os.environ.setdefault("AGENT_JWT_SECRET", "test-only-secret-do-not-use-in-prod")


@pytest.fixture(autouse=True)
def _offline_no_audit(monkeypatch):
    """隔离审计旁路：单元测试不连真实 MySQL。"""
    monkeypatch.setattr("core.permission.record_audit_log",
                        lambda *a, **k: None)
