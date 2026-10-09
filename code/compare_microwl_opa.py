#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Comparison of MicrOWL + Pellet vs. OPA/Rego on the same benchmark.

This script:
  1. Reuses the benchmark generator from benchmark_microwl.py.
  2. Runs Pellet on each configuration (MicrOWL) and records detection.
  3. Serialises each configuration as OPA input JSON and runs OPA on it.
  4. Computes precision/recall/F1/FPR for both systems.
  5. Reports mean execution time and authoring LOC for both.

Requirements:
    pip install owlready2 pandas requests
    OPA binary available in PATH (https://www.openpolicyagent.org/docs/latest/#running-opa)

Usage:
    python compare_microwl_opa.py \\
        --ontology microwl.owl \\
        --rego     rego_policies/microwl.rego \\
        --output   results/opa_comparison/
"""

import argparse
import json
import os
import statistics
import subprocess
import time
from pathlib import Path

import pandas as pd
import requests
from owlready2 import get_ontology, sync_reasoner_pellet

# Reuse the benchmark generator
from benchmark_microwl import (
    generate_benchmark, N_TOTAL, CATEGORIES,
    compute_metrics, instantiate_config, run_reasoner,
    query_inconsistencies, DeploymentConfig
)
from generate_rego import compute_swrl_loc, compute_rego_loc


# =============================================================================
# OPA SERVER MANAGEMENT
# =============================================================================

class OPAServer:
    """Manage an OPA server process and query it over HTTP."""

    def __init__(self, policy_path: Path, port: int = 8181):
        self.policy_path = policy_path
        self.port = port
        self.proc = None

    def start(self):
        """Start OPA as a server loaded with the policy file."""
        cmd = [
            "opa", "run",
            "--server",
            f"--addr=localhost:{self.port}",
            str(self.policy_path),
        ]
        self.proc = subprocess.Popen(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        # Wait for OPA to become responsive
        deadline = time.time() + 15
        while time.time() < deadline:
            try:
                r = requests.get(
                    f"http://localhost:{self.port}/health", timeout=1)
                if r.status_code == 200:
                    print(f"[OK] OPA server ready on port {self.port}")
                    return
            except requests.RequestException:
                time.sleep(0.3)
        raise RuntimeError("OPA server did not become ready in time.")

    def query(self, doc_path: str, input_obj: dict) -> dict:
        """
        Query an OPA document with the given input.

        doc_path: e.g. "microwl/deny" or "microwl/reasons"
        """
        url = f"http://localhost:{self.port}/v1/data/{doc_path}"
        r = requests.post(url, json={"input": input_obj}, timeout=5)
        r.raise_for_status()
        return r.json().get("result")

    def stop(self):
        if self.proc:
            self.proc.terminate()
            try:
                self.proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                self.proc.kill()


# =============================================================================
# CONFIG -> OPA INPUT
# =============================================================================

def config_to_opa_input(cfg: DeploymentConfig) -> dict:
    """Convert a DeploymentConfig into the JSON input expected by the Rego policy."""
    obj = {
        "microservice": f"ms_{cfg.cfg_id}",
        "replicas": cfg.replicas,
        "image": cfg.image,
        "containerPort": cfg.container_port,
        "exposedPort": cfg.exposed_port,
        "storageSize": cfg.storage_size,
        "accessMode": cfg.access_mode,
    }
    if cfg.service_type in ("NodePort", "LoadBalancer", "ClusterIP"):
        obj["kubernetes"] = {"serviceType": cfg.service_type}
        if cfg.node_port is not None:
            obj["kubernetes"]["nodePort"] = cfg.node_port
    return obj


# =============================================================================
# MICROWL RUNNER
# =============================================================================

def run_microwl(benchmark, ontology_path, output_dir):
    """Run MicrOWL + Pellet over the benchmark and return per-config results."""
    print("[MicrOWL] Loading ontology...")
    onto = get_ontology(f"file://{os.path.abspath(ontology_path)}").load()

    rows = []
    for i, cfg in enumerate(benchmark):
        # Clear previous ABox entries
        with onto:
            for ind in list(onto.individuals()):
                if ind.name.startswith(("ms_", "ws_", "cs_", "ns_", "ss_",
                                        "docker_", "k8s_", "ecs_", "svc_", "np_")):
                    ind.destroy()

        instantiate_config(onto, cfg)
        try:
            elapsed = run_reasoner(onto)
            detected_names = query_inconsistencies(onto)
            detected = f"ms_{cfg.cfg_id}" in detected_names
        except Exception as e:
            print(f"  [WARN] Pellet failed on {cfg.cfg_id}: {e}")
            elapsed, detected = 0.0, False

        rows.append({
            "cfg_id": cfg.cfg_id,
            "expected_misconfig": cfg.expected_misconfig or "none",
            "microwl_detected": detected,
            "microwl_time_s": round(elapsed, 4),
        })
        if (i + 1) % 25 == 0:
            print(f"  ... MicrOWL {i + 1}/{len(benchmark)}")

    df = pd.DataFrame(rows)
    df.to_csv(Path(output_dir) / "microwl_results.csv", index=False)
    return df


# =============================================================================
# OPA RUNNER
# =============================================================================

def run_opa(benchmark, rego_path, output_dir):
    """Run OPA/Rego over the benchmark and return per-config results."""
    print("[OPA] Starting OPA server...")
    opa = OPAServer(rego_path)
    opa.start()

    rows = []
    try:
        for i, cfg in enumerate(benchmark):
            payload = config_to_opa_input(cfg)
            t0 = time.perf_counter()
            try:
                deny = opa.query("microwl/deny", payload)
                reasons = opa.query("microwl/reasons", payload) or []
                detected = bool(deny)
            except requests.RequestException as e:
                print(f"  [WARN] OPA query failed on {cfg.cfg_id}: {e}")
                detected, reasons = False, []
            elapsed = time.perf_counter() - t0

            rows.append({
                "cfg_id": cfg.cfg_id,
                "expected_misconfig": cfg.expected_misconfig or "none",
                "opa_detected": detected,
                "opa_reasons": ";".join(sorted(reasons)) if reasons else "",
                "opa_time_s": round(elapsed, 5),
            })
            if (i + 1) % 25 == 0:
                print(f"  ... OPA {i + 1}/{len(benchmark)}")
    finally:
        opa.stop()

    df = pd.DataFrame(rows)
    df.to_csv(Path(output_dir) / "opa_results.csv", index=False)
    return df


# =============================================================================
# COMPARISON
# =============================================================================

def build_comparison(benchmark, microwl_df, opa_df):
    """Merge the two data frames and compute metrics for both systems."""
    merged = microwl_df.merge(opa_df, on=["cfg_id", "expected_misconfig"])

    results = []
    for label, col in [("MicrOWL+Pellet", "microwl_detected"),
                       ("OPA/Rego", "opa_detected")]:
        detected_ms = {f"ms_{cid}"
                       for cid, d in zip(merged["cfg_id"], merged[col])
                       if d}
        per_cat = []
        for cat in list(CATEGORIES) + [None]:
            m = compute_metrics(benchmark, detected_ms, category=cat)
            per_cat.append(m)
        overall = per_cat[-1]
        mean_time = merged[f"{'microwl' if 'MicrOWL' in label else 'opa'}_time_s"].mean()
        results.append({
            "system": label,
            "precision": overall["precision"],
            "recall": overall["recall"],
            "f1": overall["f1"],
            "fpr": overall["fpr"],
            "mean_time_s": round(mean_time, 5),
        })
    return pd.DataFrame(results)


# =============================================================================
# MAIN
# =============================================================================

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--ontology", default="microwl.owl")
    parser.add_argument("--rego", default="rego_policies/microwl.rego")
    parser.add_argument("--output", default="results/opa_comparison/")
    args = parser.parse_args()

    output_dir = Path(args.output)
    output_dir.mkdir(parents=True, exist_ok=True)

    # 1. Generate benchmark
    print(f"[1/5] Generating benchmark of {N_TOTAL} configurations...")
    benchmark = generate_benchmark()

    # 2. Run MicrOWL
    print("[2/5] Running MicrOWL + Pellet ...")
    microwl_df = run_microwl(benchmark, args.ontology, output_dir)

    # 3. Run OPA
    print("[3/5] Running OPA/Rego ...")
    opa_df = run_opa(benchmark, Path(args.rego), output_dir)

    # 4. Build comparison
    print("[4/5] Building comparison ...")
    comparison = build_comparison(benchmark, microwl_df, opa_df)
    comparison.to_csv(output_dir / "comparison_summary.csv", index=False)

    # 5. Report authoring effort
    swrl_loc = compute_swrl_loc()
    rego_loc = compute_rego_loc()
    print("\n==== Authoring-effort comparison ====")
    print(f"SWRL rules : {swrl_loc} effective lines")
    print(f"Rego policy: {rego_loc} effective lines")
    print(f"Reduction  : {(1 - swrl_loc / rego_loc) * 100:.1f}% fewer lines in SWRL")

    # 6. Print detection comparison
    print("\n==== Detection performance comparison ====")
    print(comparison.to_string(index=False))
    print(f"\nOutput written to: {output_dir.resolve()}")


if __name__ == "__main__":
    main()
