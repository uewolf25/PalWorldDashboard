"""Palworld 専用サーバ REST API のモック実装。

ゲームサーバが無い環境で管理ツールを動かす/テストするために、
http://<host>:8212/v1/api/* と同じ形のレスポンスを返す。

standalone 起動:
    uvicorn mock.mock_palworld:app --port 8212

テストからは `app`（ASGI）と `STATE` を直接触って検証する。
"""

from __future__ import annotations

import math
import os
import random
import secrets
import time
from dataclasses import dataclass, field
from typing import Any

from fastapi import Depends, FastAPI, HTTPException, status
from fastapi.security import HTTPBasic, HTTPBasicCredentials
from pydantic import BaseModel

ADMIN_USER = os.environ.get("MOCK_ADMIN_USER", "admin")
ADMIN_PASSWORD = os.environ.get("MOCK_ADMIN_PASSWORD", "mockpass")

_security = HTTPBasic(auto_error=False)

_NAMES = [
    "Aoi", "Haruto", "Sakura", "Ren", "Yuki", "Kaito", "Mio", "Sora",
    "Riku", "Hina", "Tsubasa", "Nagi",
]


def _new_player(idx: int) -> dict[str, Any]:
    return {
        "name": _NAMES[idx % len(_NAMES)],
        "accountName": f"{_NAMES[idx % len(_NAMES)].lower()}_acct",
        "playerId": f"{random.randint(0x10000000, 0x7FFFFFFF):08X}",
        "userId": f"steam_{random.randint(10**16, 10**17 - 1)}",
        "ip": f"192.168.1.{20 + idx}",
        "ping": round(random.uniform(8.0, 60.0), 1),
        "location_x": round(random.uniform(-150000, 150000), 1),
        "location_y": round(random.uniform(-150000, 150000), 1),
        "level": random.randint(1, 50),
        "building_count": random.randint(0, 300),
    }


@dataclass
class MockState:
    """モックサーバの可変状態。テストから直接書き換えて挙動を作る。"""

    # ゲームサーバのプロセスが動いているか。
    # False の間は /v1/api/* が 503 を返す（停止中のサーバに繋がらない状況の再現）。
    # __mock__/* の制御エンドポイントは停止中でも応答する（プロセス制御に相当する面のため）。
    running: bool = True
    started_at: float = field(default_factory=time.time)
    players: list[dict[str, Any]] = field(default_factory=list)
    banned: list[str] = field(default_factory=list)

    # 呼び出し履歴（テストのアサーション用）
    announcements: list[str] = field(default_factory=list)
    kicked: list[dict[str, str]] = field(default_factory=list)
    bans: list[dict[str, str]] = field(default_factory=list)
    unbans: list[str] = field(default_factory=list)
    saves: int = 0
    shutdowns: list[dict[str, Any]] = field(default_factory=list)
    stops: int = 0

    # Steam アップデートが出ている状態の再現（issue #30）。
    # 実機を待たずに検知バッジと更新カードを確かめられるようにする
    update_available: bool = False

    # 障害注入用フラグ
    fail_save: bool = False
    fail_all: bool = False
    fixed_fps: int | None = None
    settings_overrides: dict[str, Any] = field(default_factory=dict)

    def reset(self, player_count: int = 3) -> None:
        self.running = True
        self.started_at = time.time()
        self.players = [_new_player(i) for i in range(player_count)]
        self.banned = []
        self.announcements = []
        self.kicked = []
        self.bans = []
        self.unbans = []
        self.saves = 0
        self.shutdowns = []
        self.stops = 0
        self.update_available = False
        self.fail_save = False
        self.fail_all = False
        self.fixed_fps = None
        self.settings_overrides = {}

    def uptime(self) -> int:
        return int(time.time() - self.started_at)

    def fps(self) -> int:
        if self.fixed_fps is not None:
            return self.fixed_fps
        # 人数が増えるほど FPS が落ちる、ゆるい正弦波でゆらぎを付ける
        base = 60.0 - 2.2 * len(self.players)
        wobble = 3.0 * math.sin(time.time() / 7.0)
        return max(5, int(base + wobble))

    def find(self, userid: str) -> dict[str, Any] | None:
        for p in self.players:
            if p["userId"] == userid or p["playerId"] == userid:
                return p
        return None


STATE = MockState()
STATE.reset()

app = FastAPI(title="Mock Palworld REST API", docs_url="/docs")


