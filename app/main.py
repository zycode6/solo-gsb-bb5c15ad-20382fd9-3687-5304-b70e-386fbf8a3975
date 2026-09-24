"""离心机转子配平规划器 —— FastAPI 后端。"""
from __future__ import annotations

import json
import math
import os
import sqlite3
import time
from contextlib import contextmanager
from datetime import datetime, timezone
from typing import Annotated, Any, Optional

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from .balancer import run_plan
from .calibration import calibrate_predictions
from .compare import compare_plans
from .weighing import worst_case_u1

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
STATIC_DIR = os.path.join(BASE_DIR, "static")
DB_PATH = os.environ.get("BALANCER_DB", os.path.join(BASE_DIR, "data", "balancer.db"))


# ---------------------------------------------------------------------------
# 数据库
# ---------------------------------------------------------------------------
def init_db() -> None:
    os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
    with sqlite3.connect(DB_PATH) as conn:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS plans (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT NOT NULL,
                created_at TEXT NOT NULL,
                request_json TEXT NOT NULL,
                result_json TEXT NOT NULL,
                metrics_json TEXT NOT NULL,
                parent_id INTEGER
            )
            """
        )
        conn.execute("CREATE INDEX IF NOT EXISTS idx_plans_created ON plans(created_at)")


@contextmanager
def db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# 请求/响应模型
# ---------------------------------------------------------------------------
class SlotIn(BaseModel):
    index: int
    angle: Optional[float] = None
    enabled: bool = True


class TubeIn(BaseModel):
    id: int
    mass: float = Field(gt=0)
    locked: bool = False
    slot: Optional[int] = None


class PreviousPlacement(BaseModel):
    tube_id: int
    slot: int


class TubeErrorIn(BaseModel):
    tube_id: int
    # 非负称重质量误差上界(g); 不得超过该管标称质量, 由校核逻辑按方案校验
    mass_error_bound_g: float = Field(ge=0)


class WeighingCheckRequest(BaseModel):
    # 逐管非负质量误差上界; 未列出的试管按 0(标称质量无误差)处理
    tube_errors: list[TubeErrorIn] = Field(default_factory=list)
    # 一阶不平衡量限值(g·mm), 以最不利 |U₁| 判定
    u1_limit_g_mm: Optional[float] = Field(default=None, ge=0)


class VibrationCalibrationRequest(BaseModel):
    # 同一转速下试重前(原始)振动读数: 幅值(非负)与相位(0–360°)
    baseline_mag: float = Field(ge=0)
    baseline_phase_deg: float = Field(ge=0)
    # 装入试重后的振动读数: 幅值(非负)与相位(0–360°)
    with_weight_mag: float = Field(ge=0)
    with_weight_phase_deg: float = Field(ge=0)
    # 试重质量(g), 必须为正; 试重视为已取下
    trial_mass_g: float = Field(gt=0)
    # 装入试重的槽位: 必须为该方案的空闲启用槽, 由校准逻辑校验
    trial_slot: int
    # 若干可用配重质量(g), 各自必须为正; 可只做标定不给质量
    available_masses: list[Annotated[float, Field(gt=0)]] = Field(default_factory=list)


class PlanRequest(BaseModel):
    name: str = "未命名方案"
    slot_count: int = Field(default=12, ge=1, le=128)
    slots: list[SlotIn] = Field(default_factory=list)
    tubes: list[TubeIn] = Field(default_factory=list)
    radius_mm: float = Field(default=100.0, gt=0)
    rpm: float = Field(default=4000.0, ge=0)
    w1: float = 1.0
    w2: float = 0.000001
    move_weight: float = 0.0
    restarts: int = Field(default=10, ge=1, le=64)
    chain_steps: int = Field(default=2500, ge=100, le=60000)
    seed: int = 20260923
    previous: Optional[list[PreviousPlacement]] = None
    parent_id: Optional[int] = None
    save: bool = True


# ---------------------------------------------------------------------------
# 应用
# ---------------------------------------------------------------------------
app = FastAPI(title="离心机转子配平规划器", version="1.0.0")


@app.on_event("startup")
def _startup() -> None:
    init_db()


@app.get("/api/health")
def health() -> dict[str, str]:
    return {"status": "ok"}


@app.get("/api/config")
def get_config() -> dict[str, Any]:
    return {
        "defaults": {
            "slot_count": 12,
            "radius_mm": 100.0,
            "rpm": 4000.0,
            "w1": 1.0,
            "w2": 0.000001,
            "move_weight": 0.0,
            "restarts": 10,
            "chain_steps": 2500,
        },
        "physics": {
            "u1": "一阶不平衡量 ΣmR·e^(iθ), 单位 g·mm, 决定主轴离心合力",
            "u2": "二阶不平衡量 ΣmR·e^(i2θ), 单位 g·mm, 2 倍频残余",
            "force": "F = ω²·|U1|×1e-6, 单位 N",
            "moment": "M = F·R/1000, 单位 N·m",
            "grade": "等效偏心距评级: A≤1μm, B≤5μm, C≤20μm, D>20μm",
        },
    }


def _normalize_slots(req: PlanRequest) -> list[dict[str, Any]]:
    if req.slots:
        return [s.model_dump() for s in req.slots]
    return [
        {"index": i, "angle": 360.0 * i / req.slot_count, "enabled": True}
        for i in range(req.slot_count)
    ]


def _build_payload(req: PlanRequest) -> dict[str, Any]:
    return {
        "slot_count": req.slot_count,
        "slots": _normalize_slots(req),
        "tubes": [t.model_dump() for t in req.tubes],
        "radius_mm": req.radius_mm,
        "rpm": req.rpm,
        "w1": req.w1,
        "w2": req.w2,
        "move_weight": req.move_weight,
        "restarts": req.restarts,
        "chain_steps": req.chain_steps,
        "seed": req.seed,
        "previous": (
            [p.model_dump() for p in req.previous] if req.previous is not None else None
        ),
    }


@app.post("/api/plan")
def create_plan(req: PlanRequest) -> dict[str, Any]:
    payload = _build_payload(req)
    try:
        t0 = time.perf_counter()
        result = run_plan(payload)
        solve_ms = (time.perf_counter() - t0) * 1000.0
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    result["solve_time_ms"] = round(solve_ms, 1)
    result["incremental"] = req.previous is not None

    record: dict[str, Any] = {
        "name": req.name,
        "request": payload,
        "result": result,
    }
    if req.save:
        record["id"] = _save_plan(req.name, payload, result, req.parent_id)
    return record


def _save_plan(name: str, payload: dict[str, Any], result: dict[str, Any],
               parent_id: Optional[int]) -> int:
    with db() as conn:
        cur = conn.execute(
            """
            INSERT INTO plans (name, created_at, request_json, result_json,
                               metrics_json, parent_id)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (
                name,
                datetime.now(timezone.utc).isoformat(),
                json.dumps(payload, ensure_ascii=False),
                json.dumps(result, ensure_ascii=False),
                json.dumps(result["metrics"], ensure_ascii=False),
                parent_id,
            ),
        )
        return int(cur.lastrowid)


