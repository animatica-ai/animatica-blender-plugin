"""Effectors in, pose out — on the user's machine, through onnxruntime.

    from autoposer_runtime import Engine

    eng = Engine.open()                       # resolves the bundle and the runtime
    out = eng.pose([
        {"joint": "LeftHand",  "type": "pos",    "pos": [0.45, 1.60, 0.25], "tol": 0.01},
        {"joint": "LeftFoot",  "type": "pos",    "pos": [0.10, 0.06, 0.00], "tol": 0.01},
        {"joint": "RightFoot", "type": "pos",    "pos": [-0.10, 0.06, 0.00], "tol": 0.01},
        {"joint": "Head",      "type": "lookat", "pos": [1.5, 1.5, 1.0]},
    ], bone_lengths=my_29d_vector)

    out["joints"]        # (J,3) world positions
    out["rotations_6d"]  # (J,6) GLOBAL rotations, first two matrix columns
    out["ik"]            # {"err_before_cm", "err_after_cm", ...}

The payload is deliberately identical to what the sidecar's `POST /pose` returns, so a client
can switch between local and cloud solving without touching the code that applies the result.

`refine()` is the other half: re-solve a pose you already have onto moved targets, IK only.
It is what a drag loop calls between full solves — the poser decides how the body carries
itself, the IK decides where the control lands, and only the second has to keep up with the
mouse.

READ `err_before_cm`. It is the poser alone, before the IK. If it is large while the final
pose looks fine, the IK is covering for a mis-wired input — a wrong body descriptor, a frame
mix-up. That is how a 223 cm conditioning bug stayed invisible for a week.
"""
from __future__ import annotations

import sys
import time

import numpy as np

from .bundle import Bundle, resolve
from .geometry import Skeleton, mat_to_sixd, sixd_to_mat


def _ort_version() -> str:
    try:
        import onnxruntime
        return onnxruntime.__version__
    except Exception:                                   # noqa: BLE001
        return "?"


TYPES = {"pos": 0, "rot": 1, "lookat": 2}

DEFAULT_POS_TOL = 0.01
DEFAULT_ROT_TOL = 0.05


