"""ゲームサーバのプロセス制御。

実機は systemd ユニットを操作する。開発機には systemctl が無いので、
同じインタフェースでモックサーバを起動/停止するバックエンドを用意し、
どちらを使うかは設定（PAL_SERVICE_BACKEND）で切り替える。

実機との差分はこのモジュールに閉じ込める。
"""

from __future__ import annotations

import asyncio
import logging
import os
import re
import shutil
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

import httpx

logger = logging.getLogger(__name__)

# LinuxGSM は端末向けに色を付けて出力する。ログに残す前に落とす
_ANSI = re.compile(r"\x1B\[[0-9;]*[a-zA-Z]")

# `systemctl is-active` が非ゼロで返すが、コマンド自体は通っている状態。
# ユニットが動いていないだけなので、事前チェックとしては成功扱いにする
NOT_RUNNING_STATES = frozenset(
    {"inactive", "failed", "activating", "deactivating", "unknown"}
)

# 状態を変えない操作。画面のポーリングから繰り返し呼ばれるので、
# 成功したときのログは DEBUG に落とす
READ_ONLY_ACTIONS = frozenset({"is-active", "show"})


@dataclass
class CommandResult:
    ok: bool
    returncode: int
    stdout: str
    stderr: str
    simulated: bool = False

    def as_dict(self) -> dict:
        return {
            "ok": self.ok,
            "returncode": self.returncode,
            "stdout": self.stdout,
            "stderr": self.stderr,
            "simulated": self.simulated,
        }


class GameService(Protocol):
    """ゲームサーバのプロセスを起動/停止する口。"""

    async def start(self) -> CommandResult: ...
    async def stop(self) -> CommandResult: ...
    async def restart(self) -> CommandResult: ...

    async def preflight(self) -> CommandResult:
        """このサービスを操作できる状態か、実際に1回叩いて確かめる。

        再起動シーケンスがゲームサーバを落とす前に呼ぶ。落としてから
        「操作できません」と分かっても、もう起動し直せない。
        """
        ...

    async def is_active(self) -> bool | None:
        """稼働しているか。判定できない場合は None を返す。"""
        ...

    async def aclose(self) -> None: ...


