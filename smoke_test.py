"""无第三方依赖环境下, 用最小桩模块驱动 app/main.py 的端点逻辑做端到端冒烟测试。"""
import sys, types, json, os, tempfile, asyncio

# ---- 桩: pydantic ----
pyd = types.ModuleType("pydantic")
class FieldInfo:
    def __init__(self, default=None, **kw):
        self.default = default; self.kw = kw
def Field(default=None, **kw):
    if default is None and "default_factory" in kw:
        return FieldInfo(default=kw["default_factory"](), **kw)
    return FieldInfo(default=default, **kw)
class BaseModel:
    def __init__(self, **data):
        cls = type(self)
        for name, f in cls.model_fields().items():
            val = data.get(name, f.default if not callable(getattr(f.default, "__call__", None)) else None)
            if val is None and isinstance(f.default, (list,)):
                val = list(f.default)
            setattr(self, name, val)
        self.__post_init_check__(data)
    def __post_init_check__(self, data):
        for k in data:
            if not hasattr(type(self), "_ann"): pass
    def model_dump(self):
        return {k: getattr(self, k) for k in type(self).model_fields()}
    @classmethod
    def model_fields(cls):
        return getattr(cls, "_fields", {})
    @classmethod
    def parse(cls, data):
        # 简易类型转换(数字/布尔/嵌套)
        return cls(**data)
def model_validate(cls, data):
    return _build(cls, data)
def _build(cls, data):
    obj = cls.__new__(cls)
    fields = cls.model_fields()
    for name, f in fields.items():
        val = data.get(name, _factory_default(f))
        ann = cls._annotations.get(name)
        val = _convert(val, ann)
        setattr(obj, name, val)
    return obj
def _factory_default(f):
    return f.default
def _convert(val, ann):
    if val is None: return None
    import typing

pyd.BaseModel = BaseModel
pyd.Field = Field

# 更健壮:重写 BaseModel 以处理嵌套 list[Model] 与 Optional
class BM:
    def __init__(self, **kw):
        for name, f in type(self).model_fields().items():
            setattr(self, name, kw.get(name, _default(f)))
        self._validate(kw)
    def _validate(self, kw):
        allowed = set(type(self).model_fields())
        for k in kw:
            if k not in allowed: raise ValueError(f"unexpected field {k}")
    def model_dump(self):
        out = {}
        for k, f in type(self).model_fields().items():
            v = getattr(self, k)
            if isinstance(v, list):
                out[k] = [x.model_dump() if hasattr(x, "model_dump") else x for x in v]
            elif hasattr(v, "model_dump"):
                out[k] = v.model_dump()
            else:
                out[k] = v
        return out
    @classmethod
    def model_fields(cls):
        return cls._fields
    @classmethod
    def validate(cls, data):
        return _build_model(cls, data)
def _default(f):
    d = f.default
    return list(d) if isinstance(d, list) else d
def _build_model(cls, data):
    obj = cls.__new__(cls)
    for name, f in cls.model_fields().items():
        raw = data.get(name, _default(f))
        setattr(obj, name, _coerce(raw, cls._annotations.get(name)))
    return obj
def _coerce(raw, ann):
    if raw is None: return None
    # 解析泛型
    origin = getattr(ann, "__origin__", None)
    if origin is list:
        inner = ann.__args__[0]
        if isinstance(inner, type) and issubclass(inner, BM):
            return [_build_model(inner, x) for x in raw]
        return list(raw)
    if isinstance(ann, type) and issubclass(ann, BM):
        return _build_model(ann, raw)
    return raw
pyd.BaseModel = BM

# ---- 桩: fastapi ----
fa = types.ModuleType("fastapi")
fa.__path__ = []
class FileResponse:
    def __init__(self, path): self.path = path
class JSONResponse:
    def __init__(self, content, headers=None):
        self.content = content; self.headers = headers or {}
far = types.ModuleType("fastapi.responses")
far.FileResponse = FileResponse
far.JSONResponse = JSONResponse
fa.responses = far
class HTTPException(Exception):
    def __init__(self, status_code=400, detail=""):
        self.status_code=status_code; self.detail=detail
class _RouterLike:
    def __init__(self): self.routes=[]
    def add(self, method, path, fn): self.routes.append((method, path, fn))
    def get(self,p,*a,**k):
        def d(f): self.add("GET",p,f); return f
        return d
    def post(self,p,*a,**k):
        def d(f): self.add("POST",p,f); return f
        return d
    def delete(self,p,*a,**k):
        def d(f): self.add("DELETE",p,f); return f
        return d