class Engine:
    """Two onnxruntime sessions (the network, the solver) and the skeleton they share."""

    def __init__(self, bundle: Bundle, *, threads: int = 0):
        import onnxruntime as ort

        self.bundle = bundle
        self.meta = bundle.meta
        self.skel = Skeleton(bundle.meta)
        opts = ort.SessionOptions()
        if threads:
            opts.intra_op_num_threads = int(threads)
        opts.log_severity_level = 3
        providers = ["CPUExecutionProvider"]
        self._poser = ort.InferenceSession(str(bundle.poser), opts, providers=providers)
        self._ik = ort.InferenceSession(str(bundle.ik), opts, providers=providers)
        d = self.meta.get("ik", {})
        self.floor_joints = "feet"      # "feet" | "body" | a list of joints | "off"
        self.ik_prior = float(d.get("default_prior", 0.15))
        self.ik_damping = float(d.get("default_damping", 1e-3))
        self.look_w = float(d.get("default_look_w", 50.0))
        self.floor_w = float(d.get("default_floor_w", 5.0))
        self.ik_iters = int(d.get("iters", 12))

    # ------------------------------------------------------------------ construction
    @classmethod
    def open(cls, source=None, *, threads: int = 0, check: bool = True, **kw) -> "Engine":
        """Resolve a bundle (local override, cache, or download), load it, and check it."""
        eng = cls(resolve(source, **kw), threads=threads)
        if check:
            ok, err = eng.selftest()
            if not ok:
                raise RuntimeError(
                    f"the IK graph does not reproduce its own stored answer on this machine "
                    f"({err:.2f} cm out). This is an onnxruntime problem, not a bad pose — "
                    f"the solver would silently return wrong poses. onnxruntime "
                    f"{_ort_version()}, {sys.platform}.")
        return eng

    def selftest(self, tol_cm: float = 0.5):
        """Run the bundle's stored IK problem and compare against the stored answer.

        Returns `(ok, worst_joint_cm)`. Costs one solve — a few milliseconds, once — and it
        is worth it: a miscompiled graph does not raise, it returns a wrong pose. onnxruntime
        1.30 has exactly such a bug (a `Transpose -> MatMul` fusion that drops the transpose),
        and the symptom was a solver that ran, reported plausible numbers, and never moved
        the body.

        The tolerance is loose on purpose: the solver's damping ladder makes an accept/reject
        decision per iteration, so a last-bit difference between two correct implementations
        can pick a different step. Half a centimetre separates that from a broken graph by
        two orders of magnitude.
        """
        st = self.meta.get("selftest")
        if not st:
            return True, 0.0                 # an older bundle simply cannot be checked
        feed = {}
        for k, v in st["inputs"].items():
            if k in ("eff_joint", "look_joint", "floor_joint"):
                feed[k] = np.asarray(v, dtype=np.int64)
            elif isinstance(v, (int, float)):
                feed[k] = np.array(v, dtype=np.float32)
            else:
                feed[k] = np.asarray(v, dtype=np.float32)
        local, root, _ = self._ik.run(None, feed)
        rest = np.asarray(st["inputs"]["rest"], dtype=np.float32)
        _, got = self.skel.fk(local, root, rest)
        _, want = self.skel.fk(np.asarray(st["expect"]["local_out"], dtype=np.float32),
                               np.asarray(st["expect"]["root_out"], dtype=np.float32), rest)
        worst = float(np.linalg.norm(got - want, axis=-1).max() * 100.0)
        return worst <= tol_cm, worst

    @property
    def joint_names(self):
        return list(self.skel.names)

    # ------------------------------------------------------------------ the inputs
    def _encode(self, effectors):
        """Effector dicts -> the network's five input arrays, plus the centroid.

        Referencing follows the checkpoint, exactly as trained: positions and look-at targets
        are expressed relative to the centroid of the position targets, rotations carry no
        world point and are left alone, and a floor-referenced model keeps y ABSOLUTE — the
        ground is a real height, not something relative.
        """
        E = len(effectors)
        val = np.zeros((1, E, 7), dtype=np.float32)
        wts = np.ones((1, E), dtype=np.float32)
        jid = np.zeros((1, E), dtype=np.int64)
        typ = np.zeros((1, E), dtype=np.int64)
        for k, e in enumerate(effectors):
            t = TYPES[str(e.get("type", "pos"))]
            jid[0, k] = self.skel.joint_id(e["joint"])
            typ[0, k] = t
            wts[0, k] = float(e.get("weight", 1.0))
            if t == 2:
                val[0, k, 0:3] = np.asarray(e["pos"], dtype=np.float32)
                aim = np.asarray(e.get("aim", [0.0, 0.0, 1.0]), dtype=np.float32)
                val[0, k, 3:6] = aim / max(float(np.linalg.norm(aim)), 1e-6)
            elif t == 1:
                val[0, k, 0:6] = np.asarray(e["rot"], dtype=np.float32)[:6]
                val[0, k, 6] = float(e.get("tol", DEFAULT_ROT_TOL))
            else:
                val[0, k, 0:3] = np.asarray(e["pos"], dtype=np.float32)
                val[0, k, 6] = float(e.get("tol", DEFAULT_POS_TOL))
        ref = typ[0] != 1
        pos = typ[0] == 0
        if pos.any():
            cent = val[0, pos, 0:3].mean(axis=0, keepdims=True)
        elif ref.any():
            cent = val[0, ref, 0:3].mean(axis=0, keepdims=True)
        else:
            cent = np.zeros((1, 3), dtype=np.float32)
        if self.skel.floor_ref:
            cent = cent * np.array([[1.0, 0.0, 1.0]], dtype=np.float32)
        val[0, :, 0:3] -= cent * ref.reshape(-1, 1).astype(np.float32)
        return val, wts, jid, typ, cent.reshape(3).astype(np.float32)

    # ------------------------------------------------------------------ the solve
    def pose(self, effectors, bone_lengths=None, *, ik_refine: bool = True,
             ik_prior: float | None = None, floor: bool = True, toe_roll: bool = True,
             toe_tips=None, floor_joints=None, floor_w=None, hard_floor: bool = False):
        """Solve one pose. See the module docstring for the payload.

        `toe_tips` is {ball joint: offset to the toe tip}, measured on the caller's REST pose:
        SOMA-30 has no toe tip, so without it the ball goes onto the floor and the toes keep
        whatever angle they had. With it they lie flat.

        `floor_joints` chooses what the IK's floor term holds up — `"feet"` (the checkpoint's
        own set), `"body"` (every joint), `"off"`, or a list. `info["below_floor_cm"]` reports
        how deep the lowest joint still is, which is not always zero and should not be: an
        effector pinned below the floor outranks the floor.
        """
        if not effectors:
            raise ValueError("at least one effector is required")
        blen = self.skel.average_body() if bone_lengths is None else bone_lengths
        rest = self.skel.rest_joints(blen)
        val, wts, jid, typ, cent = self._encode(effectors)
        t0 = time.perf_counter()
        draft, r6 = self._poser.run(None, {
            "val": val, "weights": wts, "joint_id": jid, "eff_type": typ,
            "cond": self.skel.body_cond(blen)})
        J = self.skel.njoints
        local = sixd_to_mat(r6.reshape(J, 6))
        root = draft.reshape(J, 3)[self.skel.root_idx].astype(np.float32)

        info = None
        types = typ[0]
        pe = [e for e, t in zip(effectors, types, strict=True) if t == 0]
        le = [e for e, t in zip(effectors, types, strict=True) if t == 2]
        if ik_refine and (pe or le):
            local, root, info = self.solve_ik(
                local, root, rest,
                eff_joint=[self.skel.joint_id(e["joint"]) for e in pe],
                eff_target=val[0, types == 0, 0:3],
                eff_weight=[1.0 / max(float(e.get("tol", DEFAULT_POS_TOL)), 1e-3) for e in pe],
                look_joint=[self.skel.joint_id(e["joint"]) for e in le],
                look_target=val[0, types == 2, 0:3],
                look_axis=np.asarray([e.get("aim", [0.0, 0.0, 1.0]) for e in le],
                                     dtype=np.float32).reshape(-1, 3),
                floor=floor and self.skel.floor_ref, prior=ik_prior,
                floor_joints=floor_joints, floor_w=floor_w)

        floor_y = -float(cent[1])
        if hard_floor and floor and self.skel.floor_ref:
            local, worst = self.skel.enforce_floor(
                local, root, rest, floor_y=floor_y, toe_tips=toe_tips)
            if info is not None:
                info["hard_floor"] = True
        elif toe_roll and floor and self.skel.floor_ref:
            local, lifted = self.skel.roll_onto_floor(
                local, root, rest, self.skel.foot_roll_pairs(toe_tips), floor_y=floor_y)
            if info is not None:
                info["toe_roll_cm"] = lifted * 100.0
        g, joints = self.skel.fk(local, root, rest)
        if info is not None:
            info["below_floor_cm"] = min(0.0, float(joints[:, 1].min()) - floor_y) * 100.0
            if pe:
                # lifting a joint out of the floor can move something an effector pinned, so
                # the residual is recomputed from the pose that actually comes out. A number
                # that describes the pose before the last correction is a number that lies.
                tj = [self.skel.joint_id(e["joint"]) for e in pe]
                tw = np.asarray([1.0 / max(float(e.get("tol", DEFAULT_POS_TOL)), 1e-3)
                                 for e in pe], np.float32)
                d = np.linalg.norm(joints[tj] - val[0, types == 0, 0:3], axis=-1)
                info["err_after_cm"] = float((d * tw).sum() / max(tw.sum(), 1e-6)) * 100.0
        out = {
            "joints": (joints + cent).astype(np.float32),
            "rotations_6d": mat_to_sixd(g),
            "root": (root + cent).astype(np.float32),
            "rest_joints": rest,
            "names": list(self.skel.names),
            "ik": info,
            "solve_ms": (time.perf_counter() - t0) * 1e3,
        }
        return out

    def solve_ik(self, local, root, rest, *, eff_joint, eff_target, eff_weight=None,
                 look_joint=(), look_target=None, look_axis=None, floor: bool = True,
                 floor_y: float = 0.0, prior: float | None = None, damping=None,
                 floor_joints=None, floor_w=None):
        """The solver on its own: local rotations + root in, refined ones out.

        Frame-agnostic — it only ever compares its own forward kinematics against the targets —
        so targets and root just have to agree with each other. `pose` calls it in the
        centroid-referenced frame; `refine` calls it in world.

        The graph always carries a look-at row and a floor row. When there is nothing to aim
        at, one masked-off row goes in: shapes stay static, the row contributes nothing.
        """
        J = self.skel.njoints
        ej = np.asarray(list(eff_joint), dtype=np.int64)
        if ej.size == 0:
            # the solver needs at least one position row; ask the root to stay where it is
            ej = np.asarray([self.skel.root_idx], dtype=np.int64)
            et = root.reshape(1, 3).astype(np.float32)
            ew = np.zeros(1, dtype=np.float32)
        else:
            et = np.asarray(eff_target, dtype=np.float32).reshape(-1, 3)
            ew = (np.ones(ej.size, dtype=np.float32) if eff_weight is None
                  else np.asarray(eff_weight, dtype=np.float32).reshape(-1))
        lj = np.asarray(list(look_joint), dtype=np.int64)
        if lj.size:
            lt = np.asarray(look_target, dtype=np.float32).reshape(-1, 3)
            la = np.asarray(look_axis, dtype=np.float32).reshape(-1, 3)
            lmask = np.ones(lj.size, dtype=np.float32)
        else:
            lj = np.zeros(1, dtype=np.int64)
            lt = np.zeros((1, 3), dtype=np.float32)
            la = np.asarray([[0.0, 0.0, 1.0]], dtype=np.float32)
            lmask = np.zeros(1, dtype=np.float32)
        fj = self.skel.floor_joint_set(
            self.floor_joints if floor_joints is None else floor_joints)
        if not fj.size:                       # the graph always carries a floor row
            fj, floor = np.asarray(self.skel.foot_joints, np.int64)[:1], False
        restc = np.asarray(rest, dtype=np.float32).reshape(J, 3)
        restc = restc - restc[self.skel.root_idx]
        # onnxruntime wants a 0-d ARRAY for a scalar input; a numpy scalar is rejected
        def f32(x):
            return np.array(x, dtype=np.float32)

        out = self._ik.run(None, {
            "rest": restc,
            "local_rot": np.ascontiguousarray(local, dtype=np.float32),
            "root": np.asarray(root, dtype=np.float32).reshape(3),
            "eff_joint": ej, "eff_target": et, "eff_weight": ew,
            "look_joint": lj, "look_target": lt, "look_axis": la, "look_mask": lmask,
            "look_w": f32(self.look_w),
            "floor_joint": fj,
            "floor_w": f32((self.floor_w if floor_w is None else floor_w) if floor else 0.0),
            "floor_y": f32(floor_y),
            "prior": f32(self.ik_prior if prior is None else prior),
            "damping": f32(self.ik_damping if damping is None else damping)})
        local_out, root_out, info = out
        return local_out, root_out, {
            "err_before_cm": float(info[0]), "err_after_cm": float(info[1]),
            "max_delta_deg": float(info[2]), "look_after_deg": float(info[3]),
            "look_before_deg": float(info[4]), "backend": "onnx",
        }

    def refine(self, pose: dict, effectors, *, floor: bool = True, prior: float | None = None,
               toe_roll: bool = True, toe_tips=None, floor_joints=None, floor_w=None):
        """Re-solve a pose you already hold onto moved targets — the drag loop's call.

        `pose` is a payload from `pose()` (or from the sidecar): GLOBAL `rotations_6d`, world
        `root`, `rest_joints`. Returns the same shape, so a caller applies it identically.
        Position and look-at effectors act; a rotation effector has no term in the solver and
        is ignored here — it lands on the next full `pose()`.
        """
        J = self.skel.njoints
        rest = np.asarray(pose["rest_joints"], dtype=np.float32).reshape(J, 3)
        local = self.skel.global_to_local(
            sixd_to_mat(np.asarray(pose["rotations_6d"], dtype=np.float32)))
        root = np.asarray(pose["root"], dtype=np.float32).reshape(3)
        pe = [e for e in effectors if TYPES[str(e.get("type", "pos"))] == 0]
        le = [e for e in effectors if TYPES[str(e.get("type", "pos"))] == 2]
        t0 = time.perf_counter()
        local, root, info = self.solve_ik(
            local, root, rest,
            eff_joint=[self.skel.joint_id(e["joint"]) for e in pe],
            eff_target=np.asarray([e["pos"] for e in pe], dtype=np.float32).reshape(-1, 3),
            eff_weight=[1.0 / max(float(e.get("tol", DEFAULT_POS_TOL)), 1e-3) for e in pe],
            look_joint=[self.skel.joint_id(e["joint"]) for e in le],
            look_target=np.asarray([e["pos"] for e in le], dtype=np.float32).reshape(-1, 3),
            look_axis=np.asarray([e.get("aim", [0.0, 0.0, 1.0]) for e in le],
                                 dtype=np.float32).reshape(-1, 3),
            floor=floor and self.skel.floor_ref, prior=prior,
            floor_joints=floor_joints, floor_w=floor_w)
        if toe_roll and floor and self.skel.floor_ref:
            local, lifted = self.skel.roll_onto_floor(
                local, root, rest, self.skel.foot_roll_pairs(toe_tips))
            info["toe_roll_cm"] = lifted * 100.0
        g, joints = self.skel.fk(local, root, rest)
        return {
            "joints": joints.astype(np.float32),
            "rotations_6d": mat_to_sixd(g),
            "root": root.astype(np.float32),
            "rest_joints": rest,
            "names": list(self.skel.names),
            "ik": info,
            "solve_ms": (time.perf_counter() - t0) * 1e3,
            "local": True,
        }
