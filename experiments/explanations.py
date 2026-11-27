"""Size and cost of the explanations produced by the hybrid validator, and an impact query.

1. For every element flagged by the SWRL rules of the hybrid validator (TBox without S2) in the
   synthetic benchmark (parts A and B) and in the public corpus, the deployment unit of the element
   is validated on its own and `pellet explain --instance` is run for the inferred classification with Pellet's default settings (one justification; with
   --max > 1 the glass-box method of Pellet 2.3.1 finds one justification per rule but then fails with
   an internal error, 'Explanation contains removed axiom').
   We record the time of the call (JVM start-up included), the number of explanations, and the
   number of axioms of each explanation (rule and facts).
2. On the Compose deployment of eShopOnContainers, the dependsOn relations inferred by D1 and D2
   are used to find, for every microservice, the microservices that depend on it directly or
   transitively; the answer is computed with a SPARQL property path (dependsOn+) over the reasoned
   ontology and checked against a closure computed in Python.

Usage: python explanations.py <corpora_dir>
"""
import json
import re
import statistics
import subprocess
import sys
import tempfile
import time
from collections import defaultdict
from pathlib import Path

from microwl_lib import ROOT, MW, PelletValidator, PREFIX, safe, explain
import benchmark
import real_world

TBOX = ROOT / "ontology" / "microwl-tbox-noS2.owl"
CLASS = {"workload": "InconsistentDeployment", "endpoint": "InconsistentEndpoint",
         "ingress": "InconsistentIngress"}


def by_unit(cfgs):
    units = defaultdict(list)
    for c in cfgs:
        units[c.deployment or c.id].append(c)
    return units


def parse(text):
    """Number of explanations and axioms per explanation of a `pellet explain` output."""
    body = text.split("Explanation(s):", 1)[-1]
    expl, cur = [], None
    for line in body.splitlines():
        if not line.strip():
            continue
        if re.match(r"^\s*\d+\)", line):
            cur = 1
            expl.append(cur)
        elif expl:
            expl[-1] += 1
    return expl


def explain_all(cfgs, tmp, label):
    v = PelletValidator(TBOX)
    rows = []
    for unit, items in by_unit(cfgs).items():
        abox = tmp / f"{safe(unit)[:80]}.owl"
        flagged, _, err = v.validate(items, keep_file=abox)
        if err:
            rows.append(dict(set=label, unit=unit, error=err))
            continue
        rules = dict(v.rules)
        for c in items:
            if c.id not in flagged:
                continue
            # workloads are created in the MW namespace, endpoints and Ingresses in the ontology
            # namespace (IRI ending in '#'), so both forms are tried
            out, dt = None, 0.0
            for base in (MW, MW.rstrip("/") + "#"):
                t0 = time.perf_counter()
                try:
                    out = explain(abox, base + PREFIX[c.kind] + safe(c.id), MW + CLASS[c.kind])
                except subprocess.CalledProcessError:
                    continue
                dt = time.perf_counter() - t0
                break
            if out is None:
                rows.append(dict(set=label, id=c.id, kind=c.kind, error="explain failed"))
                continue
            ex = parse(out)
            rows.append(dict(set=label, id=c.id, kind=c.kind, rules=sorted(rules.get(c.id, [])),
                             seconds=round(dt, 3), n_explanations=len(ex), axioms=ex,
                             elements_in_unit=len(items)))
    return rows


def stats(rows):
    ok = [r for r in rows if "seconds" in r]
    axioms = [a for r in ok for a in r["axioms"]]
    per_rule = defaultdict(list)
    for r in ok:
        for rule in r["rules"]:
            per_rule[rule] += r["axioms"]
    return dict(elements=len(ok), errors=len(rows) - len(ok),
                explained=sum(1 for r in ok if r["n_explanations"] > 0),
                explanations_per_element=dict(min=min(r["n_explanations"] for r in ok),
                                              max=max(r["n_explanations"] for r in ok)),
                axioms=dict(min=min(axioms), median=statistics.median(axioms), max=max(axioms)),
                axioms_by_rule={k: dict(min=min(v), max=max(v)) for k, v in sorted(per_rule.items())},
                seconds=dict(mean=round(statistics.mean(r["seconds"] for r in ok), 3),
                             sd=round(statistics.pstdev(r["seconds"] for r in ok), 3),
                             max=round(max(r["seconds"] for r in ok), 3)))


def impact(tmp):
    """Microservices affected, directly or transitively, by the failure of each microservice of eShop."""
    from rdflib import URIRef
    units = [u for u in real_world.app_compose_units() if "eShop" in str(u[0])]
    f, root, doc = units[0]
    cfgs = real_world.compose_to_configs(f, root, doc)
    v = PelletValidator(TBOX)
    v.validate(cfgs)
    pairs = v.depends_on
    # closure in Python
    rev = defaultdict(set)
    for a, b in pairs:
        rev[b].add(a)
    closure = {}
    for m in {b for _, b in pairs}:
        seen, todo = set(), [m]
        while todo:
            for x in rev[todo.pop()]:
                if x not in seen:
                    seen.add(x)
                    todo.append(x)
        closure[m] = seen
    # the same answer through SPARQL over the reasoned ontology
    g = v.world.as_rdflib_graph()
    names = {f"{PREFIX[c.kind]}{safe(c.id)}": c.id for c in cfgs}
    sparql = {}
    for m in closure:
        q = f"""SELECT ?x WHERE {{ ?x <{MW}dependsOn>+ <{MW}{PREFIX['workload']}{safe(m)}> }}"""
        sparql[m] = {names[str(r[0]).rsplit('/', 1)[-1].split('#')[-1]] for r in g.query(q)
                     if str(r[0]).rsplit('/', 1)[-1].split('#')[-1] in names}
    short = lambda i: i.split("::")[-1]
    top = sorted(closure, key=lambda m: -len(closure[m]))
    return dict(microservices=len([c for c in cfgs if c.kind == "workload"]), depends_on=len(pairs),
                sparql_equals_closure=all(sparql[m] == closure[m] for m in closure),
                direct={short(m): sorted(short(x) for x in rev[m]) for m in top},
                affected={short(m): sorted(short(x) for x in closure[m]) for m in top},
                query="SELECT ?x WHERE { ?x mw:dependsOn+ mw:<failed microservice> }")


def main():
    tmp = Path(tempfile.mkdtemp())
    rows = explain_all(benchmark.generate() + [c for _, _, it in benchmark.generate_deployments() for c in it],
                       tmp, "benchmark")
    corpora = real_world.load()
    for name, cfgs in corpora.items():
        rows += explain_all(cfgs, tmp, name)
    bench = [r for r in rows if r["set"] == "benchmark"]
    corpus = [r for r in rows if r["set"] != "benchmark"]
    res = dict(benchmark=stats(bench), corpus=stats(corpus), impact_eshop=impact(tmp),
               examples={r["id"]: r for r in corpus if "assurance" in r.get("id", "")}, rows=rows)
    with open(ROOT / "results" / "explanations.json", "w") as f:
        json.dump(res, f, indent=1)
    print(json.dumps({k: res[k] for k in ("benchmark", "corpus")}, indent=1))
    print(json.dumps({k: v for k, v in res["impact_eshop"].items() if k != "direct"}, indent=1))


if __name__ == "__main__":
    main()
