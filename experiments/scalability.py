"""Scalability study (RQ5): validation time as a function of the number of
microservices described in a single ABox.

Part A samples single-microservice configurations; part B samples deployment
units of part B of the benchmark (three microservices with their endpoints,
dependencies and, on Kubernetes, possibly an Ingress), which exercise the
deployment-level rules. For part B, the hybrid validator is also timed on chunks
of whole deployment units, and with its closed-world shapes resolving dependency
names in SPARQL over the asserted ABox instead of using the resolvesTo relations
inferred by Pellet (ablation of this design decision).

For N in SIZES, N configurations are sampled (seed 42) from the synthetic
benchmark generator, given unique identifiers, and validated in one batch with
Pellet, the MicrOWL hybrid validator, the equivalent SHACL shape set (only up to
SHACL_MAX microservices, because pySHACL grows super-linearly with it, mainly
because of S8) and OPA. Each size is repeated REPS times. Pellet times include
the Owlready2 export and the JVM start-up, as experienced by a user.

Usage: python scalability.py
"""
import copy
import json
import random
import statistics
import tempfile
from pathlib import Path

import rdflib

from benchmark import generate, generate_deployments
from microwl_lib import (PelletValidator, shacl_validate, opa_validate, opa_closed, hybrid_validate,
                         hybrid_validate_by_unit, hybrid_validate_asserted, ROOT)

SIZES = [10, 50, 100, 250, 500, 1000, 2000]
REPS = 3
SHACL_MAX = 500


def sample(n, seed):
    pool = generate()
    rnd = random.Random(seed)
    out = []
    for i in range(n):
        c = copy.deepcopy(rnd.choice(pool))
        c.id = f"s{i:05d}_{c.id}"
        out.append(c)
    return out


def sample_units(n, seed):
    """Deployment units with n microservices in total (three per unit), with unique identifiers."""
    pool = generate_deployments()
    rnd = random.Random(seed)
    out = []
    for i in range((n + 2) // 3):
        _, _, items = rnd.choice(pool)
        for c in copy.deepcopy(items):
            c.id = f"u{i:05d}_{c.id}"
            c.deployment = f"u{i:05d}_{c.deployment}"
            c.routes_to = [f"u{i:05d}_{r}" for r in c.routes_to]
            out.append(c)
    return out


def part_b():
    v = PelletValidator(ROOT / "ontology" / "microwl-tbox.owl")
    tmp = Path(tempfile.mkdtemp())
    rows = []
    for n in SIZES:
        tp, ts, to, th, tu, ta = [], [], [], [], [], []
        triples = elements = None
        for rep in range(REPS):
            cfgs = sample_units(n, 42 + rep)
            abox = tmp / f"b{n}.owl"
            _, t, err = v.validate(cfgs, keep_file=abox)
            assert err is None, err
            tp.append(t)
            if n <= SHACL_MAX:
                ts.append(shacl_validate(abox)[1])
            to.append(opa_closed(cfgs)[1])
            fl_bulk, t_bulk = hybrid_validate(cfgs, tmp)
            fl_unit, t_unit = hybrid_validate_by_unit(cfgs, tmp)
            fl_asserted, t_asserted = hybrid_validate_asserted(cfgs, tmp)
            assert fl_bulk == fl_unit == fl_asserted
            th.append(t_bulk)
            tu.append(t_unit)
            ta.append(t_asserted)
            if triples is None:
                g = rdflib.Graph()
                g.parse(str(abox), format="xml")
                triples, elements = len(g), len(cfgs)
        m = lambda xs: round(statistics.mean(xs), 3) if xs else None
        row = dict(n=n, elements=elements, abox_triples=triples, pellet_mean_s=m(tp), hybrid_mean_s=m(th),
                   hybrid_by_unit_mean_s=m(tu), hybrid_asserted_mean_s=m(ta), shacl_mean_s=m(ts),
                   opa_closed_mean_s=m(to))
        rows.append(row)
        print("B", row, flush=True)
    return rows


def main():
    rows_b = part_b()
    v = PelletValidator(ROOT / "ontology" / "microwl-tbox.owl")
    tmp = Path(tempfile.mkdtemp())
    rows = []
    for n in SIZES:
        tp, ts, to, th = [], [], [], []
        triples = None
        for rep in range(REPS):
            cfgs = sample(n, 42 + rep)
            abox = tmp / f"n{n}.owl"
            flagged, t, err = v.validate(cfgs, keep_file=abox)
            assert err is None, err
            tp.append(t)
            if n <= SHACL_MAX:
                ts.append(shacl_validate(abox)[1])
            to.append(opa_validate(cfgs)[1])
            th.append(hybrid_validate(cfgs, tmp)[1])
            if triples is None:
                g = rdflib.Graph()
                g.parse(str(abox), format="xml")
                triples = len(g)
        row = dict(n=n, abox_triples=triples,
                   pellet_mean_s=round(statistics.mean(tp), 3), pellet_sd_s=round(statistics.pstdev(tp), 3),
                   hybrid_mean_s=round(statistics.mean(th), 3), hybrid_sd_s=round(statistics.pstdev(th), 3),
                   shacl_mean_s=round(statistics.mean(ts), 3) if ts else None,
                   shacl_sd_s=round(statistics.pstdev(ts), 3) if ts else None,
                   opa_mean_s=round(statistics.mean(to), 3), opa_sd_s=round(statistics.pstdev(to), 3))
        rows.append(row)
        print(row, flush=True)
    with open(ROOT / "results" / "scalability.json", "w") as f:
        json.dump({"part_a": rows, "part_b": rows_b}, f, indent=1)


if __name__ == "__main__":
    main()