class FastAPI(_RouterLike):
    def __init__(self, title="", version=""):
        super().__init__(); self.title=title; self.startup=[]
    def on_event(self, ev):
        def d(f):
            if ev=="startup": self.startup.append(f)
            return f
        return d
    def mount(self,*a,**k): pass
fa.FastAPI=FastAPI; fa.HTTPException=HTTPException

st=types.ModuleType("fastapi.staticfiles")
class StaticFiles:
    def __init__(self,directory=None): self.directory=directory
st.StaticFiles=StaticFiles

sys.modules["pydantic"]=pyd
sys.modules["fastapi"]=fa
sys.modules["fastapi.responses"]=far
sys.modules["fastapi.staticfiles"]=st

# ---- 导入被测应用 ----
tmpdir = tempfile.mkdtemp()
os.environ["BALANCER_DB"] = os.path.join(tmpdir, "test.db")
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from app import main  # noqa
from app.main import app

for f in app.startup: f()

def find(method, path):
    import re
    path = path.split("?", 1)[0]
    for m,p,fn in app.routes:
        if m!=method: continue
        # 把 {param} 转为单段正则([^/]+)
        pat = "^" + re.sub(r"\{[^}]+\}", r"([^/]+)", p) + "$"
        mmx = re.match(pat, path)
        if mmx:
            args = []
            for a in mmx.groups():
                args.append(int(a) if a.isdigit() else a)
            return fn, tuple(args)
    return None

def call(method, path, body=None):
    raw_query = path.split("?", 1)[1] if "?" in path else ""
    r = find(method,path)
    if isinstance(r, tuple): fn, args = r
    else: fn, args = r, ()
    if raw_query:
        import urllib.parse
        qargs = {}
        for k, v in urllib.parse.parse_qsl(raw_query):
            qargs[k] = int(v) if v.lstrip("-").isdigit() else v
        args = tuple(qargs.get(p) for p in fn.__code__.co_varnames[:fn.__code__.co_argcount])
    if body is not None:
        model_cls = main.PlanRequest
        req = _build_concrete(model_cls, body)
        return fn(req)
    return fn(*args)

def _build_concrete(cls, data):
    """根据 main.py 中具体 Pydantic 模型做嵌套转换。"""
    def slot(d):
        o = types.SimpleNamespace(**d)
        o.model_dump = lambda d=d: dict(d)
        return o
    def tube(d):
        o = types.SimpleNamespace(
            id=int(d["id"]), mass=float(d["mass"]),
            locked=bool(d.get("locked", False)),
            slot=(int(d["slot"]) if d.get("slot") is not None else None))
        o.model_dump = lambda: {"id": o.id, "mass": o.mass, "locked": o.locked, "slot": o.slot}
        return o
    def prev(d):
        o = types.SimpleNamespace(tube_id=int(d["tube_id"]), slot=int(d["slot"]))
        o.model_dump = lambda: {"tube_id": o.tube_id, "slot": o.slot}
        return o
    defaults = {
        "name":"未命名方案","slot_count":12,"slots":[],
        "radius_mm":100.0,"rpm":4000.0,"w1":1.0,"w2":0.000001,
        "move_weight":0.0,"restarts":10,"chain_steps":2500,
        "seed":20260923,"previous":None,"parent_id":None,"save":True,
    }
    merged = {**defaults, **data}
    return types.SimpleNamespace(
        name=merged["name"],
        slot_count=int(merged["slot_count"]),
        slots=[slot(d) for d in merged["slots"]],
        tubes=[tube(d) for d in merged["tubes"]],
        radius_mm=float(merged["radius_mm"]), rpm=float(merged["rpm"]),
        w1=float(merged["w1"]), w2=float(merged["w2"]),
        move_weight=float(merged["move_weight"]),
        restarts=int(merged["restarts"]), chain_steps=int(merged["chain_steps"]),
        seed=int(merged["seed"]),
        previous=([prev(d) for d in merged["previous"]] if merged["previous"] is not None else None),
        parent_id=merged["parent_id"], save=bool(merged.get("save", True)),
    )

# ---- 用例 ----
slots=[{"index":i,"angle":i*30,"enabled": i!=6} for i in range(12)]
req_body={"name":"冒烟方案","slot_count":12,"slots":slots,
          "tubes":[{"id":1,"mass":50},{"id":2,"mass":50},{"id":3,"mass":51},
                   {"id":4,"mass":49},{"id":5,"mass":30,"locked":True,"slot":0},
                   {"id":6,"mass":30}],
          "radius_mm":100,"rpm":4000,"restarts":4,"chain_steps":800}
