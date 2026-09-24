"""已保存配平方案的实测振动校准(只读, 不改变求解与保存格式)。

单面动平衡的试重法(影响系数法)
--------------------------------
在同一转速下做两次测量:

- 试重前(原始状态): 振动复数读数 V0 = a0 · e^{iφ0}
- 在试重槽 θt 装入试重 W(g, 视为位于方案半径 R)后: V1 = a1 · e^{iφ1}

两次读数之差仅由试重的质径积 W·R·e^{iθt} 引起, 由此标定影响系数:

    α = (V1 - V0) / (W · R · e^{iθt})        [读数 / (g·mm)]

试重随后取下, 原始状态保持 V0。若在空闲启用槽 θ 放入单枚可用配重 m(g),
预测残余振动:

    V(θ, m) = V0 + α · m · R · e^{iθ}

本模块在方案的每一个空闲启用槽位上, 对每一种给定的可用配重质量逐一预测,
按预测残余幅值 |V| 从小到大排序, 给出推荐槽位/质量及相对原读数 |V0| 的
改善量(幅值下降绝对值与百分比)。

全部计算只读取已保存方案的槽位、落槽与半径, 不修改、不覆盖原方案。
"""
from __future__ import annotations

import math
from typing import Any, Optional

# 影响系数近零判据: 试重前后读数差 |ΔV| 小于该尺度即视为标定失败
# (试重太小、两次测量几乎无差别、读数单位不匹配等), 此时除法数值不稳定,
# 任何配重预测都不可信, 必须明确拒绝推荐。
ALPHA_EPS_SCALE = 1e-9


def _reading(mag: float, phase_deg: float, which: str) -> complex:
    """校验幅值/相位并构造复数读数。读数须有限且非负。"""
    m = float(mag)
    p = float(phase_deg)
    if not math.isfinite(m) or not math.isfinite(p):
        raise ValueError(f"{which}的振动幅值与相位必须为有限数值。")
    if m < 0.0:
        raise ValueError(f"{which}的振动幅值不能为负。")
    if p < 0.0:
        raise ValueError(f"{which}的振动相位不能为负(0–360°)。")
    return m * complex(math.cos(math.radians(p % 360.0)),
                       math.sin(math.radians(p % 360.0)))


def _plan_geometry(plan: dict[str, Any]) -> tuple[float, list[dict[str, Any]], set[int]]:
    """从已保存方案中提取 (半径 R, 槽位列表, 已占用槽位集合)。"""
    result = plan.get("result", {})
    config = result.get("config", {})
    radius = float(config.get("radius_mm", plan.get("request", {}).get("radius_mm", 0.0)))
    if not math.isfinite(radius) or radius <= 0.0:
        raise ValueError("方案缺少有效的转子半径, 无法进行振动校准。")

    slots = list(result.get("slot_state") or [])
    if not slots:
        # 兼容直接由请求槽位构造的情况
        slots = [
            {"slot": int(s["index"]),
             "angle": float(s.get("angle", 0.0)),
             "enabled": bool(s.get("enabled", True)),
             "tube_id": None}
            for s in plan.get("request", {}).get("slots", [])
        ]
    if not slots:
        raise ValueError("该方案没有槽位数据, 无法进行振动校准。")

    occupied = {
        int(p["slot"])
        for p in result.get("placements", [])
        if p.get("slot") is not None
    }
    return radius, slots, occupied


