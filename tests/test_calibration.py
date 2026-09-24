"""实测振动校准逻辑测试: python -m unittest discover -s tests

核心断言:
- 由已知影响系数 α 合成两次试重读数, 标定结果必须还原 α;
- 单枚配重预测向量必须等于 V0 + α·m·R·e^{iθ};
- 预测按残余幅值升序, 推荐项为最小;
- 试重槽必须为空闲启用槽; 读数/质量非负有限、试重质量为正;
- 影响系数近零(两次读数几乎无差)时明确拒绝推荐。
"""
import math
import unittest

from app.balancer import run_plan
from app.calibration import calibrate_predictions


def _plan(slots=None, tubes=None, radius=100.0):
    slots = slots or [
        {"index": i, "angle": i * 30, "enabled": i != 6} for i in range(12)
    ]
    tubes = tubes or [
        {"id": 1, "mass": 50}, {"id": 2, "mass": 50},
        {"id": 3, "mass": 51}, {"id": 4, "mass": 49},
        {"id": 5, "mass": 30, "locked": True, "slot": 0},
        {"id": 6, "mass": 30},
    ]
    payload = {
        "slot_count": len(slots), "slots": slots, "tubes": tubes,
        "radius_mm": radius, "rpm": 4000, "restarts": 4, "chain_steps": 400,
    }
    result = run_plan(payload)
    return {"id": 1, "name": "t", "request": payload, "result": result}


def _free_slot(plan, want_angle=None):
    for s in plan["result"]["slot_state"]:
        if not s["enabled"] or s["tube_id"] is not None:
            continue
        if want_angle is None or abs(s["angle"] - want_angle) < 1e-9:
            return s["slot"], s["angle"]
    raise AssertionError("no free enabled slot")


def _cj(angle_deg):
    a = math.radians(angle_deg)
    return complex(math.cos(a), math.sin(a))