class SystemdService:
    """systemctl でゲームサーバのユニットを操作する（本番）。

    管理ツールは root 以外のユーザ（本番は mntuser）で動くのが普通なので、
    そのままでは systemctl を実行できない。sudoers で必要な操作だけ許可し、
    use_sudo=True にして `sudo -n systemctl ...` として呼ぶ。

    `-n` は必須。付けないとパスワード待ちでプロセスが固まり、
    再起動シーケンスがタイムアウトするまで止まる。
    """

    def __init__(
        self,
        unit: str,
        *,
        dry_run: bool = False,
        timeout: float = 60.0,
        use_sudo: bool = False,
    ) -> None:
        self.unit = unit
        self.dry_run = dry_run
        self.timeout = timeout
        self.use_sudo = use_sudo

    @property
    def available(self) -> bool:
        if shutil.which("systemctl") is None:
            return False
        if self.use_sudo and shutil.which("sudo") is None:
            return False
        return True

    def _command(self, args: tuple[str, ...], *, privileged: bool = True) -> list[str]:
        # -n: パスワードを聞かれたら待たずに失敗する
        prefix = ["sudo", "-n"] if self.use_sudo and privileged else []
        return [*prefix, "systemctl", *args]

    async def _run(self, *args: str, privileged: bool = True) -> CommandResult:
        cmd = self._command(args, privileged=privileged)
        printable = " ".join(cmd)
        # 状態を変える操作は必ず記録する。is-active は画面のポーリングから
        # 何度も呼ばれるので DEBUG に落とし、journald を埋めないようにする
        level = logging.DEBUG if args and args[0] in READ_ONLY_ACTIONS else logging.INFO

        if self.dry_run or not self.available:
            reason = "dry_run" if self.dry_run else "systemctl が無い環境"
            logger.info("%s をスキップ (%s)", printable, reason)
            return CommandResult(
                ok=True,
                returncode=0,
                stdout=f"[simulated] {printable}",
                stderr="",
                simulated=True,
            )
        logger.log(level, "%s を実行します", printable)
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        try:
            out, err = await asyncio.wait_for(proc.communicate(), timeout=self.timeout)
        except asyncio.TimeoutError:
            proc.kill()
            logger.warning("%s が %.0f 秒でタイムアウトしました", printable, self.timeout)
            return CommandResult(False, -1, "", f"{printable} がタイムアウトしました")

        stderr = err.decode(errors="replace").strip()
        if proc.returncode == 0:
            logger.log(level, "%s が完了しました", printable)
        else:
            # 唯一ここでしか残らない。画面の 500 だけでは後から追えない
            logger.warning(
                "%s が失敗しました (rc=%s): %s",
                printable, proc.returncode, stderr or "(stderr なし)",
            )
        # sudoers の設定漏れは原因が分かりにくいので、そうと分かる形にする
        if proc.returncode != 0 and "password is required" in stderr:
            stderr = (
                f"{stderr}\n"
                "sudoers でこの操作が許可されていません。"
                "/etc/sudoers.d/dashboard-Pal に NOPASSWD で登録してください。"
            )
        return CommandResult(
            ok=proc.returncode == 0,
            returncode=proc.returncode or 0,
            stdout=out.decode(errors="replace").strip(),
            stderr=stderr,
        )

    async def restart(self) -> CommandResult:
        return await self._run("restart", self.unit)

    async def start(self) -> CommandResult:
        return await self._run("start", self.unit)

    async def stop(self) -> CommandResult:
        return await self._run("stop", self.unit)

    async def preflight(self) -> CommandResult:
        """サーバを落とす前に、このユニットを操作できるか確かめる。

        見たいのは2つ。systemd がこのユニットを知っているか（綴り間違いや、
        そもそもゲームサーバが systemd 管理下にない構成を弾く）と、
        sudo や polkit の段階で弾かれていないか。
        ユニットが止まっているだけなら成功として返す。
        """
        # `is-active` は存在しないユニットにも inactive を返すので、これだけでは
        # 「ユニットが無い」を見抜けない。落としてから起動できないと分かるのが
        # 最悪なので、先に存在を確かめる。show は読み取りだけで権限が要らないため
        # sudo を通さない（sudoers に show を足さずに済ませる）
        loaded = await self._run(
            "show", self.unit, "--property=LoadState", privileged=False
        )
        if not loaded.simulated:
            state = loaded.stdout.partition("=")[2].strip()
            if state and state != "loaded":
                return CommandResult(
                    False,
                    loaded.returncode,
                    loaded.stdout,
                    f"systemd が {self.unit} を認識していません（LoadState={state}）。"
                    "PAL_SERVICE_NAME が実在するユニット名か確認してください。",
                )

        result = await self._run("is-active", self.unit)
        if result.ok or result.simulated:
            return result
        if result.stdout.strip() in NOT_RUNNING_STATES:
            # systemctl まで届いている。あとは操作できるはず
            return CommandResult(True, result.returncode, result.stdout, "")
        return result

    async def is_active(self) -> bool | None:
        result = await self._run("is-active", self.unit)
        if result.simulated:
            # 実行できていないので「分からない」。稼働中と誤判定させない
            return None
        return result.stdout.strip() == "active"

    async def aclose(self) -> None:  # pragma: no cover - 保持リソース無し
        return None


