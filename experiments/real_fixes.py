"""Recall of MicrOWL on real fix commits (RQ2).

Input: results/real_fixes_candidates.json, a list of GitHub commits that fix one fault of the
fifteen categories in a plain Kubernetes manifest or Docker Compose file (collected with targeted
commit searches and checked manually; see the paper). For every commit, the descriptors of its
deployment unit are downloaded at the parent commit (faulty version) and at the commit (fixed
version) with the GitHub API, translated, and validated with the hybrid validator. A fix is
detected when the constraint that targets its category is reported on the faulty version and is
reported on fewer elements of the fixed version.

Downloaded files are only parsed (yaml.safe_load); nothing from the repositories is executed.

Entries with "source": "random sample" are the in-scope commits of a random sample of fix commits
(results/real_fixes_coverage.json); the others come from the targeted searches. Entries with
"recursive": true take the YAML files of the subdirectories of `dir` as well.

Usage: python real_fixes.py <download_dir>
"""
import json
import subprocess
import sys
import tempfile
from collections import Counter, defaultdict
from pathlib import Path

import yaml

import microwl_lib
from microwl_lib import ROOT, hybrid_validate
from translate import k8s_group_to_configs, load_k8s_docs, compose_to_configs, is_templated

DL = Path(sys.argv[1]) if len(sys.argv) > 1 else Path(tempfile.mkdtemp())
TARGET = {"M1": "S1", "M2": "S2c", "M3": "S3", "M4": "S3", "M5": "S5", "M7": "S7", "M8": "S8", "M9": "S9",
          "M10": "S10", "M11": "S11", "M12": "S12", "M13": "S13", "M14": "S14", "M15": "S15", "M16": "S16"}
YAML = (".yaml", ".yml")
# Manual review of every commit that the automatic criterion does not count as detected
# (commit prefix -> cause). Causes:
#   residual        the fault is reported on the faulty version; the fixed version still has another fault
#                   of the same category on the same element, which MicrOWL keeps reporting
#   named-target    a Service targetPort names a port that no container declares; the translator leaves
#                   the target unresolved, so S2c has nothing to compare
#   service-only    the out-of-range nodePort is on a Service that selects no workload of the unit;
#                   S3 is checked on the ports of the workloads
#   not-declared    the port the container listens on is set in the image, not in the descriptor
#   placeholder     the descriptor has unresolved placeholders (${VAR}, %VAR%, <value>): unparsable YAML,
#                   or a non-numeric value in a numeric field that makes the ABox inconsistent
#   bare-pod        the unit has only bare Pods, which the translator does not take as workloads
REVIEW = {"52bea6a6": "residual", "926c02e8": "residual", "a4dd13e6": "residual",
          "1f6724cb": "named-target", "3e85624c": "named-target", "16786c3e": "named-target",
          "fa10b9bf": "service-only", "cb346c10": "service-only",
          "e9a12644": "not-declared", "c782928a": "not-declared",
          "8d398ee7": "placeholder", "539e3494": "placeholder", "51550d1f": "placeholder",
          "3a3d43b4": "bare-pod"}


def gh(path, raw=False):
    cmd = ["gh", "api", path] + (["-H", "Accept: application/vnd.github.raw"] if raw else [])
    r = subprocess.run(cmd, capture_output=True, text=not raw)
    if r.returncode != 0:
        return None
    return r.stdout if raw else json.loads(r.stdout)


def list_yaml(repo, ref, directory, recursive):
    listing = gh(f"repos/{repo}/contents/{directory}?ref={ref}") if directory not in ("", ".") else \
        gh(f"repos/{repo}/contents?ref={ref}")
    names = []
    for x in listing or []:
        if not isinstance(x, dict):
            continue
        if x.get("type") == "file" and x["path"].endswith(YAML):
            names.append(x["path"])
        elif recursive and x.get("type") == "dir":
            names += list_yaml(repo, ref, x["path"], recursive)
    return names


def fetch_unit(repo, ref, directory, files, platform, recursive=False):
    """Download the descriptors of the deployment unit at `ref`; returns local paths."""
    base = DL / repo.replace("/", "__") / ref
    if platform == "kubernetes":
        names = list_yaml(repo, ref, directory, recursive)
    else:
        names = list(files)
    out = []
    for p in names:
        local = base / p
        if not local.exists():
            data = gh(f"repos/{repo}/contents/{p}?ref={ref}", raw=True)
            if data is None:
                continue
            local.parent.mkdir(parents=True, exist_ok=True)
            local.write_bytes(data)
        out.append(local)
    return out


def translate(paths, platform, source):
    if platform == "kubernetes":
        docs = [d for f in paths for d in load_k8s_docs(f)]
        return k8s_group_to_configs(docs, source, incomplete=any(is_templated(f) for f in paths))
    cfgs = []
    for f in paths:
        cfgs += compose_to_configs(f, f.parent)
    return cfgs


def violations(cfgs, constraint, tmp):
    if not cfgs:
        return None
    hybrid_validate(cfgs, tmp)
    return sorted(k for k, r in microwl_lib.HYBRID_RULES.items() if constraint in r)


def main():
    cands = json.load(open(ROOT / "results" / "real_fixes_candidates.json"))
    tmp = Path(tempfile.mkdtemp())
    rows = []
    for c in cands:
        cat = c["category"].split("/")[0]
        constraint = TARGET[cat]
        res = dict(repo=c["repo"], sha=c["sha"], category=cat, constraint=constraint, platform=c["platform"],
                   source=c.get("source", "targeted search"))
        try:
            before = translate(fetch_unit(c["repo"], c["parent"], c["dir"], c["files"], c["platform"],
                                         c.get("recursive", False)), c["platform"], f"{c['repo']}@parent")
            after = translate(fetch_unit(c["repo"], c["sha"], c["dir"], c["files"], c["platform"],
                                        c.get("recursive", False)), c["platform"], f"{c['repo']}@fix")
            vb, va = violations(before, constraint, tmp), violations(after, constraint, tmp)
        except Exception as e:  # unparsable descriptors, API errors
            res.update(outcome="error", error=str(e)[:200], review=REVIEW.get(c["sha"][:8], "not reviewed"))
            rows.append(res)
            print(res, flush=True)
            continue
        res.update(elements_before=len(before), elements_after=len(after),
                   flagged_before=len(vb or []), flagged_after=len(va or []))
        if vb is None:
            res["outcome"] = "not translated"
        elif vb and len(va or []) < len(vb):
            res["outcome"] = "detected"
        else:
            res["outcome"] = "missed"
        if res["outcome"] != "detected":
            res["review"] = REVIEW.get(c["sha"][:8], "not reviewed")
        rows.append(res)
        print(res, flush=True)
    summary = {}
    for src in sorted({r["source"] for r in rows}):
        sub = [r for r in rows if r["source"] == src]
        by_cat = defaultdict(Counter)
        for r in sub:
            by_cat[r["category"]][r["outcome"]] += 1
        summary[src] = dict(n=len(sub), outcomes=dict(Counter(r["outcome"] for r in sub)),
                            reported_on_faulty_version=sum(1 for r in sub if r.get("flagged_before")),
                            review=dict(Counter(r["review"] for r in sub if "review" in r)),
                            by_category={k: dict(v) for k, v in
                                         sorted(by_cat.items(), key=lambda x: int(x[0][1:]))})
    with open(ROOT / "results" / "real_fixes.json", "w") as f:
        json.dump(dict(summary=summary, commits=rows), f, indent=1)
    print(json.dumps(summary, indent=1))


if __name__ == "__main__":
    main()