@app.get("/api/plans")
def list_plans(limit: int = 50) -> dict[str, Any]:
    limit = max(1, min(limit, 500))
    with db() as conn:
        rows = conn.execute(
            """
            SELECT id, name, created_at, metrics_json, parent_id
            FROM plans ORDER BY id DESC LIMIT ?
            """,
            (limit,),
        ).fetchall()
    items = []
    for row in rows:
        metrics = json.loads(row["metrics_json"])
        items.append({
            "id": row["id"],
            "name": row["name"],
            "created_at": row["created_at"],
            "parent_id": row["parent_id"],
            "u1_mag_g_mm": metrics["u1_mag_g_mm"],
            "u2_mag_g_mm": metrics["u2_mag_g_mm"],
            "grade": metrics["grade"],
            "centrifugal_force_n": metrics["centrifugal_force_n"],
        })
    return {"items": items}


def _load_plan_row(plan_id: int) -> sqlite3.Row:
    with db() as conn:
        row = conn.execute(
            "SELECT * FROM plans WHERE id = ?", (plan_id,)
        ).fetchone()
    if row is None:
        raise HTTPException(status_code=404, detail=f"方案 {plan_id} 不存在。")
    return row


def _row_to_plan(row: sqlite3.Row) -> dict[str, Any]:
    return {
        "id": row["id"],
        "name": row["name"],
        "created_at": row["created_at"],
        "parent_id": row["parent_id"],
        "request": json.loads(row["request_json"]),
        "result": json.loads(row["result_json"]),
    }


@app.get("/api/plan/{plan_id}")
def get_plan(plan_id: int) -> dict[str, Any]:
    return _row_to_plan(_load_plan_row(plan_id))


@app.get("/api/plans/compare")
def compare_saved_plans(a: int, b: int) -> dict[str, Any]:
    """比较两条已保存方案: 按试管编号合并摆放、并列指标差值、标注参数差异。"""
    if a == b:
        raise HTTPException(status_code=400, detail="请选择两条不同的方案进行比较。")
    # 两次独立取数, 保证两个 404 信息各自准确
    plan_a = _row_to_plan(_load_plan_row(a))
    plan_b = _row_to_plan(_load_plan_row(b))
    return compare_plans(plan_a, plan_b)


