"""管理ツール自身の稼働記録 (issue #41)。

停止→起動が数秒で連続していたら人の操作ではまず起きない形なので、
そうと分かる形で通知したい。その判定材料をプロセスをまたいで持ち回る。
"""

from __future__ import annotations

import json
import time

from app.runtime_state import RuntimeState


def make(tmp_path, **kwargs) -> RuntimeState:
    return RuntimeState(tmp_path / "runtime-state.json", **kwargs)


def test_first_ever_start_reports_nothing(tmp_path):
    """記録が無い初回起動を「異常」と言わないこと。"""
    info = make(tmp_path).mark_started()

    assert info.gap is None
    assert info.quick is False
    assert info.unclean is False
    assert info.suspect_external is False


def test_a_quick_restart_is_flagged(tmp_path):
    """数秒で戻ってきたら、外部要因を疑う印を付けること。"""
    state = make(tmp_path, quick_restart_sec=60.0)
    state.mark_started()
    state.mark_stopped(drain="idle")

    info = state.mark_started()

    assert info.gap is not None and info.gap < 5
    assert info.quick is True
    assert info.suspect_external is True
    assert info.drain == "idle"


def test_a_long_gap_is_not_flagged(tmp_path):
    """半日ぶりの起動は、ただの起動として扱うこと。"""
    state = make(tmp_path, quick_restart_sec=60.0)
    state.store_path.write_text(
        json.dumps({"started_at": time.time() - 90000, "stopped_at": time.time() - 43200}),
        encoding="utf-8",
    )

    info = state.mark_started()

    assert info.quick is False
    assert info.unclean is False
    assert info.gap is not None and info.gap > 60


def test_a_missing_stop_record_means_it_was_killed(tmp_path):
    """停止処理を通らずに消えた（SIGKILL / OOM / 電源断）ことを検知する。"""
    state = make(tmp_path)
    state.mark_started()  # 停止を記録しないまま……

    info = state.mark_started()

    assert info.unclean is True
    assert info.quick is False
    assert info.suspect_external is True


def test_the_drain_result_survives_the_restart(tmp_path):
    """前回シーケンスの途中で落ちたことを、次の起動に伝えること。"""
    state = make(tmp_path)
    state.mark_started()
    state.mark_stopped(drain="timeout")

    assert state.mark_started().drain == "timeout"


def test_a_broken_file_does_not_break_startup(tmp_path):
    """記録が壊れていても起動を止めないこと。通知が一段そっけなくなるだけ。"""
    state = make(tmp_path)
    state.store_path.write_text("{壊れた", encoding="utf-8")

    info = state.mark_started()

    assert info.gap is None
    assert json.loads(state.store_path.read_text(encoding="utf-8"))["started_at"] > 0


def test_without_a_store_path_it_just_does_nothing(tmp_path):
    state = RuntimeState(None)

    state.mark_stopped(drain="idle")
    info = state.mark_started()

    assert info.gap is None
    assert info.unclean is False


def test_a_backwards_clock_does_not_produce_a_negative_gap(tmp_path):
    """時計が巻き戻っても、負の経過時間を配らないこと。"""
    state = make(tmp_path)
    state.store_path.write_text(
        json.dumps({"started_at": time.time(), "stopped_at": time.time() + 600}),
        encoding="utf-8",
    )

    info = state.mark_started()

    assert info.gap == 0.0
    assert info.quick is True
