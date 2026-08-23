"""Steam アップデートの検知（issue #30 / Phase 1）。

これまで検知は管理ツールの外の cron（`update-watch.sh`）が持っていた。動いては
いるが、**検知したことが管理ツールから見えない**。予告は検知時の1回だけで、
あとは `sleep 30m` の間ずっと無言のままサーバが落ちる。

ここでやるのは検知と表示だけで、**サーバには一切触らない**。適用と予約の
自動生成は Phase 2。この段階では cron と併存しても害が無い（どちらも
`check-update` を読むだけ）。

**黙って止まらないこと**をこのモジュールの第一の要件にしている。現行 cron の
いちばん痛い壊れ方は、`/tmp` にロックが残って以降の検知が全部素通りするのに
何も通知されない、というものだった。だから

- 判定できない出力は「更新なし」に丸めず、失敗として扱う（services.parse_check_update）
- 検知が続けて失敗したら Discord に流す
- 最後に確かめられた時刻を画面に出す

の3点で「気づけない沈黙」を潰している。
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Callable

from .notify import DiscordNotifier
from .services import UpdateCheck, supports_update

logger = logging.getLogger(__name__)


@dataclass
class UpdateState:
    """永続化する検知の状態。

    プロセスを再起動しても通知が重複しないよう、「この検知はもう流した」まで
    含めてファイルに残す。
    """

    available: bool = False
    checked_at: float | None = None
    detail: str = ""
    last_error: str | None = None
    # この検知について Discord に流した時刻。available が false に戻ると消す
    notified_at: float | None = None
    # 自動生成した予約の ID（Phase 2 で使う。いまは常に None）
    scheduled_id: str | None = None
    # 連続失敗の回数と、それを通知済みか
    fail_streak: int = 0
    failure_notified: bool = False


class UpdateWatcher:
    """`check-update` を定期的に叩いて、更新の有無を覚えておく。

    Monitor と同じ形の asyncio ループ。更新を扱えない構成（`SystemdService` など）
    では supported が false になり、ループも API も動かない。
    """

    def __init__(
        self,
        service: object,
        notifier: DiscordNotifier,
        *,
        interval: float = 600.0,
        store_path: Path | None = None,
        fail_alert_threshold: int = 3,
    ) -> None:
        self._service = service
        self._notifier = notifier
        self.interval = interval
        self.store_path = Path(store_path) if store_path else None
        self.fail_alert_threshold = max(1, fail_alert_threshold)

        self.state = UpdateState()
        self._task: asyncio.Task | None = None
        # 手動チェックと定期チェックが重ならないようにする
        self._lock = asyncio.Lock()
        self._checking = False
        # 再起動シーケンスなどが進行中かを尋ねるフック。
        # 停止/起動の最中に pwserver をもう1つ走らせない
        self._busy: Callable[[], bool] | None = None
        self._load()

    # ---- 能力 ----------------------------------------------------------

    @property
    def supported(self) -> bool:
        return supports_update(self._service)

    def set_busy_probe(self, probe: Callable[[], bool]) -> None:
        self._busy = probe

    # ---- 永続化 --------------------------------------------------------

    def _load(self) -> None:
        if not self.store_path or not self.store_path.is_file():
            return
        try:
            raw = json.loads(self.store_path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            logger.warning("アップデート検知の状態を読み込めません: %s", exc)
            return
        if not isinstance(raw, dict):
            return
        known = {f for f in UpdateState.__dataclass_fields__}
        try:
            self.state = UpdateState(**{k: v for k, v in raw.items() if k in known})
        except TypeError as exc:  # pragma: no cover - 型が壊れたファイル
            logger.warning("アップデート検知の状態が壊れています: %s", exc)

    def _save(self) -> None:
        if not self.store_path:
            return
        try:
            self.store_path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.store_path.with_suffix(".json.tmp")
            tmp.write_text(
                json.dumps(asdict(self.state), ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
            tmp.replace(self.store_path)
        except OSError as exc:
            logger.warning("アップデート検知の状態を保存できません: %s", exc)

    # ---- 検知 ----------------------------------------------------------

    async def check(self) -> UpdateState:
        """1回だけ確かめる。画面の「今すぐ確認」もここを通る。"""
        if not self.supported:
            return self.state
        async with self._lock:
            self._checking = True
            try:
                result = await self._service.check_update()  # type: ignore[attr-defined]
            except Exception as exc:  # pragma: no cover - 想定外は失敗として扱う
                logger.exception("アップデートの確認で想定外のエラー")
                result = UpdateCheck(False, False, "", str(exc))
            finally:
                self._checking = False
            await self._apply(result)
        return self.state

    async def _apply(self, result: UpdateCheck) -> None:
        st = self.state
        st.checked_at = time.time()

        if not result.ok:
            st.last_error = result.error or "check-update に失敗しました"
            st.fail_streak += 1
            logger.warning(
                "アップデートの確認に失敗しました（%d回連続）: %s",
                st.fail_streak, st.last_error,
            )
            # 「更新の有無」は前回の判断のまま据え置く。確かめられなかっただけで、
            # 出ている更新が消えたわけではない
            if st.fail_streak >= self.fail_alert_threshold and not st.failure_notified:
                st.failure_notified = True
                await self._notifier.send(
                    "アップデートの確認が続けて失敗しています",
                    f"{st.fail_streak}回連続で失敗しました。更新の検知が止まっている"
                    f"可能性があります。\n{st.last_error}",
                    "warn",
                )
            self._save()
            return

        recovered = st.failure_notified
        st.last_error = None
        st.fail_streak = 0
        st.failure_notified = False
        st.detail = result.detail

        if recovered:
            await self._notifier.send(
                "アップデートの確認が復帰しました",
                "check-update に再び成功しました。",
                "info",
            )

        was = st.available
        st.available = result.available

        if result.available and not was:
            st.notified_at = time.time()
            logger.info("Palworld のアップデートを検知しました")
            await self._notifier.send(
                "Palworld のアップデートを検知しました",
                # Phase 1 は検知だけ。適用が cron 任せのままであることを隠さない
                "適用はまだ管理ツールからは行いません（現行の update-watch.sh が"
                "30分後に適用します）。\n" + (result.detail[:500] or ""),
                "info",
            )
        elif was and not result.available:
            # 誰かが（いまは cron が）適用した。次の検知をまた通知できるように戻す
            logger.info("アップデートは適用済みになりました")
            st.notified_at = None
            st.scheduled_id = None

        self._save()

    # ---- ループ制御 ----------------------------------------------------

    async def _loop(self) -> None:
        while True:
            try:
                if self._busy and self._busy():
                    # 停止/起動シーケンスの最中。pwserver を二重に走らせない。
                    # 次の周回で確かめれば足りる
                    logger.debug("シーケンス進行中のためアップデート確認を見送ります")
                else:
                    await self.check()
            except asyncio.CancelledError:
                raise
            except Exception:  # pragma: no cover - 検知ループは絶対に止めない
                logger.exception("アップデート検知ループで想定外のエラー")
            await asyncio.sleep(self.interval)

    def start(self) -> None:
        if not self.supported:
            logger.info("この構成ではアップデートを扱えないため、検知は動かしません")
            return
        if self._task is None or self._task.done():
            self._task = asyncio.create_task(self._loop(), name="update-watch")

    async def stop(self) -> None:
        if self._task is None:
            return
        self._task.cancel()
        try:
            await self._task
        except asyncio.CancelledError:
            pass
        self._task = None

    # ---- 画面向け ------------------------------------------------------

    def as_dict(self) -> dict[str, Any]:
        st = self.state
        return {
            "supports_update": self.supported,
            "available": st.available,
            "checked_at": st.checked_at,
            "detail": st.detail,
            "last_error": st.last_error,
            "notified_at": st.notified_at,
            # Phase 2 で自動生成した予約を載せる
            "scheduled": None,
            "interval": self.interval,
            "checking": self._checking,
            "fail_streak": st.fail_streak,
        }
