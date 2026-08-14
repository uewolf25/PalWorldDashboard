"""ログの区分（INFO / WARN / ERROR）とその絞り込み（Issue #19）。

管理ツール自身のログは logging の levelno から決められるので推測しない。
ゲームサーバ側の行には決まった書式が無いので単語で当たりを付ける。
その2経路が混ざらないことを見る。
"""

from __future__ import annotations

import logging

import pytest

from app.logstream import BrokerLogHandler, LogBroker, detect_level


# ---- 推測 ------------------------------------------------------------------


@pytest.mark.parametrize(
    "line",
    [
        "LogPal: Error: failed to load",
        "Exception in thread",
        "Traceback (most recent call last):",
        "connection FAILED",
        "critical failure",
    ],
)
def test_error_lines_are_detected(line):
    assert detect_level(line) == "error"


@pytest.mark.parametrize(
    "line",
    ["Warning: low memory", "this api is deprecated", "WARN slow tick"],
)
def test_warn_lines_are_detected(line):
    assert detect_level(line) == "warn"


@pytest.mark.parametrize(
    "line",
    ["Server started", "player joined", "tick 60fps", ""],
)
def test_everything_else_is_info(line):
    assert detect_level(line) == "info"


def test_error_wins_over_warn():
    assert detect_level("warning: an error occurred") == "error"


def test_words_inside_other_words_do_not_match():
    """「terror」で error 判定になると、まともに使えない。"""
    assert detect_level("no terrors here") == "info"


# ---- 配信 ------------------------------------------------------------------


def test_published_records_carry_a_level():
    broker = LogBroker()
    broker.publish("Error: boom", source="server")
    assert broker.backlog()[0]["level"] == "error"


def test_an_explicit_level_is_not_overridden():
    """呼び出し側が分かっているなら推測しない。"""
    broker = LogBroker()
    broker.publish("これは error という語を含むが info", source="app", level="info")
    assert broker.backlog()[0]["level"] == "info"


def test_an_unknown_explicit_level_falls_back_to_detection():
    broker = LogBroker()
    broker.publish("Error: boom", source="app", level="nonsense")
    assert broker.backlog()[0]["level"] == "error"


def test_backlog_can_be_filtered():
    broker = LogBroker()
    broker.publish("ok", source="server", level="info")
    broker.publish("Warning: hmm", source="server")
    broker.publish("Error: boom", source="server")

    assert len(broker.backlog()) == 3
    assert len(broker.backlog("warn")) == 1
    assert broker.backlog("error")[0]["line"] == "Error: boom"


def test_level_counts():
    broker = LogBroker()
    broker.publish("a", source="server", level="info")
    broker.publish("b", source="server", level="warn")
    broker.publish("c", source="server", level="warn")
    assert broker.level_counts() == {"error": 0, "warn": 2, "info": 1}


# ---- logging ハンドラ ------------------------------------------------------


@pytest.mark.parametrize(
    ("levelno", "expected"),
    [
        (logging.DEBUG, "info"),
        (logging.INFO, "info"),
        (logging.WARNING, "warn"),
        (logging.ERROR, "error"),
        (logging.CRITICAL, "error"),
    ],
)
def test_app_logs_use_the_logging_level(levelno, expected):
    """本文の単語ではなく levelno で決まること。"""
    broker = LogBroker()
    published: list[tuple] = []
    broker.publish_threadsafe = lambda line, source="app", level=None: published.append(
        (line, source, level)
    )

    handler = BrokerLogHandler(broker)
    handler.setFormatter(logging.Formatter("%(message)s"))
    record = logging.LogRecord("app.test", levelno, __file__, 1, "何も起きていない", None, None)
    handler.emit(record)

    assert published[0][2] == expected


def test_app_log_wording_does_not_change_the_level():
    """INFO のまま「error」という語を含む行を error にしない。"""
    broker = LogBroker()
    broker.bind_loop = lambda: None
    handler = BrokerLogHandler(broker)
    handler.setFormatter(logging.Formatter("%(message)s"))

    captured: list[str | None] = []
    broker.publish_threadsafe = lambda line, source="app", level=None: captured.append(level)
    record = logging.LogRecord(
        "app.test", logging.INFO, __file__, 1, "0 errors found", None, None
    )
    handler.emit(record)
    assert captured[0] == "info"