class TestCalibration(unittest.TestCase):
    def setUp(self):
        self.plan = _plan()
        self.trial_slot, self.trial_angle = _free_slot(self.plan)
        # 已知真实影响系数: 0.02 读数单位/(g·mm), 方向 120°
        self.alpha = 0.02 * _cj(120.0)
        self.v0 = complex(10.0, 0.0)

    def _readings(self, trial_mass=5.0, v0=None, alpha=None,
                  trial_angle=None):
        v0 = self.v0 if v0 is None else v0
        alpha = self.alpha if alpha is None else alpha
        ta = self.trial_angle if trial_angle is None else trial_angle
        v1 = v0 + alpha * (trial_mass * 100.0 * _cj(ta))
        return (abs(v0), math.degrees(math.atan2(v0.imag, v0.real)) % 360.0,
                abs(v1), math.degrees(math.atan2(v1.imag, v1.real)) % 360.0)

    def _run(self, masses=(3.0, 5.0, 8.0), **over):
        bm, bp, wm, wp = self._readings()
        kw = dict(
            baseline_mag=bm, baseline_phase_deg=bp,
            with_weight_mag=wm, with_weight_phase_deg=wp,
            trial_mass_g=5.0, trial_slot=self.trial_slot,
            available_masses=list(masses),
        )
        kw.update(over)
        return calibrate_predictions(self.plan, **kw)

    def test_recovers_known_influence_coefficient(self):
        r = self._run()
        a = r["influence_coefficient"]
        self.assertAlmostEqual(a["mag"], 0.02, places=12)
        self.assertAlmostEqual(a["phase_deg"], 120.0, places=9)
        # 试重质径积
        self.assertAlmostEqual(r["trial"]["mass_radius_g_mm"], 500.0, places=9)
        self.assertAlmostEqual(r["trial"]["angle_deg"], self.trial_angle, places=9)
        # 读数差
        dv = self.alpha * 500.0 * _cj(self.trial_angle)
        self.assertAlmostEqual(r["readings"]["delta"]["mag"], abs(dv), places=9)

    def test_prediction_vectors_match_formula(self):
        masses = [3.0, 5.0, 8.0]
        r = self._run(masses)
        free = {s["slot"]: s for s in self.plan["result"]["slot_state"]
                if s["enabled"] and s["tube_id"] is None}
        self.assertEqual(len(r["predictions"]), len(free) * len(masses))
        for row in r["predictions"]:
            theta = row["angle_deg"]
            vpred = self.v0 + self.alpha * (row["mass_g"] * 100.0 * _cj(theta))
            self.assertAlmostEqual(row["predicted"]["mag"], abs(vpred), places=8)
            self.assertAlmostEqual(row["predicted"]["x"], vpred.real, places=8)
            self.assertAlmostEqual(row["predicted"]["y"], vpred.imag, places=8)
            self.assertAlmostEqual(
                row["predicted"]["phase_deg"],
                math.degrees(math.atan2(vpred.imag, vpred.real)) % 360.0, places=8)
            imp = 10.0 - abs(vpred)
            self.assertAlmostEqual(row["improvement_mag"], imp, places=8)
            self.assertAlmostEqual(row["improvement_pct"], 10.0 * imp, places=8)

    def test_predictions_sorted_by_residual_and_best_is_minimum(self):
        r = self._run()
        mags = [p["predicted"]["mag"] for p in r["predictions"]]
        self.assertEqual(mags, sorted(mags))
        self.assertAlmostEqual(r["recommendation"]["predicted"]["mag"], min(mags),
                               places=12)

    def test_best_slot_nearest_ideal_and_full_cancel_improves_100pct(self):
        # 使理想配重恰好落在某个空闲启用槽(角度取该槽), 质量恰为给定值
        slot_idx, slot_ang = _free_slot(self.plan)
        m = 5.0
        # V0 与 α 满足 V0 = -α·m·R·e^{iθ}: 直接构造
        alpha = 0.02 * _cj(90.0)
        v0 = -alpha * (m * 100.0 * _cj(slot_ang))
        bm, bp, wm, wp = self._readings(trial_mass=m, v0=v0, alpha=alpha,
                                        trial_angle=slot_ang)
        r = calibrate_predictions(
            self.plan, baseline_mag=bm, baseline_phase_deg=bp,
            with_weight_mag=wm, with_weight_phase_deg=wp,
            trial_mass_g=m, trial_slot=slot_idx, available_masses=[m])
        best = r["recommendation"]
        self.assertEqual(best["slot"], slot_idx)
        self.assertLess(best["predicted"]["mag"], 1e-7)
        self.assertAlmostEqual(best["improvement_pct"], 100.0, places=6)

    def test_zero_baseline_improvement_pct_is_none(self):
        alpha = 0.02 * _cj(0.0)
        v0 = 0j
        slot_idx, slot_ang = _free_slot(self.plan)
        v1 = v0 + alpha * 500.0 * _cj(slot_ang)
        r = calibrate_predictions(
            self.plan, baseline_mag=0.0, baseline_phase_deg=0.0,
            with_weight_mag=abs(v1),
            with_weight_phase_deg=math.degrees(math.atan2(v1.imag, v1.real)) % 360,
            trial_mass_g=5.0, trial_slot=slot_idx, available_masses=[5.0])
        self.assertIsNone(r["predictions"][0]["improvement_pct"])
        self.assertAlmostEqual(r["ideal_single_weight"]["mass_g"], 0.0, places=9)

    def test_duplicate_masses_dedup_and_sort(self):
        r = self._run(masses=[8.0, 3.0, 3.0, 5.0])
        self.assertEqual(r["available_masses_g"], [3.0, 5.0, 8.0])

    def test_no_masses_returns_no_recommendation(self):
        r = self._run(masses=[])
        self.assertEqual(r["predictions"], [])
        self.assertIsNone(r["recommendation"])
        # 影响系数仍完成标定
        self.assertAlmostEqual(r["influence_coefficient"]["mag"], 0.02, places=12)

    def test_trial_slot_must_be_free_enabled(self):
        occ = next(s["slot"] for s in self.plan["result"]["slot_state"]
                   if s["tube_id"] is not None)
        with self.assertRaises(ValueError):
            calibrate_predictions(
                self.plan, baseline_mag=10, baseline_phase_deg=0,
                with_weight_mag=12, with_weight_phase_deg=30,
                trial_mass_g=5, trial_slot=occ, available_masses=[5])
        # 禁用槽 6
        with self.assertRaises(ValueError):
            calibrate_predictions(
                self.plan, baseline_mag=10, baseline_phase_deg=0,
                with_weight_mag=12, with_weight_phase_deg=30,
                trial_mass_g=5, trial_slot=6, available_masses=[5])
        # 不存在的槽
        with self.assertRaises(ValueError):
            calibrate_predictions(
                self.plan, baseline_mag=10, baseline_phase_deg=0,
                with_weight_mag=12, with_weight_phase_deg=30,
                trial_mass_g=5, trial_slot=99, available_masses=[5])

    def test_near_zero_influence_coefficient_rejected(self):
        with self.assertRaises(ValueError):
            calibrate_predictions(
                self.plan, baseline_mag=10, baseline_phase_deg=0,
                with_weight_mag=10, with_weight_phase_deg=0,
                trial_mass_g=5, trial_slot=self.trial_slot,
                available_masses=[5])
        # 极微小差别也拒绝
        with self.assertRaises(ValueError):
            calibrate_predictions(
                self.plan, baseline_mag=10, baseline_phase_deg=0,
                with_weight_mag=10.0, with_weight_phase_deg=1e-12,
                trial_mass_g=5, trial_slot=self.trial_slot,
                available_masses=[5])

    def test_nonfinite_and_negative_inputs_rejected(self):
        base = dict(
            plan=self.plan, baseline_mag=10, baseline_phase_deg=0,
            with_weight_mag=12, with_weight_phase_deg=30,
            trial_mass_g=5, trial_slot=self.trial_slot, available_masses=[5])
        for bad in (float("nan"), float("inf"), float("-inf")):
            kw = dict(base); kw["baseline_mag"] = bad
            with self.assertRaises(ValueError):
                calibrate_predictions(**kw)
        with self.assertRaises(ValueError):
            calibrate_predictions(**{**base, "baseline_mag": -0.1})
        with self.assertRaises(ValueError):
            calibrate_predictions(**{**base, "with_weight_phase_deg": -1.0})
        with self.assertRaises(ValueError):
            calibrate_predictions(**{**base, "trial_mass_g": 0.0})
        with self.assertRaises(ValueError):
            calibrate_predictions(**{**base, "available_masses": [0.0]})
        with self.assertRaises(ValueError):
            calibrate_predictions(**{**base, "available_masses": [-1.0]})

    def test_read_only_does_not_mutate_plan(self):
        before = self.plan["result"]["metrics"]["u1_mag_g_mm"]
        self._run()
        after = self.plan["result"]["metrics"]["u1_mag_g_mm"]
        self.assertEqual(before, after)


if __name__ == "__main__":
    unittest.main()
