# -*- coding: utf-8 -*-
"""API 层契约测试：每日推进 / 遭遇处理 / 返程的响应结构与幂等语义。

前端只依赖响应 JSON 做状态流转，这里固定后端下发的契约：
- 推进一天：crisis 字段只承载地堡危机，探索遭遇随 session.expedition 下发
- 遭遇结算 / 返程：重复请求幂等回放，效果只施加一次
"""
import pytest
from fastapi.testclient import TestClient

from app.core.database import Base, engine
from app.main import app


class ScriptedRand:
    """可脚本化随机数：0.5 触发探索遭遇（<=0.85）但不触发地堡危机（>0.45）。"""

    def random(self):
        return 0.5

    def choice(self, seq):
        return seq[0]


@pytest.fixture()
def client(monkeypatch):
    # 引擎在 API 层自行实例化随机源，这里替换为确定性脚本
    monkeypatch.setattr("app.services.engine._rng", lambda: ScriptedRand())
    Base.metadata.drop_all(bind=engine)
    Base.metadata.create_all(bind=engine)
    with TestClient(app) as c:
        yield c
    Base.metadata.drop_all(bind=engine)


def _new_session(client):
    r = client.post("/api/sessions", json={"name": "API契约"})
    assert r.status_code == 201
    return r.json()


def _send_expedition(client, sid, members=1, food=10, water=10):
    s = client.get(f"/api/sessions/{sid}").json()
    ids = [r["id"] for r in s["residents"] if r["alive"]][:members]
    r = client.post(
        f"/api/sessions/{sid}/expedition/send",
        json={"member_ids": ids, "supplies": {"food": food, "water": water}},
    )
    assert r.status_code == 200
    return r.json()


def test_advance_returns_encounter_via_expedition_not_crisis(client):
    """探索队在外触发遭遇时：crisis 字段为 None，遭遇随 expedition 下发。

    前端据此只弹出探索遭遇弹层，不会把遭遇误当地堡危机（双层弹层卡死）。
    """
    s = _new_session(client)
    sid = s["id"]
    _send_expedition(client, sid)
    r = client.post(f"/api/sessions/{sid}/advance")
    assert r.status_code == 200
    body = r.json()
    assert body["crisis"] is None
    enc = body["session"]["expedition"]["pending_encounter"]
    assert enc is not None
    assert enc["event"] == "cache"  # ScriptedRand.choice 取首项
    assert enc["token"]
    # 推进按钮对应的危机弹层不应被激活
    assert body["session"]["pending_crisis"] is None


def test_encounter_resolve_then_duplicate_replays(client):
    """遭遇结算后重复提交同 token 同选项：200 幂等回放，战利品只累计一次。"""
    s = _new_session(client)
    sid = s["id"]
    _send_expedition(client, sid)
    adv = client.post(f"/api/sessions/{sid}/advance").json()
    token = adv["session"]["expedition"]["pending_encounter"]["token"]

    r1 = client.post(
        f"/api/sessions/{sid}/expedition/resolve",
        json={"choice_key": "search_carefully", "token": token},
    )
    assert r1.status_code == 200
    loot1 = r1.json()["expedition"]["loot"]
    assert loot1["food"] == 8 and loot1["water"] == 6

    # 重复提交：不回放错误，也不二次累计
    r2 = client.post(
        f"/api/sessions/{sid}/expedition/resolve",
        json={"choice_key": "search_carefully", "token": token},
    )
    assert r2.status_code == 200
    assert r2.json()["expedition"]["loot"] == loot1

    # 同一遭遇换选项重试：409 冲突
    r3 = client.post(
        f"/api/sessions/{sid}/expedition/resolve",
        json={"choice_key": "grab_quickly", "token": token},
    )
    assert r3.status_code == 409


def test_return_expedition_duplicate_replays(client):
    """重复返程（同 token）：200 幂等回放，战利品与归还物资只入库一次。"""
    s = _new_session(client)
    sid = s["id"]
    _send_expedition(client, sid)
    adv = client.post(f"/api/sessions/{sid}/advance").json()
    enc_token = adv["session"]["expedition"]["pending_encounter"]["token"]
    client.post(
        f"/api/sessions/{sid}/expedition/resolve",
        json={"choice_key": "search_carefully", "token": enc_token},
    )
    s = client.get(f"/api/sessions/{sid}").json()
    exp_token = s["expedition"]["token"]

    r1 = client.post(f"/api/sessions/{sid}/expedition/return", json={"token": exp_token})
    assert r1.status_code == 200
    body1 = r1.json()
    assert body1["expedition"] is None
    food_after = body1["resources"]["food"]

    # 重复返程：回放而非 400，资源不变
    r2 = client.post(f"/api/sessions/{sid}/expedition/return", json={"token": exp_token})
    assert r2.status_code == 200
    assert r2.json()["resources"]["food"] == food_after

    # 错误 token：400
    r3 = client.post(f"/api/sessions/{sid}/expedition/return", json={"token": "stale"})
    assert r3.status_code == 400


def test_management_rejected_during_pending_encounter(client):
    """遭遇挂起期间：建造/调岗与推进一样被 400 拒绝（前后端状态机一致）。"""
    s = _new_session(client)
    sid = s["id"]
    _send_expedition(client, sid)
    client.post(f"/api/sessions/{sid}/advance")
    s = client.get(f"/api/sessions/{sid}").json()
    assert s["expedition"]["pending_encounter"] is not None

    r_build = client.post(f"/api/sessions/{sid}/build", json={"category": "med"})
    assert r_build.status_code == 400
    rid = next(r["id"] for r in s["residents"] if not r["away"])
    r_job = client.post(f"/api/sessions/{sid}/resident/{rid}/job", json={"job": "farmer"})
    assert r_job.status_code == 400
    r_adv = client.post(f"/api/sessions/{sid}/advance")
    assert r_adv.status_code == 400


def test_old_archive_without_new_columns_still_served(client):
    """旧档案（无探索队相关列）迁移后可正常读取与推进。"""
    from sqlalchemy import text
    from app.core.database import SessionLocal
    from app.core.migration import ensure_schema

    s = _new_session(client)
    sid = s["id"]
    # 模拟旧版表：移除探索队相关列
    db = SessionLocal()
    try:
        db.execute(text("ALTER TABLE game_sessions RENAME TO game_sessions_old"))
        db.execute(text(
            "CREATE TABLE game_sessions ("
            "id INTEGER PRIMARY KEY, name VARCHAR(64), day INTEGER, target_day INTEGER, "
            "status VARCHAR(16), resources JSON, survivors INTEGER, pending_crisis JSON, "
            "last_resolution JSON, outcome JSON, score INTEGER, row_version INTEGER NOT NULL DEFAULT 1, "
            "created_at DATETIME, updated_at DATETIME)"
        ))
        db.execute(text(
            "INSERT INTO game_sessions SELECT id,name,day,target_day,status,resources,"
            "survivors,pending_crisis,last_resolution,outcome,score,row_version,"
            "created_at,updated_at FROM game_sessions_old"
        ))
        db.execute(text("DROP TABLE game_sessions_old"))
        db.commit()
    finally:
        db.close()

    ensure_schema(engine)
    # 旧档案可读取（expedition 为 None），可正常推进
    r = client.get(f"/api/sessions/{sid}")
    assert r.status_code == 200
    assert r.json()["expedition"] is None
    r = client.post(f"/api/sessions/{sid}/advance")
    assert r.status_code == 200
    assert r.json()["session"]["day"] == 2
