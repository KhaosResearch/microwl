"""Do existing Kubernetes linters detect the misconfigurations targeted by MicrOWL?

For each faulty manifest in linter_cases/ (F1-F5 and F7-F16, one per fault category M1-M5 and
M7-M16) and its valid control, run
kubeconform (schema), kube-linter, kube-score and Checkov with their default
rule sets, the Kubernetes API server (server-side dry run, and a real apply into a fresh namespace)
when a cluster context is given, plus the MicrOWL translator + hybrid validator. A tool is considered
to detect a fault when it reports at least one finding on the faulty manifest
that it does not report on the corresponding valid control.

Usage: python linters.py <checkov_executable> [<kube_context>]
The API-server baselines need a disposable cluster, e.g. `kind create cluster --name microwl`
(context kind-microwl). The real apply creates and deletes a namespace per manifest.
"""
import json
import re
import subprocess
import sys
import tempfile
from pathlib import Path

import microwl_lib
from microwl_lib import ROOT, hybrid_validate
from translate import k8s_group_to_configs, load_k8s_docs

CASES = ROOT / "experiments" / "linter_cases"
CHECKOV = sys.argv[1] if len(sys.argv) > 1 else "checkov"
KUBE = sys.argv[2] if len(sys.argv) > 2 else None
PAIRS = {"F1_rwo_replicas": "valid", "F2_targetport": "valid", "F3_nodeport_low": "valid_nodeport",
         "F4_nodeport_high": "valid_nodeport", "F5_rwo_zero": "valid",
         "F7_hostport_replicas": "valid_app", "F8_duplicate_nodeport": "valid_app_nodeport",
         "F9_dependency_port": "valid_app", "F10_dependency_name": "valid_app", "F11_latest_tag": "valid_app",
         "F12_dangling_service": "valid_app", "F13_ingress_backend": "valid_app", "F14_probe_port": "valid_app",
         "F15_rwop_replicas": "valid_app", "F16_no_memory_limit": "valid_app"}


def run(cmd):
    return subprocess.run(cmd, capture_output=True, text=True)


def findings_kubeconform(f):
    out = run(["kubeconform", "-strict", "-summary", "-output", "json", str(f)]).stdout
    try:
        res = json.loads(out).get("resources", [])
    except json.JSONDecodeError:
        return {out.strip()[:80]}
    return {f"{r.get('kind')}:{r.get('msg')}" for r in res if r.get("status") not in ("statusValid", "valid")}


def findings_kubelinter(f):
    out = run(["kube-linter", "lint", "--format", "json", str(f)]).stdout
    try:
        reps = json.loads(out).get("Reports") or []
    except json.JSONDecodeError:
        return {out.strip()[:80]}
    return {r["Check"] for r in reps}


def findings_kubescore(f):
    out = run(["kube-score", "score", "--output-format", "json", str(f)]).stdout
    found = set()
    try:
        for obj in json.loads(out):
            for chk in obj.get("checks", []):
                if chk.get("grade", 10) < 10 and not chk.get("skipped"):
                    found.add(f"{obj['object_name']}:{chk['check']['id']}")
    except json.JSONDecodeError:
        found.add(out.strip()[:80])
    return found


def findings_checkov(f):
    out = run([CHECKOV, "-f", str(f), "--framework", "kubernetes", "-o", "json", "--quiet", "--compact"]).stdout
    try:
        data = json.loads(out)
    except json.JSONDecodeError:
        return set()
    data = data if isinstance(data, list) else [data]
    found = set()
    for d in data:
        for r in (d.get("results") or {}).get("failed_checks", []):
            found.add(f"{r['check_id']}:{r.get('resource')}")
    return found


def _api_errors(out):
    """Error lines of kubectl, without the namespace-dependent parts."""
    return {re.sub(r'namespace "[^"]*"', 'namespace', l.strip()) for l in out.splitlines()
            if l.strip() and not re.search(r" (created|configured|unchanged)( \(server dry run\))?$", l)}


def findings_apiserver_dryrun(f):
    ns = "microwl-lint"
    run(["kubectl", "--context", KUBE, "create", "namespace", ns])
    r = run(["kubectl", "--context", KUBE, "apply", "--dry-run=server", "-n", ns, "-f", str(f)])
    return _api_errors(r.stdout + r.stderr)


def findings_apiserver_apply(f):
    ns = "microwl-apply"
    run(["kubectl", "--context", KUBE, "create", "namespace", ns])
    r = run(["kubectl", "--context", KUBE, "apply", "-n", ns, "-f", str(f)])
    run(["kubectl", "--context", KUBE, "delete", "namespace", ns, "--wait"])
    return _api_errors(r.stdout + r.stderr)


def findings_microwl(f):
    cfgs = k8s_group_to_configs(load_k8s_docs(f), "app")
    hybrid_validate(cfgs, Path(tempfile.mkdtemp()))
    return {f"{r}:{c}" for c, rules in microwl_lib.HYBRID_RULES.items() for r in rules}


TOOLS = {"kubeconform": findings_kubeconform, "kube-linter": findings_kubelinter,
         "kube-score": findings_kubescore, "checkov": findings_checkov}
if KUBE:
    TOOLS.update({"API server (dry run)": findings_apiserver_dryrun, "API server (apply)": findings_apiserver_apply})
TOOLS["MicrOWL (hybrid)"] = findings_microwl


def main():
    versions = {
        "kubeconform": run(["kubeconform", "-v"]).stdout.strip(),
        "kube-linter": run(["kube-linter", "version"]).stdout.strip(),
        "kube-score": run(["kube-score", "version"]).stdout.strip().split(",")[0],
        "checkov": run([CHECKOV, "--version"]).stdout.strip(),
    }
    if KUBE:
        versions["kubernetes"] = json.loads(run(["kubectl", "--context", KUBE, "version", "-o", "json"]).stdout)["serverVersion"]["gitVersion"]
    cache = {}
    res = {"versions": versions, "detection": {}, "new_findings": {}}
    for tool, fn in TOOLS.items():
        for case, ctrl in PAIRS.items():
            for name in (case, ctrl):
                if (tool, name) not in cache:
                    cache[(tool, name)] = fn(CASES / f"{name}.yaml")
            new = sorted(cache[(tool, case)] - cache[(tool, ctrl)])
            res["detection"].setdefault(tool, {})[case] = bool(new)
            res["new_findings"].setdefault(tool, {})[case] = new
    with open(ROOT / "results" / "linters.json", "w") as f:
        json.dump(res, f, indent=1)
    print(json.dumps(res["detection"], indent=1))
    print(json.dumps(res["new_findings"], indent=1))


if __name__ == "__main__":
    main()