def require_admin(creds: HTTPBasicCredentials | None = Depends(_security)) -> None:
    if creds is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Unauthorized",
            headers={"WWW-Authenticate": "Basic"},
        )
    ok_user = secrets.compare_digest(creds.username, ADMIN_USER)
    ok_pass = secrets.compare_digest(creds.password, ADMIN_PASSWORD)
    if not (ok_user and ok_pass):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Unauthorized",
            headers={"WWW-Authenticate": "Basic"},
        )


def _guard() -> None:
    """/v1/api/* の共通ガード。

    停止中は実機なら接続自体が拒否される。HTTP では表現できないので 503 を返す。
    管理ツール側は 4xx/5xx をまとめて PalApiError にするため、挙動は等価になる。
    """
    if not STATE.running:
        raise HTTPException(status_code=503, detail="mock: server is not running")
    if STATE.fail_all:
        raise HTTPException(status_code=500, detail="mock: induced failure")


class AnnounceBody(BaseModel):
    message: str


class UserBody(BaseModel):
    userid: str
    message: str = ""


class UnbanBody(BaseModel):
    userid: str


class ShutdownBody(BaseModel):
    waittime: int = 30
    message: str = ""


@app.get("/v1/api/info")
def info(_: None = Depends(require_admin)) -> dict[str, Any]:
    _guard()
    return {
        "version": "v0.6.2.0",
        "servername": "Mock Palworld Server",
        "description": "local mock for dashboard-Pal",
        "worldguid": "00000000000000000000000000000001",
    }


@app.get("/v1/api/metrics")
def metrics(_: None = Depends(require_admin)) -> dict[str, Any]:
    _guard()
    fps = STATE.fps()
    return {
        "serverfps": fps,
        "currentplayernum": len(STATE.players),
        "serverframetime": round(1000.0 / max(fps, 1), 2),
        "maxplayernum": 32,
        "uptime": STATE.uptime(),
    }


@app.get("/v1/api/players")
def players(_: None = Depends(require_admin)) -> dict[str, Any]:
    _guard()
    # 毎回わずかに動かして「生きている」感を出す
    for p in STATE.players:
        p["ping"] = round(max(1.0, p["ping"] + random.uniform(-3, 3)), 1)
        p["location_x"] = round(p["location_x"] + random.uniform(-800, 800), 1)
        p["location_y"] = round(p["location_y"] + random.uniform(-800, 800), 1)
    return {"players": STATE.players}


@app.get("/v1/api/settings")
def settings(_: None = Depends(require_admin)) -> dict[str, Any]:
    _guard()
    base = {
        "Difficulty": "None",
        "DayTimeSpeedRate": 1.0,
        "NightTimeSpeedRate": 1.0,
        "ExpRate": 1.0,
        "PalCaptureRate": 1.0,
        "PalSpawnNumRate": 1.0,
        "ServerPlayerMaxNum": 32,
        "ServerName": "Mock Palworld Server",
        "ServerDescription": "local mock",
        "PublicPort": 8211,
        "PublicIP": "127.0.0.1",
        "RESTAPIEnabled": True,
        "RESTAPIPort": 8212,
        "bIsPvP": False,
        "bEnablePlayerToPlayerDamage": False,
    }
    base.update(STATE.settings_overrides)
    return base


@app.post("/v1/api/announce")
def announce(body: AnnounceBody, _: None = Depends(require_admin)) -> dict[str, str]:
    _guard()
    STATE.announcements.append(body.message)
    return {"result": "ok"}


@app.post("/v1/api/kick")
def kick(body: UserBody, _: None = Depends(require_admin)) -> dict[str, str]:
    _guard()
    player = STATE.find(body.userid)
    if player is None:
        raise HTTPException(status_code=400, detail="player not found")
    STATE.players.remove(player)
    STATE.kicked.append({"userid": body.userid, "message": body.message})
    return {"result": "ok"}


@app.post("/v1/api/ban")
def ban(body: UserBody, _: None = Depends(require_admin)) -> dict[str, str]:
    _guard()
    player = STATE.find(body.userid)
    if player is not None:
        STATE.players.remove(player)
    STATE.banned.append(body.userid)
    STATE.bans.append({"userid": body.userid, "message": body.message})
    return {"result": "ok"}