rec = call("POST","/api/plan",req_body)
assert rec["id"]==1, rec.keys()
m=rec["result"]["metrics"]
print("plan#1 U1=%.2f U2=%.2f grade=%s F=%.3f M=%.4f steps=%d" % (
    m["u1_mag_g_mm"],m["u2_mag_g_mm"],m["grade"],
    m["centrifugal_force_n"],m["resultant_moment_n_m"],len(rec["result"]["steps"])))
# locked respected
assert next(p for p in rec["result"]["placements"] if p["tube_id"]==5)["slot"]==0
# disabled respected
assert all(p["slot"]!=6 for p in rec["result"]["placements"])

# 400 validation
bad=dict(req_body); bad["tubes"]=bad["tubes"]*4
try:
    call("POST","/api/plan",bad); raise SystemExit("should 400")
except main.HTTPException as e:
    assert e.status_code==400; print("validation 400 OK:",e.detail)

# 增量重排
inc=dict(req_body); inc["name"]="增量方案"
inc["tubes"]=[dict(t) for t in req_body["tubes"]]+[{"id":7,"mass":20}]
inc["previous"]=[{"tube_id":p["tube_id"],"slot":p["slot"]} for p in rec["result"]["placements"]]
inc["parent_id"]=1
rec2=call("POST","/api/plan",inc)
print("plan#2 incremental U1=%.2f moved=%s" % (
    rec2["result"]["metrics"]["u1_mag_g_mm"], rec2["result"]["moved_tube_ids"]))

# 方案比较
cmp12 = call("GET", "/api/plans/compare?a=1&b=2")
assert cmp12["plan_a"]["id"] == 1 and cmp12["plan_b"]["id"] == 2
# plan#2 比 plan#1 多一支试管 -> 有 1 条 added, 无 removed
assert cmp12["placement_counts"]["added"] == 1, cmp12["placement_counts"]
added = [r for r in cmp12["placements"] if r["status"] == "added"]
assert added and added[0]["tube_id"] == 7
# 试管清单不同 -> 非同条件, 必须在参数差异中体现
assert cmp12["same_conditions"] is False
assert "tubes" in cmp12["differing_groups"]
# 指标并列含一阶/二阶模值与角度, 差值为 B-A
um = {x["key"]: x for x in cmp12["metrics"]}
assert set(("u1_mag_g_mm", "u2_mag_g_mm", "u1_angle_deg", "u2_angle_deg")) <= set(um)
assert abs(um["u1_mag_g_mm"]["delta_b_minus_a"]
           - (rec2["result"]["metrics"]["u1_mag_g_mm"]
              - rec["result"]["metrics"]["u1_mag_g_mm"])) < 1e-6
print("compare added/diff-condition OK")

# 同条件比较: 用相同输入再求解保存一份(plan#3)
same = dict(req_body); same["name"] = "同条件副本"
rec3 = call("POST", "/api/plan", same)
cmp13 = call("GET", "/api/plans/compare?a=1&b=3")
assert cmp13["same_conditions"] is True, cmp13["differing_groups"]
assert not cmp13["input_diff"]["rotor"]
assert not cmp13["input_diff"]["solver"]
assert not cmp13["input_diff"]["tubes"]
assert all(r["status"] == "unchanged" for r in cmp13["placements"])
assert all(abs(r["delta_b_minus_a"]) < 1e-9
           for r in cmp13["metrics"] if not r["is_angle"])
print("compare same-conditions OK")

# a==b 拒绝; 不存在 404
try:
    call("GET", "/api/plans/compare?a=1&b=1"); raise SystemExit("should 400")
except main.HTTPException as e:
    assert e.status_code == 400
try:
    call("GET", "/api/plans/compare?a=1&b=999"); raise SystemExit("should 404")
except main.HTTPException as e:
    assert e.status_code == 404
print("compare validation OK")

# ---- 称重误差校核(只读) ----
import math as _math
def wc_route(plan_id):
    found = find("POST", f"/api/plan/{plan_id}/weighing-check")
    return (found if isinstance(found, tuple) else (found, ()))
def wc_call(plan_id, errors, limit=None):
    fn, args = wc_route(plan_id)
    req = types.SimpleNamespace(
        tube_errors=[types.SimpleNamespace(tube_id=int(t),
                                          mass_error_bound_g=float(e))
                     for t, e in errors],
        u1_limit_g_mm=limit,
    )
    return fn(*args, req)