class MockGameService:
    """モック Palworld サーバを起動/停止する（開発用）。

    systemctl の代わりにモックの制御エンドポイントを叩く。
    これがないと開発機では「停止」しても停止扱いにならず、
    設定ファイルの編集（停止中のみ許可）が一度も試せない。
    """

    def __init__(
        self,
        control_url: str,
        *,
        timeout: float = 5.0,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        self.control_url = control_url.rstrip("/")
        self._timeout = timeout
        self._client = client
        self._owns_client = client is None

    async def _get_client(self) -> httpx.AsyncClient:
        if self._client is None:
            self._client = httpx.AsyncClient(timeout=self._timeout)
        return self._client

    async def aclose(self) -> None:
        if self._client is not None and self._owns_client:
            await self._client.aclose()
            self._client = None

    async def _post(self, path: str) -> CommandResult:
        url = f"{self.control_url}/__mock__/{path}"
        try:
            client = await self._get_client()
            resp = await client.post(url)
        except httpx.HTTPError as exc:
            return CommandResult(False, -1, "", f"モックサーバに接続できません: {exc}")
        if resp.status_code >= 400:
            return CommandResult(False, resp.status_code, "", resp.text[:200])
        return CommandResult(True, 0, f"[mock] {path}", "")

    async def start(self) -> CommandResult:
        return await self._post("start")

    async def stop(self) -> CommandResult:
        return await self._post("stop")

    async def restart(self) -> CommandResult:
        return await self._post("restart")

    async def preflight(self) -> CommandResult:
        """モックの制御エンドポイントに届くか確かめる。"""
        try:
            client = await self._get_client()
            resp = await client.get(f"{self.control_url}/__mock__/status")
        except httpx.HTTPError as exc:
            return CommandResult(False, -1, "", f"モックサーバに接続できません: {exc}")
        if resp.status_code >= 400:
            return CommandResult(False, resp.status_code, "", resp.text[:200])
        return CommandResult(True, 0, "[mock] status", "")

    async def is_active(self) -> bool | None:
        try:
            client = await self._get_client()
            resp = await client.get(f"{self.control_url}/__mock__/status")
        except httpx.HTTPError:
            return None
        if resp.status_code >= 400:
            return None
        try:
            return bool(resp.json().get("running"))
        except ValueError:  # pragma: no cover
            return None


class LgsmService:
    """LinuxGSM の管理スクリプト経由でゲームサーバを操作する。

    LinuxGSM は tmux セッションの中でゲームを起動する、それ自体で完結した
    プロセス管理ツール（監視・アップデート・バックアップまで持つ）。
    上に systemd を重ねると「落ちてたら起こす」役目が二人になり、
    どちらかを無効化する調整が要る。ここでは重ねずに直接呼ぶ。

    管理ツールと LinuxGSM が同じユーザで動く前提なので、特権昇格は使わない。
    issue #28 は sudo の設定ミスで起きたので、その失敗クラスごと無くす。
    """

    def __init__(
        self,
        command: str,
        *,
        dry_run: bool = False,
        timeout: float = 300.0,
    ) -> None:
        self.command = command
        self.dry_run = dry_run
        self.timeout = timeout

    async def _run(self, action: str) -> CommandResult:
        printable = f"{self.command} {action}"
        if self.dry_run:
            logger.info("%s をスキップ (dry_run)", printable)
            return CommandResult(True, 0, f"[simulated] {printable}", "", simulated=True)

        logger.info("%s を実行します", printable)
        try:
            proc = await asyncio.create_subprocess_exec(
                self.command,
                action,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
        except OSError as exc:
            logger.warning("%s を起動できません: %s", printable, exc)
            return CommandResult(False, -1, "", f"{self.command} を実行できません: {exc}")

        try:
            out, err = await asyncio.wait_for(proc.communicate(), timeout=self.timeout)
        except asyncio.TimeoutError:
            proc.kill()
            logger.warning("%s が %.0f 秒でタイムアウトしました", printable, self.timeout)
            return CommandResult(False, -1, "", f"{printable} がタイムアウトしました")

        # LinuxGSM は端末向けに色を付けて出力するので、記録する前に落とす
        stdout = _ANSI.sub("", out.decode(errors="replace")).strip()
        stderr = _ANSI.sub("", err.decode(errors="replace")).strip()
        if proc.returncode == 0:
            logger.info("%s が完了しました", printable)
        else:
            logger.warning(
                "%s が失敗しました (rc=%s): %s",
                printable, proc.returncode, stderr or stdout or "(出力なし)",
            )
        return CommandResult(
            ok=proc.returncode == 0,
            returncode=proc.returncode or 0,
            stdout=stdout,
            # LinuxGSM は失敗の理由も stdout に書くことがあるので拾っておく
            stderr=stderr or (stdout if proc.returncode != 0 else ""),
        )

    async def start(self) -> CommandResult:
        return await self._run("start")

    async def stop(self) -> CommandResult:
        return await self._run("stop")

    async def restart(self) -> CommandResult:
        return await self._run("restart")

    async def preflight(self) -> CommandResult:
        """管理スクリプトを実行できるかだけ確かめる。

        LinuxGSM のサブコマンドは種類によって数十秒かかるうえ、
        `monitor` のように副作用のあるものもある。サーバを落とす前に
        知りたいのは「呼べるかどうか」なので、ここでは叩かない。
        """
        if self.dry_run:
            return CommandResult(True, 0, "[simulated] preflight", "", simulated=True)
        path = Path(self.command)
        if not self.command:
            return CommandResult(
                False, 1, "", "PAL_SERVICE_COMMAND が空です（LinuxGSM の管理スクリプトのパス）"
            )
        if not path.is_file():
            return CommandResult(
                False, 1, "", f"{self.command} がありません。PAL_SERVICE_COMMAND を確認してください"
            )
        if not os.access(path, os.X_OK):
            return CommandResult(
                False, 1, "", f"{self.command} に実行権限がありません（管理ツールと同じユーザで実行できること）"
            )
        return CommandResult(True, 0, f"{self.command} を実行できます", "")

    async def is_active(self) -> bool | None:
        """LinuxGSM には機械可読な状態問い合わせが無いので「分からない」を返す。

        `details` の出力を読む手もあるが、書式が版で変わるうえ遅い。
        稼働判定は REST API の到達性に委ねる（呼び出し側がそう作られている）。
        """
        return None

    async def aclose(self) -> None:
        return None


class SimulatedService:
    """何もせず成功を返す（テストと動作確認用）。

    systemd バックエンドはホストに systemctl があるかどうかで挙動が変わるため、
    再起動シーケンスそのものを検証したいテストでは結果が環境に左右される。
    ここを明示的に選べるようにして、その揺れを断つ。

    `dry_run` でも似たことはできるが、あちらはキックや BAN も止めてしまうので
    「サーバのプロセス制御だけを空回しにしたい」用途には使えない。
    """

    def __init__(self, unit: str = "") -> None:
        self.unit = unit
        self._running = True

    async def _result(self, action: str) -> CommandResult:
        return CommandResult(
            ok=True, returncode=0, stdout=f"[simulated] {action} {self.unit}".strip(),
            stderr="", simulated=True,
        )

    async def start(self) -> CommandResult:
        self._running = True
        return await self._result("start")

    async def stop(self) -> CommandResult:
        self._running = False
        return await self._result("stop")

    async def restart(self) -> CommandResult:
        self._running = True
        return await self._result("restart")

    async def preflight(self) -> CommandResult:
        return await self._result("preflight")

    async def is_active(self) -> bool | None:
        # 実行できていないので「分からない」を返す。
        # 稼働判定は REST API の到達性に委ねる
        return None

    async def aclose(self) -> None:
        return None


def build_service(
    backend: str,
    *,
    unit: str,
    dry_run: bool,
    mock_control_url: str,
    use_sudo: bool = False,
    command: str = "",
    timeout: float = 300.0,
) -> GameService:
    if backend == "mock":
        logger.info("ゲームサーバの制御にモックバックエンドを使います: %s", mock_control_url)
        return MockGameService(mock_control_url)
    if backend == "simulated":
        logger.info("ゲームサーバの制御を空回しします（simulated）")
        return SimulatedService(unit)
    if backend == "lgsm":
        logger.info("ゲームサーバの制御に LinuxGSM を使います: %s", command)
        return LgsmService(command, dry_run=dry_run, timeout=timeout)
    if backend and backend != "systemd":
        # 綴り間違いや、この版が知らないバックエンド名。黙って systemd に落ちると
        # 「設定したつもりの経路と違う」まま動いてしまう（切り戻し時に踏みやすい）
        logger.warning(
            "PAL_SERVICE_BACKEND=%r は知らない値です。systemd として扱います", backend
        )
    logger.info(
        "ゲームサーバの制御に systemd を使います: %s (sudo=%s)", unit, use_sudo
    )
    return SystemdService(unit, dry_run=dry_run, use_sudo=use_sudo, timeout=timeout)