@app.delete("/api/plan/{plan_id}")
def delete_plan(plan_id: int) -> dict[str, Any]:
    with db() as conn:
        cur = conn.execute("DELETE FROM plans WHERE id = ?", (plan_id,))
    if cur.rowcount == 0:
        raise HTTPException(status_code=404, detail="方案不存在。")
    return {"deleted": plan_id}


@app.get("/api/plan/{plan_id}/export")
def export_plan(plan_id: int) -> JSONResponse:
    plan = get_plan(plan_id)
    fname = f"balance-plan-{plan_id}.json"
    return JSONResponse(
        plan,
        headers={"Content-Disposition": f'attachment; filename="{fname}"'},
    )


@app.post("/api/plan/{plan_id}/weighing-check")
def weighing_check(plan_id: int, req: WeighingCheckRequest) -> dict[str, Any]:
    """对已保存方案做称重误差校核(只读): 固定落槽与角度取自方案, 不覆盖原方案。

    各管实际质量在 [m-e, m+e] 内可独立正负变化, 服务端用二维 zonotope 顶点
    枚举精确求最不利 |U₁| 及一组对应实际质量(不枚举 2^n 组合, 也不把误差
    绝对值标量相加), 同时返回标称 |U₁| 作对照。
    """
    row = _load_plan_row(plan_id)
    plan = _row_to_plan(row)
    result = plan["result"]
    placements = result.get("placements", [])
    placed_ids = {
        int(p["tube_id"]) for p in placements if p.get("slot") is not None
    }

    errors: dict[int, float] = {}
    for item in req.tube_errors:
        if item.tube_id in errors:
            raise HTTPException(
                status_code=400,
                detail=f"试管 {item.tube_id} 的误差上界重复填写。",
            )
        if item.tube_id not in placed_ids:
            raise HTTPException(
                status_code=400,
                detail=f"试管 {item.tube_id} 不在方案 {plan_id} 的摆放明细中。",
            )
        errors[item.tube_id] = item.mass_error_bound_g

    radius_mm = float(result["config"]["radius_mm"])
    try:
        check = worst_case_u1(
            placements=placements,
            radius_mm=radius_mm,
            errors=errors,
            u1_limit_g_mm=req.u1_limit_g_mm,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    rpm = float(result["config"]["rpm"])
    omega = 2.0 * math.pi * rpm / 60.0
    check["rpm"] = rpm
    check["angular_velocity_rad_s"] = omega
    check["nominal"]["centrifugal_force_n"] = (
        omega ** 2 * check["nominal"]["u1_mag_g_mm"] * 1e-6
    )
    check["worst_case"]["centrifugal_force_n"] = (
        omega ** 2 * check["worst_case"]["u1_mag_g_mm"] * 1e-6
    )
    check["plan"] = {
        "id": plan["id"],
        "name": plan["name"],
        "created_at": plan["created_at"],
    }
    return check


@app.post("/api/plan/{plan_id}/vibration-calibration")
def vibration_calibration(
    plan_id: int, req: VibrationCalibrationRequest
) -> dict[str, Any]:
    """对已保存方案做实测振动校准(只读): 试重法标定影响系数并预测单枚配重。

    输入同一转速下试重前与装入试重后的振动幅值/相位、试重质量及其空槽,
    以复数振动差除以试重质径方向求影响系数; 随后在方案的每个空闲启用槽位
    上, 对每种给定可用配重质量预测装入单枚配重后的振动向量, 按残余幅值
    排序返回推荐。试重视为已取下; 不修改、不覆盖原方案。
    """
    plan = _row_to_plan(_load_plan_row(plan_id))
    # NaN/Infinity 无法由 JSON 约束直接以 400 中文提示拒绝, 这里显式兜底
    raw_vals = [
        req.baseline_mag, req.baseline_phase_deg,
        req.with_weight_mag, req.with_weight_phase_deg,
        req.trial_mass_g,
    ]
    if any(not math.isfinite(float(x)) for x in raw_vals):
        raise HTTPException(status_code=400, detail="读数与试重质量必须为有限数值。")
    if any(not math.isfinite(float(m)) for m in req.available_masses):
        raise HTTPException(status_code=400, detail="可用配重质量必须为有限数值。")
    try:
        return calibrate_predictions(
            plan,
            baseline_mag=req.baseline_mag,
            baseline_phase_deg=req.baseline_phase_deg,
            with_weight_mag=req.with_weight_mag,
            with_weight_phase_deg=req.with_weight_phase_deg,
            trial_mass_g=req.trial_mass_g,
            trial_slot=req.trial_slot,
            available_masses=list(req.available_masses),
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


# ---------------------------------------------------------------------------
# 静态前端
# ---------------------------------------------------------------------------
@app.get("/")
def index() -> FileResponse:
    return FileResponse(os.path.join(STATIC_DIR, "index.html"))


app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")
