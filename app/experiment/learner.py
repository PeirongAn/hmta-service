"""Boundary Learner — Phase A piecewise ridge + Phase B constrained GP.

Phase A: Piecewise ridge regression per discrete collaboration mode.
Phase B: Dual Gaussian Process (objective + safety) with expert prior mean,
         categorical mode embedding, and constrained Bayesian optimisation.
"""

from __future__ import annotations

import logging
import time
from collections import defaultdict
from pathlib import Path
from statistics import mean
from typing import TYPE_CHECKING, Any

import numpy as np
from pydantic import BaseModel, Field
import yaml
from scipy.optimize import minimize
from scipy.stats import norm
from sklearn.gaussian_process import GaussianProcessRegressor
from sklearn.gaussian_process.kernels import Matern, RBF, WhiteKernel, ConstantKernel
from sklearn.linear_model import Ridge
from sklearn.metrics import r2_score
from sklearn.preprocessing import OneHotEncoder, StandardScaler

from app.experiment.store import ExperimentRecord, ExperimentStore

if TYPE_CHECKING:
    from app.capability.registry import CapabilityRegistry

logger = logging.getLogger(__name__)

MODE_SEGMENTS: dict[int, tuple[float, float]] = {
    1: (0.0, 0.1),    # autonomous (task_based)
    2: (0.1, 0.3),    # supervised_report
    3: (0.3, 0.5),    # human_approve_execute (approve)
    4: (0.5, 0.8),    # human_plan_execute (partner)
    5: (0.8, 1.0),    # human_remote_control (proxy)
}

MODE_IDX_TO_COLLAB: dict[int, str] = {
    1: "task_based",
    2: "task_based",
    3: "partner",
    4: "partner",
    5: "proxy",
}

MODE_IDX_TO_BT: dict[int, str] = {
    1: "autonomous",
    2: "autonomous",
    3: "human_plan_execute",
    4: "human_plan_execute",
    5: "human_plan_execute",
}

FEATURE_NAMES = [
    "complexity", "urgency", "risk", "ambiguity",
    "time_pressure", "cognitive_switch_cost",
]


class SegmentResult(BaseModel):
    mode_idx: int
    alpha: dict[str, float] = Field(default_factory=dict)
    beta: float = 0.0
    gamma: dict[str, float] = Field(default_factory=dict)
    intercept: float = 0.0
    r_squared: float = 0.0
    sample_count: int = 0


class PiecewiseLinearResult(BaseModel):
    segments: dict[int, SegmentResult] = Field(default_factory=dict)
    overall_r_squared: float = 0.0
    sample_count: int = 0
    boundary_equations: dict[int, str] = Field(default_factory=dict)
    fitted_at: float = Field(default_factory=time.time)


class ConstrainedOptimalX(BaseModel):
    optimal_x: float = 0.0
    optimal_mode_idx: int = 1
    predicted_obj: float = 0.0
    predicted_safety: float = 1.0
    safety_feasible: bool = True
    attention_cost: float = 0.0
    confidence: float = 0.0