# ---- API -------------------------------------------------------------------


async def test_logs_endpoint_returns_counts(client, app):
    app.state.broker.publish("ふつうの行", source="server", level="info")
    app.state.broker.publish("Warning: 危ない", source="server")

    body = (await client.get("/api/logs")).json()
    assert body["counts"]["warn"] >= 1
    assert body["total"] == len(body["lines"])


async def test_logs_endpoint_filters_by_level(client, app):
    app.state.broker.publish("ふつうの行", source="server", level="info")
    app.state.broker.publish("Error: だめ", source="server")

    body = (await client.get("/api/logs?level=error")).json()
    assert [r["line"] for r in body["lines"]] == ["Error: だめ"]
    # counts は絞り込みに関わらず全体を返す（バッジに出すため）
    assert body["counts"]["info"] >= 1


async def test_logs_endpoint_rejects_an_unknown_level(client):
    assert (await client.get("/api/logs?level=nope")).status_code == 422


# ---- 出力先の設定（issue #28） ---------------------------------------------


def _isolated_root():
    """root ロガーと app ロガーの状態を退避する。"""
    import contextlib
    import logging

    @contextlib.contextmanager
    def ctx():
        root = logging.getLogger()
        app_logger = logging.getLogger("app")
        saved_handlers = root.handlers[:]
        saved_root_level = root.level
        saved_app_level = app_logger.level
        root.handlers.clear()
        try:
            yield
        finally:
            root.handlers[:] = saved_handlers
            root.setLevel(saved_root_level)
            app_logger.setLevel(saved_app_level)

    return ctx()


def test_app_logs_reach_stderr():
    """これが無いと、管理ツールのログは画面のメモリ上にしか残らない。

    uvicorn は root にハンドラを付けず、app 側にハンドラがあるせいで
    Python の lastResort も発動しないので、ERROR すら stderr に出ない。
    """
    import io
    import logging

    from app.logstream import configure_logging

    buf = io.StringIO()
    with _isolated_root():
        configure_logging("INFO", stream=buf)
        logging.getLogger("app.restart").info("再起動シーケンス: service_stop")
        logging.getLogger("app.restart").error("シーケンスが異常終了")

    out = buf.getvalue()
    assert "service_stop" in out
    assert "異常終了" in out


def test_third_party_info_logs_are_not_included():
    """root ごと INFO にすると httpx や apscheduler で埋まる。"""
    import io
    import logging

    from app.logstream import configure_logging

    buf = io.StringIO()
    with _isolated_root():
        configure_logging("INFO", stream=buf)
        logging.getLogger("httpx").info("HTTP Request: GET /v1/api/info")
        logging.getLogger("httpx").warning("これは残す")

    out = buf.getvalue()
    assert "HTTP Request" not in out
    assert "これは残す" in out


def test_configure_logging_is_idempotent():
    """create_app が複数回呼ばれても、同じ行が二重に出ないこと。"""
    import io

    from app.logstream import configure_logging

    with _isolated_root():
        first = configure_logging("INFO", stream=io.StringIO())
        second = configure_logging("INFO", stream=io.StringIO())

    assert first is not None
    assert second is None


def test_log_level_can_be_turned_up():
    """原因を追うときに DEBUG まで下げられること。"""
    import io
    import logging

    from app.logstream import configure_logging

    buf = io.StringIO()
    with _isolated_root():
        configure_logging("DEBUG", stream=buf)
        logging.getLogger("app.services").debug("sudo -n systemctl is-active palworld.service")

    assert "is-active" in buf.getvalue()


def test_a_broken_log_level_does_not_silence_everything():
    """LOG_LEVEL の書き間違いでログが全部消えるのが一番困る。"""
    import io
    import logging

    from app.logstream import configure_logging

    buf = io.StringIO()
    with _isolated_root():
        configure_logging("VERBOSE", stream=buf)   # そんなレベルは無い
        logging.getLogger("app.restart").info("残ること")

    assert "残ること" in buf.getvalue()