nom_u1 = rec["result"]["metrics"]["u1_mag_g_mm"]
masses = {p["tube_id"]: p["mass"] for p in rec["result"]["placements"]}
errs = [(tid, min(0.5, m)) for tid, m in masses.items()]
wc = wc_call(1, errs)
# 标称值必须与原方案指标一致(固定落槽/角度)
assert abs(wc["nominal"]["u1_mag_g_mm"] - nom_u1) < 1e-6, wc["nominal"]
# 最不利值不小于标称值; 实际质量落在 [m-e, m+e] 且向量复算与最不利值一致
assert wc["worst_case"]["u1_mag_g_mm"] + 1e-7 >= nom_u1
ux = uy = 0.0
for row in wc["tubes"]:
    m, e = masses[row["tube_id"]], row["mass_error_bound_g"]
    assert m - e - 1e-9 <= row["actual_mass_g"] <= m + e + 1e-9
    a = _math.radians(row["angle_deg"])
    ux += row["actual_mass_g"] * _math.cos(a)
    uy += row["actual_mass_g"] * _math.sin(a)
recomputed = 100.0 * _math.hypot(ux, uy)
assert abs(recomputed - wc["worst_case"]["u1_mag_g_mm"]) < 1e-5, (
    recomputed, wc["worst_case"]["u1_mag_g_mm"])
# 用 2^n 全组合枚举交叉验证最不利值
placed = [p for p in rec["result"]["placements"]]
emap = dict(errs)
brute = 0.0
for mask in range(1 << len(placed)):
    bx = by = 0.0
    for i, p in enumerate(placed):
        s = 1.0 if (mask >> i) & 1 else -1.0
        mm = p["mass"] + s * emap[p["tube_id"]]
        a = _math.radians(p["angle"])
        bx += mm * _math.cos(a); by += mm * _math.sin(a)
    brute = max(brute, 100.0 * _math.hypot(bx, by))
assert abs(brute - wc["worst_case"]["u1_mag_g_mm"]) < 1e-5, (brute, wc["worst_case"])
# 限值判定: 取最不利值+1 合格 / -1 超限 / 0 限值必超限(标称非零)
wc_ok = wc_call(1, errs, wc["worst_case"]["u1_mag_g_mm"] + 1.0)
assert wc_ok["within_limit"] is True and wc_ok["excess_g_mm"] == 0.0
wc_bad = wc_call(1, errs, max(0.0, wc["worst_case"]["u1_mag_g_mm"] - 1.0))
assert wc_bad["within_limit"] is False
assert abs(wc_bad["excess_g_mm"] - 1.0) < 1e-6
wc_nolim = wc_call(1, errs)
assert wc_nolim["within_limit"] is None and wc_nolim["excess_g_mm"] is None
# 缺省(全部误差为 0): 最不利 == 标称
wc_zero = wc_call(1, [])
assert abs(wc_zero["worst_case"]["u1_mag_g_mm"] - nom_u1) < 1e-7
print("weighing-check worst=%.3f nominal=%.3f brute=%.3f naive=%.3f OK" % (
    wc["worst_case"]["u1_mag_g_mm"], nom_u1, brute, wc["naive_abs_worst_g_mm"]))

# 校验错误: 误差超标称质量 / 试管不存在 / 重复填写 -> 400
tid0, m0 = next(iter(masses.items()))
for bad_body in (
    [(tid0, m0 + 0.01)],        # 超过标称质量
    [(99999, 0.1)],             # 方案中不存在的试管
    [(tid0, 0.1), (tid0, 0.2)],# 重复填写
):
    try:
        wc_call(1, bad_body); raise SystemExit("should 400")
    except main.HTTPException as e:
        assert e.status_code == 400, e
# 方案不存在 -> 404
try:
    wc_call(999, [(1, 0.1)]); raise SystemExit("should 404")
except main.HTTPException as e:
    assert e.status_code == 404
# 只读: 校核后原方案指标不变
assert call("GET", "/api/plan/1")["result"]["metrics"]["u1_mag_g_mm"] == nom_u1
print("weighing-check validation/read-only OK")

# ---- 实测振动校准(只读, 试重法) ----
def cal_route(plan_id):
    found = find("POST", f"/api/plan/{plan_id}/vibration-calibration")
    return (found if isinstance(found, tuple) else (found, ()))
def cal_call(plan_id, body):
    fn, args = cal_route(plan_id)
    req = types.SimpleNamespace(
        baseline_mag=float(body["baseline_mag"]),
        baseline_phase_deg=float(body["baseline_phase_deg"]),
        with_weight_mag=float(body["with_weight_mag"]),
        with_weight_phase_deg=float(body["with_weight_phase_deg"]),
        trial_mass_g=float(body["trial_mass_g"]),
        trial_slot=int(body["trial_slot"]),
        available_masses=[float(x) for x in body.get("available_masses", [])],
    )
    return fn(*args, req)