class BoundaryLearner:
    """Phase A piecewise linear boundary learner."""

    def __init__(
        self,
        store: ExperimentStore,
        ontology_path: str | Path | None = None,
    ) -> None:
        self._store = store
        self._ontology_path = Path(ontology_path) if ontology_path else None
        self._models: dict[int, Ridge] = {}
        self._scalers: dict[int, StandardScaler] = {}
        self._result: PiecewiseLinearResult | None = None

    def _records_to_arrays(
        self, records: list[ExperimentRecord],
    ) -> tuple[np.ndarray, np.ndarray, np.ndarray, np.ndarray]:
        """Convert records to numpy arrays: features, x, performance_obj, safety_score."""
        n = len(records)
        F = np.zeros((n, len(FEATURE_NAMES)))
        X = np.zeros(n)
        P_obj = np.zeros(n)
        P_safety = np.zeros(n)

        for i, r in enumerate(records):
            for j, fname in enumerate(FEATURE_NAMES):
                F[i, j] = getattr(r, fname, 0.0)
            X[i] = r.human_involvement
            P_obj[i] = r.performance_obj
            P_safety[i] = r.safety_score

        return F, X, P_obj, P_safety

    def _build_design_matrix(
        self, F: np.ndarray, X: np.ndarray,
    ) -> np.ndarray:
        """Build expanded design matrix: [f_1..f_k, x, x*f_1..x*f_k]."""
        n = F.shape[0]
        X_col = X.reshape(-1, 1)
        interaction = F * X_col
        return np.hstack([F, X_col, interaction])

    def fit_piecewise_linear(self) -> PiecewiseLinearResult:
        """Fit piecewise ridge regression across discrete mode segments."""
        all_records = self._store.query_all()
        if not all_records:
            self._result = PiecewiseLinearResult(sample_count=0)
            return self._result

        segments_data: dict[int, list[ExperimentRecord]] = defaultdict(list)
        for r in all_records:
            segments_data[r.collaboration_mode_idx].append(r)

        segment_results: dict[int, SegmentResult] = {}
        all_y_true: list[float] = []
        all_y_pred: list[float] = []
        boundary_eqs: dict[int, str] = {}

        for mode_idx in sorted(segments_data.keys()):
            recs = segments_data[mode_idx]
            if len(recs) < 3:
                segment_results[mode_idx] = SegmentResult(
                    mode_idx=mode_idx, sample_count=len(recs),
                )
                continue

            F, X, P_obj, P_safety = self._records_to_arrays(recs)
            design = self._build_design_matrix(F, X)

            scaler = StandardScaler()
            design_scaled = scaler.fit_transform(design)

            model = Ridge(alpha=1.0)
            model.fit(design_scaled, P_obj)

            y_pred = model.predict(design_scaled)
            r2 = r2_score(P_obj, y_pred) if len(recs) > 2 else 0.0

            self._models[mode_idx] = model
            self._scalers[mode_idx] = scaler

            n_features = len(FEATURE_NAMES)
            coef = model.coef_
            alpha_dict = {FEATURE_NAMES[j]: float(coef[j]) for j in range(n_features)}
            beta_val = float(coef[n_features])
            gamma_dict = {
                FEATURE_NAMES[j]: float(coef[n_features + 1 + j])
                for j in range(n_features)
            }

            segment_results[mode_idx] = SegmentResult(
                mode_idx=mode_idx,
                alpha=alpha_dict,
                beta=beta_val,
                gamma=gamma_dict,
                intercept=float(model.intercept_),
                r_squared=r2,
                sample_count=len(recs),
            )

            all_y_true.extend(P_obj.tolist())
            all_y_pred.extend(y_pred.tolist())

            eq_parts = []
            for fname, a in alpha_dict.items():
                if abs(a) > 0.01:
                    eq_parts.append(f"{a:+.3f}*{fname}")
            if abs(beta_val) > 0.01:
                eq_parts.append(f"{beta_val:+.3f}*x")
            for fname, g in gamma_dict.items():
                if abs(g) > 0.01:
                    eq_parts.append(f"{g:+.3f}*x*{fname}")
            eq_str = f"P = {model.intercept_:.3f} " + " ".join(eq_parts)
            boundary_eqs[mode_idx] = eq_str

        overall_r2 = 0.0
        if all_y_true and len(all_y_true) > 2:
            overall_r2 = r2_score(all_y_true, all_y_pred)

        self._result = PiecewiseLinearResult(
            segments=segment_results,
            overall_r_squared=overall_r2,
            sample_count=len(all_records),
            boundary_equations=boundary_eqs,
        )
        logger.info(
            "Phase A fit: %d records, %d segments, R²=%.4f",
            len(all_records), len(segment_results), overall_r2,
        )
        return self._result

    def predict(self, features: dict[str, float], x: float, mode_idx: int) -> float:
        """Predict performance for given features, involvement degree, and mode."""
        model = self._models.get(mode_idx)
        scaler = self._scalers.get(mode_idx)
        if model is None or scaler is None:
            return 0.5

        F = np.array([[features.get(f, 0.0) for f in FEATURE_NAMES]])
        X = np.array([x])
        design = self._build_design_matrix(F, X)
        design_scaled = scaler.transform(design)
        return float(model.predict(design_scaled)[0])

    def suggest_optimal_x_constrained(
        self,
        features: dict[str, float],
        safety_threshold: float = 0.95,
    ) -> ConstrainedOptimalX:
        """Find optimal x under safety constraint using scipy.optimize."""
        if not self._models:
            if not self._result:
                self.fit_piecewise_linear()
            if not self._models:
                return ConstrainedOptimalX()

        all_records = self._store.query_all()
        if not all_records:
            return ConstrainedOptimalX()

        # Build a simple safety model from data
        F_all, X_all, _, P_safety_all = self._records_to_arrays(all_records)
        safety_mean = float(np.mean(P_safety_all)) if len(P_safety_all) > 0 else 1.0

        best_result = ConstrainedOptimalX()
        best_obj = -np.inf

        for mode_idx, model in self._models.items():
            scaler = self._scalers[mode_idx]
            lo, hi = MODE_SEGMENTS.get(mode_idx, (0.0, 1.0))

            def neg_obj(x_val, _mode=mode_idx, _model=model, _scaler=scaler):
                F = np.array([[features.get(f, 0.0) for f in FEATURE_NAMES]])
                X = np.array([x_val[0]])
                design = self._build_design_matrix(F, X)
                design_scaled = _scaler.transform(design)
                return -float(_model.predict(design_scaled)[0])

            res = minimize(
                neg_obj,
                x0=[(lo + hi) / 2],
                bounds=[(lo, hi)],
                method="L-BFGS-B",
            )

            if res.success:
                opt_x = float(res.x[0])
                pred_obj = -float(res.fun)
                pred_safety = safety_mean

                # Estimate safety from records in this mode
                mode_safety_records = [
                    r.safety_score for r in all_records
                    if r.collaboration_mode_idx == mode_idx
                ]
                if mode_safety_records:
                    pred_safety = float(np.mean(mode_safety_records))

                feasible = pred_safety >= safety_threshold

                if feasible and pred_obj > best_obj:
                    best_obj = pred_obj
                    seg = self._result.segments.get(mode_idx) if self._result else None
                    confidence = seg.r_squared if seg else 0.0
                    best_result = ConstrainedOptimalX(
                        optimal_x=round(opt_x, 4),
                        optimal_mode_idx=mode_idx,
                        predicted_obj=round(pred_obj, 4),
                        predicted_safety=round(pred_safety, 4),
                        safety_feasible=True,
                        confidence=round(confidence, 4),
                    )

        return best_result

    def get_boundary_equation(self, mode_idx: int) -> dict[str, Any]:
        """Return human-readable boundary equation for a mode segment."""
        if not self._result:
            self.fit_piecewise_linear()
        if not self._result:
            return {"mode_idx": mode_idx, "equation": "insufficient data"}

        seg = self._result.segments.get(mode_idx)
        if not seg:
            return {"mode_idx": mode_idx, "equation": "no data for this mode"}

        return {
            "mode_idx": mode_idx,
            "equation": self._result.boundary_equations.get(mode_idx, ""),
            "alpha": seg.alpha,
            "beta": seg.beta,
            "gamma": seg.gamma,
            "intercept": seg.intercept,
            "r_squared": seg.r_squared,
            "sample_count": seg.sample_count,
        }

    def export_weights(self) -> dict[str, float]:
        """Export learned weights in a format compatible with capability-ontology.yaml."""
        if not self._result:
            self.fit_piecewise_linear()
        if not self._result or not self._result.segments:
            return {}

        weights: dict[str, float] = {}
        for mode_idx, seg in self._result.segments.items():
            prefix = f"mode_{mode_idx}"
            for fname, val in seg.alpha.items():
                weights[f"{prefix}.alpha.{fname}"] = val
            weights[f"{prefix}.beta"] = seg.beta
            weights[f"{prefix}.intercept"] = seg.intercept
            for fname, val in seg.gamma.items():
                weights[f"{prefix}.gamma.{fname}"] = val
        return weights

    def write_back_to_graph(self, registry: "CapabilityRegistry") -> dict[str, Any]:
        """Write learned results back to the hypergraph edges."""
        if not self._result:
            self.fit_piecewise_linear()
        if not self._result or self._result.overall_r_squared < 0.1:
            return {}

        graph = registry.get_graph_ref()
        records = self._store.query_all()
        if not records:
            return {}

        # Group by (entity, primary_capability)
        groups: dict[tuple[str, str], list[ExperimentRecord]] = defaultdict(list)
        for r in records:
            if r.assigned_robot and r.primary_capability:
                groups[(r.assigned_robot, r.primary_capability)].append(r)
            if r.human_supervisor and r.primary_capability:
                groups[(r.human_supervisor, r.primary_capability)].append(r)

        updates: dict[str, dict[str, Any]] = {}

        for (entity_id, cap_id), recs in groups.items():
            if not recs:
                continue

            avg_perf = mean(r.performance_obj for r in recs)
            features_avg = {
                fname: mean(getattr(r, fname, 0.0) for r in recs)
                for fname in FEATURE_NAMES
            }
            optimal = self.suggest_optimal_x_constrained(features_avg)

            registry.update_proficiency(entity_id, cap_id, min(1.0, max(0.0, avg_perf)))

            edge_id = f"hc_{entity_id}_{cap_id}"
            edge = graph.edges.get(edge_id)
            if edge:
                edge.attrs["optimal_x"] = optimal.optimal_x
                edge.attrs["optimal_x_confidence"] = optimal.confidence
                edge.attrs["optimal_mode_idx"] = optimal.optimal_mode_idx
                edge.attrs["performance_mean"] = round(avg_perf, 4)
                edge.attrs["sample_count"] = len(recs)
                edge.attrs["last_learned_at"] = time.time()

            updates[f"{entity_id}/{cap_id}"] = {
                "proficiency": round(avg_perf, 4),
                "optimal_x": optimal.optimal_x,
                "optimal_mode_idx": optimal.optimal_mode_idx,
                "confidence": optimal.confidence,
                "sample_count": len(recs),
            }

        # Update operator cognitive profiles
        operator_groups: dict[str, list[ExperimentRecord]] = defaultdict(list)
        for r in records:
            if r.human_supervisor:
                operator_groups[r.human_supervisor].append(r)

        for op_id, op_recs in operator_groups.items():
            response_times = [
                r.human_response_time_ms for r in op_recs
                if r.human_response_time_ms is not None and r.human_response_time_ms > 0
            ]
            success_rate = mean(1.0 if r.outcome_success else 0.0 for r in op_recs)
            cog_updates: dict[str, float] = {"decision_accuracy": round(success_rate, 4)}
            if response_times:
                cog_updates["avg_response_sec"] = round(mean(response_times) / 1000.0, 3)
            registry.update_cognitive_profile(op_id, cog_updates)

        logger.info("write_back_to_graph: updated %d entity-capability pairs", len(updates))
        return updates

    def write_back_to_ontology(self, yaml_path: str | Path) -> None:
        """Write learned weights back to capability-ontology YAML."""
        import yaml
        p = Path(yaml_path)
        if not p.exists():
            logger.warning("Ontology YAML not found at %s", p)
            return

        with open(p, encoding="utf-8") as f:
            data = yaml.safe_load(f) or {}

        weights = self.export_weights()
        data.setdefault("learned_weights", {})
        data["learned_weights"].update(weights)
        data["learned_weights"]["_fitted_at"] = time.time()
        data["learned_weights"]["_overall_r_squared"] = (
            self._result.overall_r_squared if self._result else 0.0
        )

        with open(p, "w", encoding="utf-8") as f:
            yaml.safe_dump(data, f, default_flow_style=False, allow_unicode=True)
        logger.info("Wrote learned weights to %s", p)

    def generate_heatmap_data(
        self,
        dim_x: str = "complexity",
        dim_y: str = "urgency",
        resolution: int = 20,
        fixed_features: dict[str, float] | None = None,
    ) -> dict[str, Any]:
        """Generate a 2D heatmap of optimal_x over two feature dimensions."""
        if not self._models:
            if not self._result:
                self.fit_piecewise_linear()

        base = {f: 0.5 for f in FEATURE_NAMES}
        if fixed_features:
            base.update(fixed_features)

        xs = np.linspace(0, 1, resolution).tolist()
        ys = np.linspace(0, 1, resolution).tolist()
        z_optimal_x = []
        z_performance = []

        for y_val in ys:
            row_x = []
            row_p = []
            for x_val in xs:
                feat = dict(base)
                feat[dim_x] = x_val
                feat[dim_y] = y_val
                opt = self.suggest_optimal_x_constrained(feat)
                row_x.append(opt.optimal_x)
                row_p.append(opt.predicted_obj)
            z_optimal_x.append(row_x)
            z_performance.append(row_p)

        return {
            "dim_x": dim_x,
            "dim_y": dim_y,
            "x_values": xs,
            "y_values": ys,
            "z_optimal_x": z_optimal_x,
            "z_performance": z_performance,
            "resolution": resolution,
        }

    # ══════════════════════════════════════════════════════════════════
    # Phase B: Constrained Gaussian Process
    # ══════════════════════════════════════════════════════════════════

    def fit_constrained_gp(self) -> "ConstrainedGPResult":
        """Fit dual GP: GP_obj for performance objective, GP_safety for safety constraint.

        Uses expert prior from Phase A + ontology as the mean function,
        one-hot encoded mode index as categorical feature, and Matern kernel.
        """
        records = self._store.query_all()
        if len(records) < 5:
            return ConstrainedGPResult(sample_count=len(records))

        Z, P_obj, P_safety = self._build_gp_features(records)

        # Expert prior mean: Phase A predictions (or 0.5 fallback)
        prior_mean_obj = self._compute_prior_mean(records, target="obj")
        prior_mean_safety = self._compute_prior_mean(records, target="safety")

        y_obj_centered = P_obj - prior_mean_obj
        y_safety_centered = P_safety - prior_mean_safety

        kernel = ConstantKernel(1.0) * Matern(nu=2.5) + WhiteKernel(noise_level=0.05)

        self._gp_obj = GaussianProcessRegressor(
            kernel=kernel, n_restarts_optimizer=3, alpha=1e-6, normalize_y=True,
        )
        self._gp_safety = GaussianProcessRegressor(
            kernel=kernel, n_restarts_optimizer=3, alpha=1e-6, normalize_y=True,
        )

        self._gp_obj.fit(Z, y_obj_centered)
        self._gp_safety.fit(Z, y_safety_centered)

        self._gp_Z = Z
        self._gp_prior_obj = prior_mean_obj
        self._gp_prior_safety = prior_mean_safety
        self._gp_records = records

        mode_sim = self._compute_mode_similarity(records)

        gp_result = ConstrainedGPResult(
            sample_count=len(records),
            obj_noise=float(self._gp_obj.kernel_.get_params().get("k2__noise_level", 0.05)),
            safety_noise=float(self._gp_safety.kernel_.get_params().get("k2__noise_level", 0.05)),
            mode_similarity=mode_sim,
            expert_prior_source=str(self._ontology_path or "phase_a"),
        )
        self._gp_result = gp_result
        logger.info("Phase B GP fit: %d records, obj_noise=%.4f", len(records), gp_result.obj_noise)
        return gp_result

    def _build_gp_features(
        self, records: list[ExperimentRecord],
    ) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
        """Build GP feature matrix Z = [f_features, x, mode_onehot, operator_state]."""
        n = len(records)
        n_feat = len(FEATURE_NAMES)
        n_modes = 5

        Z = np.zeros((n, n_feat + 1 + n_modes + 2))
        P_obj = np.zeros(n)
        P_safety = np.zeros(n)

        for i, r in enumerate(records):
            for j, fname in enumerate(FEATURE_NAMES):
                Z[i, j] = getattr(r, fname, 0.0)
            Z[i, n_feat] = r.human_involvement
            mode_col = min(max(r.collaboration_mode_idx - 1, 0), n_modes - 1)
            Z[i, n_feat + 1 + mode_col] = 1.0
            Z[i, n_feat + 1 + n_modes] = r.operator_cognitive_load
            Z[i, n_feat + 1 + n_modes + 1] = r.operator_fatigue
            P_obj[i] = r.performance_obj
            P_safety[i] = r.safety_score

        return Z, P_obj, P_safety

    def _compute_prior_mean(
        self, records: list[ExperimentRecord], target: str = "obj",
    ) -> np.ndarray:
        """Compute expert prior mean for each record using Phase A model or ontology."""
        n = len(records)
        prior = np.full(n, 0.5)

        if self._models:
            for i, r in enumerate(records):
                features = {fname: getattr(r, fname, 0.0) for fname in FEATURE_NAMES}
                pred = self.predict(features, r.human_involvement, r.collaboration_mode_idx)
                prior[i] = pred
        elif self._ontology_path and self._ontology_path.exists():
            try:
                with open(self._ontology_path, encoding="utf-8") as f:
                    ont = yaml.safe_load(f) or {}
                weights = ont.get("utility_weights", {})
                for i, r in enumerate(records):
                    prof = r.robot_proficiency * weights.get("proficiency", 0.3)
                    cap_match = weights.get("cap_match", 0.4)
                    prior[i] = min(1.0, prof + cap_match * 0.5)
            except Exception:
                pass

        return prior

    def _gp_predict(
        self, Z: np.ndarray, target: str = "obj",
    ) -> tuple[np.ndarray, np.ndarray]:
        """Predict with GP, adding back the prior mean."""
        gp = self._gp_obj if target == "obj" else self._gp_safety
        prior = self._gp_prior_obj if target == "obj" else self._gp_prior_safety

        if gp is None:
            return np.full(Z.shape[0], 0.5), np.full(Z.shape[0], 0.1)

        mean_centered, std = gp.predict(Z, return_std=True)

        if len(prior) == 1:
            mean_val = mean_centered + prior[0]
        else:
            mean_val = mean_centered + np.mean(prior)

        return mean_val, std

    def _build_gp_input(
        self, features: dict[str, float], x: float, mode_idx: int,
        cog_load: float = 0.3, fatigue: float = 0.2,
    ) -> np.ndarray:
        """Build a single GP input vector."""
        n_feat = len(FEATURE_NAMES)
        n_modes = 5
        z = np.zeros(n_feat + 1 + n_modes + 2)
        for j, fname in enumerate(FEATURE_NAMES):
            z[j] = features.get(fname, 0.0)
        z[n_feat] = x
        mc = min(max(mode_idx - 1, 0), n_modes - 1)
        z[n_feat + 1 + mc] = 1.0
        z[n_feat + 1 + n_modes] = cog_load
        z[n_feat + 1 + n_modes + 1] = fatigue
        return z.reshape(1, -1)

    def suggest_next_experiment(
        self, safety_threshold: float = 0.95,
    ) -> "ConstrainedExperimentSuggestion":
        """Suggest the next experiment using constrained Expected Improvement."""
        if not hasattr(self, "_gp_obj") or self._gp_obj is None:
            self.fit_constrained_gp()
        if not hasattr(self, "_gp_obj") or self._gp_obj is None:
            return ConstrainedExperimentSuggestion()

        best_obj = -np.inf
        best_suggestion = ConstrainedExperimentSuggestion()

        for mode_idx in range(1, 6):
            lo, hi = MODE_SEGMENTS[mode_idx]
            for x_trial in np.linspace(lo, hi, 8):
                for complexity in [0.2, 0.5, 0.8]:
                    for urgency in [0.3, 0.6]:
                        features = {
                            "complexity": complexity, "urgency": urgency,
                            "risk": 0.3, "ambiguity": 0.3,
                            "time_pressure": 0.5, "cognitive_switch_cost": 0.1,
                        }
                        z = self._build_gp_input(features, x_trial, mode_idx)

                        mu_obj, sigma_obj = self._gp_predict(z, "obj")
                        mu_safety, sigma_safety = self._gp_predict(z, "safety")

                        mu_o = float(mu_obj[0])
                        sig_o = max(float(sigma_obj[0]), 1e-8)
                        mu_s = float(mu_safety[0])
                        sig_s = max(float(sigma_safety[0]), 1e-8)

                        # P[safety >= T]
                        safety_prob = float(norm.cdf((mu_s - safety_threshold) / sig_s))
                        if safety_prob < 0.5:
                            continue

                        # Constrained EI: EI(obj) * P[safety >= T]
                        best_so_far = float(np.max(self._gp_prior_obj)) if len(self._gp_prior_obj) > 0 else 0.5
                        z_score = (mu_o - best_so_far) / sig_o
                        ei = sig_o * (z_score * norm.cdf(z_score) + norm.pdf(z_score))
                        constrained_ei = ei * safety_prob

                        if constrained_ei > best_obj:
                            best_obj = constrained_ei
                            best_suggestion = ConstrainedExperimentSuggestion(
                                suggested_features=features,
                                suggested_mode_idx=mode_idx,
                                suggested_x=round(float(x_trial), 4),
                                expected_obj_improvement=round(float(ei), 4),
                                safety_probability=round(safety_prob, 4),
                                expected_information_gain=round(float(sig_o), 4),
                                rationale=self._build_rationale(
                                    mode_idx, float(x_trial), mu_o, safety_prob, sig_o,
                                ),
                            )

        return best_suggestion

    def _build_rationale(
        self, mode_idx: int, x: float, mu_obj: float,
        safety_prob: float, uncertainty: float,
    ) -> str:
        mode_name = MODE_IDX_TO_COLLAB.get(mode_idx, "unknown")
        parts = [f"建议在 {mode_name} 模式 (x={x:.2f}) 下进行实验"]
        parts.append(f"预测绩效={mu_obj:.3f}, 安全概率={safety_prob:.3f}")
        if uncertainty > 0.1:
            parts.append("该区域不确定性较高，实验信息收益大")
        return "；".join(parts)

    def get_boundary_surface(
        self, resolution: int = 15, safety_threshold: float = 0.95,
    ) -> dict[str, Any]:
        """Export boundary surface data within the safety-feasible region."""
        if not hasattr(self, "_gp_obj") or self._gp_obj is None:
            return {"points": [], "message": "GP not fitted"}

        points = []
        for mode_idx in range(1, 6):
            lo, hi = MODE_SEGMENTS[mode_idx]
            for x_val in np.linspace(lo, hi, resolution):
                for comp in np.linspace(0, 1, resolution):
                    features = {
                        "complexity": comp, "urgency": 0.5, "risk": 0.3,
                        "ambiguity": 0.3, "time_pressure": 0.5,
                        "cognitive_switch_cost": 0.1,
                    }
                    z = self._build_gp_input(features, float(x_val), mode_idx)
                    mu_obj, sig_obj = self._gp_predict(z, "obj")
                    mu_safety, sig_safety = self._gp_predict(z, "safety")

                    safety_prob = float(norm.cdf(
                        (float(mu_safety[0]) - safety_threshold) / max(float(sig_safety[0]), 1e-8)
                    ))

                    points.append({
                        "mode_idx": mode_idx,
                        "x": round(float(x_val), 3),
                        "complexity": round(comp, 3),
                        "predicted_obj": round(float(mu_obj[0]), 4),
                        "uncertainty_obj": round(float(sig_obj[0]), 4),
                        "predicted_safety": round(float(mu_safety[0]), 4),
                        "safety_probability": round(safety_prob, 4),
                        "feasible": safety_prob >= 0.5,
                    })

        return {"points": points, "resolution": resolution, "safety_threshold": safety_threshold}

    def get_uncertainty_map(
        self, dim_x: str = "complexity", dim_y: str = "urgency",
        resolution: int = 15, mode_idx: int = 3,
    ) -> dict[str, Any]:
        """Export dual uncertainty heatmap (objective + safety)."""
        if not hasattr(self, "_gp_obj") or self._gp_obj is None:
            return {"message": "GP not fitted"}

        base = {f: 0.5 for f in FEATURE_NAMES}
        xs = np.linspace(0, 1, resolution).tolist()
        ys = np.linspace(0, 1, resolution).tolist()
        lo, hi = MODE_SEGMENTS.get(mode_idx, (0.3, 0.5))
        x_mid = (lo + hi) / 2

        z_obj_mean, z_obj_std, z_safety_mean, z_safety_std = [], [], [], []

        for y_val in ys:
            row_om, row_os, row_sm, row_ss = [], [], [], []
            for x_val in xs:
                feat = dict(base)
                feat[dim_x] = x_val
                feat[dim_y] = y_val
                z = self._build_gp_input(feat, x_mid, mode_idx)

                mu_o, sig_o = self._gp_predict(z, "obj")
                mu_s, sig_s = self._gp_predict(z, "safety")

                row_om.append(round(float(mu_o[0]), 4))
                row_os.append(round(float(sig_o[0]), 4))
                row_sm.append(round(float(mu_s[0]), 4))
                row_ss.append(round(float(sig_s[0]), 4))

            z_obj_mean.append(row_om)
            z_obj_std.append(row_os)
            z_safety_mean.append(row_sm)
            z_safety_std.append(row_ss)

        return {
            "dim_x": dim_x, "dim_y": dim_y,
            "x_values": xs, "y_values": ys,
            "mode_idx": mode_idx,
            "z_obj_mean": z_obj_mean, "z_obj_std": z_obj_std,
            "z_safety_mean": z_safety_mean, "z_safety_std": z_safety_std,
        }

    def get_mode_similarity_matrix(self) -> dict[str, Any]:
        """Compute empirical 5x5 mode similarity matrix from experiment data."""
        records = self._store.query_all()
        if len(records) < 5:
            return {"matrix": [[0.0] * 5 for _ in range(5)], "message": "insufficient data"}

        mode_perfs: dict[int, list[float]] = defaultdict(list)
        for r in records:
            mode_perfs[r.collaboration_mode_idx].append(r.performance_obj)

        matrix = [[0.0] * 5 for _ in range(5)]
        for i in range(5):
            for j in range(5):
                mi, mj = i + 1, j + 1
                pi = mode_perfs.get(mi, [])
                pj = mode_perfs.get(mj, [])
                if pi and pj:
                    diff = abs(np.mean(pi) - np.mean(pj))
                    matrix[i][j] = round(float(np.exp(-diff * 5)), 3)
                elif i == j:
                    matrix[i][j] = 1.0

        labels = ["autonomous", "supervised", "approve", "plan_execute", "remote_control"]
        return {"matrix": matrix, "labels": labels}

    def _compute_mode_similarity(self, records: list[ExperimentRecord]) -> list[list[float]]:
        result = self.get_mode_similarity_matrix()
        return result["matrix"]


# ── Phase B result models ─────────────────────────────────────────────────────

class ConstrainedGPResult(BaseModel):
    sample_count: int = 0
    obj_noise: float = 0.05
    safety_noise: float = 0.05
    mode_similarity: list[list[float]] = Field(default_factory=lambda: [[0.0] * 5 for _ in range(5)])
    expert_prior_source: str = ""
    safety_threshold: float = 0.95


class ConstrainedExperimentSuggestion(BaseModel):
    suggested_features: dict[str, float] = Field(default_factory=dict)
    suggested_mode_idx: int = 1
    suggested_x: float = 0.0
    expected_obj_improvement: float = 0.0
    safety_probability: float = 0.0
    expected_information_gain: float = 0.0
    rationale: str = ""