@app.post("/v1/api/unban")
def unban(body: UnbanBody, _: None = Depends(require_admin)) -> dict[str, str]:
    _guard()
    if body.userid in STATE.banned:
        STATE.banned.remove(body.userid)
    STATE.unbans.append(body.userid)
    return {"result": "ok"}


@app.post("/v1/api/save")
def save(_: None = Depends(require_admin)) -> dict[str, str]:
    _guard()
    if STATE.fail_save:
        raise HTTPException(status_code=500, detail="mock: world save failed")
    STATE.saves += 1
    return {"result": "ok"}


@app.post("/v1/api/shutdown")
def shutdown(body: ShutdownBody, _: None = Depends(require_admin)) -> dict[str, str]:
    _guard()
    STATE.shutdowns.append({"waittime": body.waittime, "message": body.message})
    STATE.players = []
    # 実機と同じく、この API はサーバプロセスを落とす
    STATE.running = False
    return {"result": "ok"}


@app.post("/v1/api/stop")
def stop(_: None = Depends(require_admin)) -> dict[str, str]:
    _guard()
    STATE.stops += 1
    STATE.players = []
    STATE.running = False
    return {"result": "ok"}


# --- モック専用の操作エンドポイント（実機には無い） -----------------------
# standalone で動かして UI を触るときに、状況を作るために使う。
#
# 停止中でも応答する。実機では LinuxGSM がこの役割（プロセスの起動/停止）を持ち、
# ゲームサーバが落ちていても管理スクリプトは動くのと同じ関係にしてある。


@app.get("/__mock__/status")
def mock_status() -> dict[str, Any]:
    return {"running": STATE.running, "players": len(STATE.players)}


@app.post("/__mock__/start")
def mock_start() -> dict[str, Any]:
    """停止中のサーバを起動する（pwserver start 相当）。"""
    if not STATE.running:
        STATE.running = True
        STATE.started_at = time.time()
    return {"running": STATE.running}


@app.post("/__mock__/stop")
def mock_stop() -> dict[str, Any]:
    """サーバを停止する（pwserver stop 相当）。"""
    STATE.running = False
    STATE.players = []
    return {"running": STATE.running}


@app.post("/__mock__/restart")
def mock_restart() -> dict[str, Any]:
    """停止して起動し直す（pwserver restart 相当）。"""
    STATE.running = True
    STATE.started_at = time.time()
    STATE.players = [_new_player(i) for i in range(3)]
    return {"running": STATE.running}


@app.get("/__mock__/check-update")
def mock_check_update() -> dict[str, Any]:
    """pwserver check-update 相当。サーバの稼働状態とは無関係に答える。"""
    return {
        "available": STATE.update_available,
        "detail": "[mock] Update available" if STATE.update_available
                  else "[mock] No update available",
    }


@app.post("/__mock__/update-available")
def mock_set_update_available(value: bool = True) -> dict[str, Any]:
    STATE.update_available = value
    return {"available": STATE.update_available}


@app.post("/__mock__/reset")
def mock_reset(player_count: int = 3) -> dict[str, Any]:
    STATE.reset(player_count)
    return {"players": len(STATE.players)}


@app.post("/__mock__/join")
def mock_join() -> dict[str, Any]:
    STATE.players.append(_new_player(len(STATE.players) + random.randint(0, 11)))
    return {"players": len(STATE.players)}


@app.post("/__mock__/leave")
def mock_leave() -> dict[str, Any]:
    if STATE.players:
        STATE.players.pop()
    return {"players": len(STATE.players)}


@app.post("/__mock__/fail")
def mock_fail(fail_all: bool = False, fail_save: bool = False) -> dict[str, Any]:
    STATE.fail_all = fail_all
    STATE.fail_save = fail_save
    return {"fail_all": STATE.fail_all, "fail_save": STATE.fail_save}


@app.post("/__mock__/fps")
def mock_fps(value: int | None = None) -> dict[str, Any]:
    STATE.fixed_fps = value
    return {"fixed_fps": STATE.fixed_fps}


@app.post("/__mock__/settings")
def mock_settings(overrides: dict[str, Any]) -> dict[str, Any]:
    """/v1/api/settings が返す内容に項目を足す/上書きする。

    「Palworld のアップデートで新しいプロパティが増えた」状況を再現して、
    管理ツール側の項目発見を確認するために使う。
    """
    STATE.settings_overrides.update(overrides)
    return STATE.settings_overrides