def calibrate_predictions(
    plan: dict[str, Any],
    *,
    baseline_mag: float,
    baseline_phase_deg: float,
    with_weight_mag: float,
    with_weight_phase_deg: float,
    trial_mass_g: float,
    trial_slot: int,
    available_masses: Optional[list[float]] = None,
) -> dict[str, Any]:
    """标定影响系数并逐空闲启用槽预测单枚配重后的残余振动。

    参数
    ----
    plan: GET /api/plan/{id} 返回的完整方案(只读)。
    baseline_mag / baseline_phase_deg: 试重前(原始)振动幅值与相位(°)。
    with_weight_mag / with_weight_phase_deg: 装入试重后振动幅值与相位(°)。
    trial_mass_g: 试重质量(g), 必须为正。
    trial_slot: 装入试重的槽位, 必须为方案中的空闲启用槽(试重视为已取下)。
    available_masses: 若干可用配重质量(g), 各自必须为正; 可为空。

    返回
    ----
    含两次读数、影响系数、连续最优配重及逐槽逐质量预测(按残余幅值排序)的字典。
    """
    v0 = _reading(baseline_mag, baseline_phase_deg, "试重前")
    v1 = _reading(with_weight_mag, with_weight_phase_deg, "装入试重后")

    radius, slots, occupied = _plan_geometry(plan)

    wt = float(trial_mass_g)
    if not math.isfinite(wt):
        raise ValueError("试重质量必须为有限数值。")
    if wt <= 0.0:
        raise ValueError("试重质量必须大于零。")

    slot_idx = int(trial_slot)
    slot_by_idx = {int(s["slot"]): s for s in slots}
    ts = slot_by_idx.get(slot_idx)
    if ts is None:
        raise ValueError(f"试重槽位 {slot_idx} 不在该方案的槽位中。")
    if not bool(ts.get("enabled", True)):
        raise ValueError(f"试重槽位 {slot_idx} 已被禁用, 试重必须装在启用槽位。")
    if slot_idx in occupied:
        raise ValueError(f"试重槽位 {slot_idx} 已被方案中的试管占用, 必须使用空闲槽位。")

    masses_in = list(available_masses or [])
    masses: list[float] = []
    seen: set[float] = set()
    for raw in masses_in:
        m = float(raw)
        if not math.isfinite(m):
            raise ValueError("可用配重质量必须为有限数值。")
        if m < 0.0:
            raise ValueError("可用配重质量不能为负。")
        if m <= 0.0:
            raise ValueError("可用配重质量必须大于零。")
        key = round(m, 12)
        if key in seen:
            continue
        seen.add(key)
        masses.append(m)
    masses.sort()

    trial_angle = float(ts["angle"]) % 360.0

    # 影响系数 α = (V1 - V0) / (W·R·e^{iθt})
    dv = v1 - v0
    eps_scale = ALPHA_EPS_SCALE * max(1.0, abs(v0), abs(v1))
    if abs(dv) <= eps_scale:
        raise ValueError(
            f"试重前后振动读数差的模 |ΔV|={abs(dv):.6g} 近似为零"
            f"(阈值 {eps_scale:g}), 影响系数近零, 无法可靠标定, 拒绝给出配重推荐。"
            "请检查试重是否过轻、两次读数是否几乎无差别(须同一转速), "
            "或试重槽/相位记录是否有误。"
        )
    trial_mr = wt * radius * complex(
        math.cos(math.radians(trial_angle)),
        math.sin(math.radians(trial_angle)),
    )
    alpha = dv / trial_mr

    def _vec_block(v: complex) -> dict[str, Any]:
        return {
            "mag": abs(v),
            "phase_deg": math.degrees(math.atan2(v.imag, v.real)) % 360.0,
            "x": v.real,
            "y": v.imag,
        }

    baseline_block = _vec_block(v0)
    baseline_mag0 = abs(v0)

    # 连续意义上的最优单枚配重: V0 + α·z = 0 → z = -V0/α
    ideal_mr = -v0 / alpha
    ideal_mass = abs(ideal_mr) / radius
    ideal_angle = math.degrees(math.atan2(ideal_mr.imag, ideal_mr.real)) % 360.0

    free_slots = [
        s for s in slots
        if bool(s.get("enabled", True)) and int(s["slot"]) not in occupied
    ]
    free_slots.sort(key=lambda s: int(s["slot"]))

    predictions: list[dict[str, Any]] = []
    for s in free_slots:
        theta = float(s["angle"])
        udir = complex(math.cos(math.radians(theta)), math.sin(math.radians(theta)))
        for m in masses:
            vpred = v0 + alpha * (m * radius * udir)
            resid_mag = abs(vpred)
            improvement = baseline_mag0 - resid_mag
            predictions.append({
                "slot": int(s["slot"]),
                "angle_deg": theta % 360.0,
                "mass_g": m,
                "predicted": _vec_block(vpred),
                "improvement_mag": improvement,
                "improvement_pct": (
                    100.0 * improvement / baseline_mag0 if baseline_mag0 > 0.0 else None
                ),
            })
    predictions.sort(key=lambda r: (
        r["predicted"]["mag"], r["slot"], round(r["mass_g"], 12)
    ))

    best = predictions[0] if predictions else None

    return {
        "method": (
            "单面试重法(影响系数法): α=(V1−V0)/(W·R·e^{iθt}); "
            "试重视为已取下, 单枚配重 m 于槽 θ 的预测残余 "
            "V=V0+α·m·R·e^{iθ}; 逐空闲启用槽 × 可用质量枚举并按残余幅值排序"
        ),
        "radius_mm": radius,
        "readings": {
            "before_trial": baseline_block,
            "with_trial": _vec_block(v1),
            "delta": _vec_block(dv),
        },
        "trial": {
            "slot": slot_idx,
            "angle_deg": trial_angle,
            "mass_g": wt,
            "mass_radius_g_mm": wt * radius,
        },
        "influence_coefficient": {
            "mag": abs(alpha),
            "phase_deg": math.degrees(math.atan2(alpha.imag, alpha.real)) % 360.0,
            "x": alpha.real,
            "y": alpha.imag,
            "reading_unit_per_g_mm": "读数单位/(g·mm)",
        },
        "ideal_single_weight": {
            # 连续最优质径方向(不受离散槽位/质量限制), 供与离散推荐对照
            "mass_g": ideal_mass,
            "angle_deg": ideal_angle,
        },
        "free_enabled_slots": [int(s["slot"]) for s in free_slots],
        "available_masses_g": masses,
        "predictions": predictions,
        "recommendation": best,
        "plan": {
            "id": plan.get("id"),
            "name": plan.get("name"),
            "created_at": plan.get("created_at"),
        },
    }
