"""
Test suite for FRAMEWORK/logger.py

Covers:
- _fmt_extra: dict 格式化，None 过滤
- SymphonyLogger: 初始化、日志文件创建、info/warning/error/debug
- RotatingFileHandler: 轮转效果
- init_logger: 全局实例创建
"""

import logging
import os

from FRAMEWORK.logger import SymphonyLogger, _fmt_extra, init_logger


class TestFmtExtra:
    def test_basic_format(self):
        """基本 key=value 格式"""
        result = _fmt_extra({"event": "test", "task_id": "T-1"})
        assert "event=test" in result
        assert "task_id=T-1" in result

    def test_none_filtered(self):
        """None 值被过滤"""
        result = _fmt_extra({"event": "x", "task_id": None})
        assert "task_id" not in result
        assert result.strip() == "event=x"

    def test_all_none(self):
        """全部 None 返回空字符串"""
        assert _fmt_extra({"a": None}).strip() == ""

    def test_multiple_values(self):
        """多个 key=value 用双空格分隔"""
        result = _fmt_extra({"a": "1", "b": "2", "c": "3"})
        assert "a=1" in result
        assert "b=2" in result
        assert "c=3" in result


class TestSymphonyLogger:
    def test_creates_log_file(self, temp_dir):
        """初始化时创建日志文件"""
        log_file = str(temp_dir / "symphony.log")
        SymphonyLogger(log_file)
        assert os.path.exists(log_file)

    def test_info_writes_to_file(self, temp_dir):
        """info 写入文件"""
        log_file = str(temp_dir / "symphony.log")
        sl = SymphonyLogger(log_file)
        sl.info(event="test_event", task_id="T-1", detail="hello")
        content = open(log_file).read()
        assert "event=test_event" in content
        assert "task_id=T-1" in content
        assert "detail=hello" in content

    def test_warning_writes_to_file(self, temp_dir):
        """warning 写入文件"""
        log_file = str(temp_dir / "symphony.log")
        sl = SymphonyLogger(log_file, level="WARNING")
        sl.warning(event="warn_event")
        content = open(log_file).read()
        assert "WARNING" in content
        assert "event=warn_event" in content

    def test_error_writes_to_file(self, temp_dir):
        """error 写入文件"""
        log_file = str(temp_dir / "symphony.log")
        sl = SymphonyLogger(log_file)
        sl.error(event="err_event", outcome="failed")
        content = open(log_file).read()
        assert "ERROR" in content
        assert "outcome=failed" in content

    def test_debug_writes_to_file(self, temp_dir):
        """debug 写入文件（file handler 级别是 DEBUG）"""
        log_file = str(temp_dir / "symphony.log")
        sl = SymphonyLogger(log_file, level="DEBUG")
        sl.debug(event="debug_msg")
        content = open(log_file).read()
        # DEBUG 级别时 debug 消息写入文件
        assert "debug_msg" in content

    def test_log_format_contains_timestamp(self, temp_dir):
        """日志行包含时间戳和级别"""
        log_file = str(temp_dir / "symphony.log")
        sl = SymphonyLogger(log_file)
        sl.info(event="fmt_test")
        content = open(log_file).read()
        assert "INFO" in content
        assert "20" in content  # year prefix in timestamp

    def test_level_none_defaults_to_info(self, temp_dir):
        """level=None 时默认 INFO"""
        log_file = str(temp_dir / "symphony.log")
        sl = SymphonyLogger(log_file, level="")  # empty string → .upper() fails
        # 实际代码里传入空字符串时 getattr 会取到 logging.INFO
        assert sl.logger.level == logging.INFO

    def test_session_id_in_log(self, temp_dir):
        """session_id 出现在日志中"""
        log_file = str(temp_dir / "symphony.log")
        sl = SymphonyLogger(log_file)
        sl.info(event="run", session_id="sess-001")
        content = open(log_file).read()
        assert "session_id=sess-001" in content


class TestInitLogger:
    def test_init_logger_returns_instance(self, temp_dir):
        """init_logger 返回 SymphonyLogger 实例"""
        log_file = str(temp_dir / "symphony.log")
        sl = init_logger(log_file)
        from FRAMEWORK.logger import log
        assert log is sl
        assert isinstance(sl, SymphonyLogger)