# 合成已知 alpha: 0.02 读数/(g·mm) @120°, V0=10@0°, 试重 5g 放某空闲启用槽
cal_alpha = 0.02 * complex(_math.cos(_math.radians(120)), _math.sin(_math.radians(120)))
v0c = complex(10.0, 0.0)
trial_s = next(s["slot"] for s in rec["result"]["slot_state"]
               if s["enabled"] and s["tube_id"] is None)
trial_a = _math.radians(next(s["angle"] for s in rec["request"]["slots"]
                             if s["index"] == trial_s))
v1c = v0c + cal_alpha * (5.0 * 100.0 * complex(_math.cos(trial_a), _math.sin(trial_a)))
cal_body = {
    "baseline_mag": 10.0, "baseline_phase_deg": 0.0,
    "with_weight_mag": abs(v1c),
    "with_weight_phase_deg": _math.degrees(_math.atan2(v1c.imag, v1c.real)) % 360,
    "trial_mass_g": 5.0, "trial_slot": trial_s,
    "available_masses": [3.0, 5.0, 8.0],
}
cal = cal_call(1, cal_body)
assert abs(cal["influence_coefficient"]["mag"] - 0.02) < 1e-9
assert abs(cal["influence_coefficient"]["phase_deg"] - 120.0) < 1e-9
mags = [p["predicted"]["mag"] for p in cal["predictions"]]
assert mags == sorted(mags)
best = cal["recommendation"]
assert best["predicted"]["mag"] == min(mags)
# 预测公式复算
for p in cal["predictions"]:
    th = _math.radians(p["angle_deg"])
    expect = v0c + cal_alpha * (p["mass_g"] * 100.0 * complex(_math.cos(th), _math.sin(th)))
    assert abs(abs(expect) - p["predicted"]["mag"]) < 1e-7
assert best["improvement_pct"] is not None
print("vibration-calibration alpha/prediction OK, best slot=%s mass=%g resid=%.4f" % (
    best["slot"], best["mass_g"], best["predicted"]["mag"]))

# 无可用配重: 只标定不推荐
cal_nomass = cal_call(1, {**cal_body, "available_masses": []})
assert cal_nomass["recommendation"] is None and cal_nomass["predictions"] == []
assert abs(cal_nomass["influence_coefficient"]["mag"] - 0.02) < 1e-9

# 校验失败 -> 400: 试重槽占用 / 禁用 / 影响系数近零 / 试重质量非正 / 负读数
occ_slot = next(p["slot"] for p in rec["result"]["placements"])
for bad_body in (
    {**cal_body, "trial_slot": occ_slot},       # 已占用
    {**cal_body, "trial_slot": 6},              # 已禁用
    {**cal_body, "with_weight_mag": 10.0,
     "with_weight_phase_deg": 0.0},              # ΔV=0, α 近零
    {**cal_body, "trial_mass_g": 0.0},
    {**cal_body, "baseline_mag": -1.0},
):
    try:
        cal_call(1, bad_body); raise SystemExit("should 400")
    except main.HTTPException as e:
        assert e.status_code == 400, e
# 方案不存在 -> 404
try:
    cal_call(999, cal_body); raise SystemExit("should 404")
except main.HTTPException as e:
    assert e.status_code == 404
# 只读: 校准后原方案指标不变
assert call("GET", "/api/plan/1")["result"]["metrics"]["u1_mag_g_mm"] == nom_u1
print("vibration-calibration validation/read-only OK")

# 列表 / 详情 / 导出 / 删除
lst=call("GET","/api/plans")
assert len(lst["items"])==3
detail=call("GET","/api/plan/2")
assert detail["result"]["total_tubes"]==7
exp=call("GET","/api/plan/2/export")
assert json.loads(json.dumps(exp.content))["id"]==2
d=call("DELETE","/api/plan/1")
assert d["deleted"]==1
try:
    call("GET","/api/plan/1"); raise SystemExit("should 404")
except main.HTTPException as e:
    assert e.status_code==404
print("list/detail/export/delete OK")

# health/config
assert call("GET","/api/health")["status"]=="ok"
assert "physics" in call("GET","/api/config")
print("health/config OK")

# 纯等量完美平衡
perf=dict(req_body); perf["tubes"]=[{"id":i,"mass":50} for i in range(1,5)]
perf["slots"]=[{"index":i,"angle":i*30,"enabled":True} for i in range(12)]
rp=call("POST","/api/plan",perf)
assert rp["result"]["metrics"]["u1_mag_g_mm"]<1e-6
print("perfect balance U1=0 OK")
print("BACKEND SMOKE TESTS PASS")
