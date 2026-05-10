"""
Test suite for FRAMEWORK/agent_types.py

Covers:
- Session dataclass construction and defaults
- AgentResult dataclass construction
- AgentAdapter interface (all methods raise NotImplementedError)
"""

import pytest

from FRAMEWORK.agent_types import Session, AgentResult, AgentAdapter


class TestSession:
    def test_construct_with_minimal_args(self):
        """Session 最少参数构造"""
        s = Session(id="sess-1", pid=1234)
        assert s.id == "sess-1"
        assert s.pid == 1234
        assert s.workspace is None
        assert s.process is None

    def test_construct_with_all_args(self):
        """Session 全部参数构造"""
        s = Session(id="sess-2", pid=5678, workspace="ws1", process="proc1")
        assert s.id == "sess-2"
        assert s.pid == 5678
        assert s.workspace == "ws1"
        assert s.process == "proc1"

    def test_immutable_fields(self):
        """dataclass 字段可读写"""
        s = Session(id="s", pid=1)
        s.pid = 999
        assert s.pid == 999

    def test_repr(self):
        """Session 有可读的 repr"""
        s = Session(id="sess-1", pid=1234)
        r = repr(s)
        assert "sess-1" in r
        assert "1234" in r


class TestAgentResult:
    def test_construct_with_minimal_args(self):
        """AgentResult 最少参数构造"""
        r = AgentResult(status="success", exit_code=0, stdout="out", stderr="err", result={})
        assert r.status == "success"
        assert r.exit_code == 0
        assert r.stdout == "out"
        assert r.stderr == "err"
        assert r.result == {}

    def test_failed_result(self):
        """失败结果"""
        r = AgentResult(status="failed", exit_code=1, stdout="", stderr="error msg", result={"error": "timeout"})
        assert r.status == "failed"
        assert r.result["error"] == "timeout"

    def test_repr(self):
        """AgentResult 有可读的 repr"""
        r = AgentResult(status="success", exit_code=0, stdout="ok", stderr="", result={"key": "val"})
        assert "success" in repr(r)


class TestAgentAdapter:
    def test_start_session_raises_not_implemented(self):
        """基类 start_session 抛 NotImplementedError"""
        adapter = AgentAdapter()
        with pytest.raises(NotImplementedError):
            adapter.start_session(None, "prompt")

    def test_wait_session_raises_not_implemented(self):
        """基类 wait_session 抛 NotImplementedError"""
        adapter = AgentAdapter()
        with pytest.raises(NotImplementedError):
            adapter.wait_session(None)

    def test_stop_session_raises_not_implemented(self):
        """基类 stop_session 抛 NotImplementedError"""
        adapter = AgentAdapter()
        with pytest.raises(NotImplementedError):
            adapter.stop_session(None)

    def test_is_process_alive_raises_not_implemented(self):
        """基类 is_process_alive 抛 NotImplementedError"""
        adapter = AgentAdapter()
        with pytest.raises(NotImplementedError):
            adapter.is_process_alive(0)

    def test_can_subclass_and_override(self):
        """可以继承并覆写方法"""
        class MockAdapter(AgentAdapter):
            def start_session(self, workspace, prompt):
                return Session(id="mock", pid=1)

        adapter = MockAdapter()
        s = adapter.start_session(None, "hello")
        assert s.id == "mock"
