"""Synthetic benchmark (RQ1) and engine comparison (RQ3) for MicrOWL.

Part A: 250 single-microservice configurations generated from the three case studies (seed 42):
  * 150 valid configurations, 75 of which are *hard negatives* that sit next to
    a rule boundary and must NOT be flagged:
      HN-remap   (20) exposed port != container port, target port == container port
      HN-perrep  (15) ReadWriteOnce per-replica storage with replicas > 1
      HN-bound   (15) NodePort exactly 30000 or 32767
      HN-single  (10) shared ReadWriteOnce storage with exactly one replica
      HN-multi   (15) container with two ports, traffic forwarded to one of them
  * 100 misconfigured configurations, 20 per category, one fault each:
      M1 replicas > 1 with shared ReadWriteOnce storage   (S1)
      M2 target port != container port                    (S2)
      M3 NodePort < 30000                                 (S3a + S4)
      M4 NodePort > 32767                                 (S3b + S4)
      M5 ReadWriteOnce storage with zero replicas         (S5)

Part B: 335 deployment units of three microservices (the three case studies, with the
endpoints through which they are reached, the dependencies of MyWebApp on the other two, and
optionally an Ingress), generated with seed 43 for the deployment-level constraints:
  * 135 valid units, 85 of which are hard negatives:
      HN-hostport   (10) host port with a single replica or a DaemonSet (no replica count)
      HN-nodeport   (10) two endpoints with different NodePorts
      HN-svcport    (15) dependency on the Service port of a remapped Service (port 80, targetPort 8080)
      HN-external   (10) dependency on an endpoint without selector (e.g. ExternalName)
      HN-latest     (10) image without tag and a single replica
      HN-probemulti (10) probe on the second port of a two-port container
      HN-rwop       (10) ReadWriteOncePod per-replica storage with several replicas
      HN-portname   (10) Ingress backend that refers to the Service port by name
  * 200 misconfigured units, 20 per category, one fault each:
      M7  several replicas with a fixed host port                       (S7)
      M8  two endpoints with the same NodePort / two Compose services
          publishing the same host port                                 (S8a / S8b)
      M9  dependency on a port that the endpoint does not serve         (S9)
      M10 dependency on a name that no endpoint of the unit provides    (S10)
      M11 several replicas and an image without tag                     (S11)
      M12 endpoint whose selector matches no workload                   (S12)
      M13 Ingress backend with a wrong Service name or port             (S13)
      M14 probe on a port that is not a container port                  (S14)
      M15 several replicas sharing a ReadWriteOncePod volume            (S15)
      M16 workload without memory limit                                 (S16)
Detection is measured per deployment unit (a configuration of part A is a unit of its own):
a unit is flagged when any of its elements is flagged.

Every configuration is validated with (a) MicrOWL + Pellet, (b) equivalent
SHACL shapes (pySHACL) over the same asserted ABox, (c) the equivalent Rego policy
with OPA, (d)/(e) closed-world SHACL and Rego variants in which S2 is replaced by
S2c (target port not among the container ports), (f) the MicrOWL hybrid validator
(Pellet for the SWRL rules + SHACL for the closed-world constraints) and (g) the ablated
variant of the knowledge base (microwl-ablated.owl, see build_microwl.py; part A only).
Per-configuration validation (one process call per configuration of part A) and batch
validation (the 250 configurations of part A at once) are both timed. The verdicts on parts
A and B are obtained with the SHACL shapes evaluated on chunks of whole deployment units,
because pySHACL grows super-linearly with the size of the data graph.

Usage: python benchmark.py [--per-config-reps 1]
"""
import argparse
import json
import random
import statistics
import tempfile
import time
from pathlib import Path

from microwl_lib import (Config, Port, Storage, Dependency, PelletValidator, PelletValidatorAblated,
                         shacl_validate, shacl_rules_chunked, opa_validate, opa_rules, opa_closed,
                         hybrid_validate, hybrid_rules_chunked, make_tbox, SHAPES, SHAPES_CLOSED, SHAPES_HYBRID, REGO, ROOT)
from translate import image_tag

SEED = 42
BASES = {
    "MyWebApp": dict(image="nginx:alpine", port=80, storage=("ReadWriteMany", "10Gi"), memory="256Mi"),
    "PaymentProcessor": dict(image="payment-processor:2.1.0", port=8080, storage=("ReadWriteOnce", "5Gi"),
                             memory="512Mi"),
    "DataAggregator": dict(image="data-aggregator:1.0.0", port=9090, storage=("ReadWriteMany", "20Gi"),
                           memory="1Gi"),
}
CATS_A = ["M1", "M2", "M3", "M4", "M5"]
CATS_B = ["M7", "M8", "M9", "M10", "M11", "M12", "M13", "M14", "M15", "M16"]
CATS = CATS_A + CATS_B
EXPECTED_RULE = {"M1": "S1", "M2": "S2c", "M3": "S3", "M4": "S3", "M5": "S5", "M7": "S7", "M8": "S8",
                 "M9": "S9", "M10": "S10", "M11": "S11", "M12": "S12", "M13": "S13", "M14": "S14",
                 "M15": "S15", "M16": "S16"}
HN_B = ["HN-hostport", "HN-nodeport", "HN-svcport", "HN-external", "HN-latest", "HN-probemulti", "HN-rwop",
        "HN-portname"]


def generate(seed=SEED):
    rnd = random.Random(seed)
    cfgs = []

    def base(i, kind):
        name = rnd.choice(list(BASES))
        b = BASES[name]
        platform = "kubernetes" if kind in ("M3", "M4", "HN-bound") else rnd.choice(["kubernetes", "compose", "ecs"])
        port = b["port"]
        c = Config(id=f"{kind}_{i:03d}", platform=platform, replicas=rnd.randint(1, 5), image=b["image"],
                   container_ports=[port], ports=[Port(exposed=port, target=port)], source=name,
                   image_tag=image_tag(b["image"]), memory_limit=b["memory"])
        mode, size = b["storage"]
        if mode == "ReadWriteOnce":       # valid base: RWO shared storage needs a single replica
            c.replicas = 1
        c.storage = [Storage(mode, size, "shared")]
        if platform == "kubernetes" and rnd.random() < 0.5:
            c.ports[0].node_port = rnd.randint(30000, 32767)
        return c

    n = 0
    # --- valid -------------------------------------------------------------
    for kind, count in (("V", 75), ("HN-remap", 20), ("HN-perrep", 15), ("HN-bound", 15), ("HN-single", 10), ("HN-multi", 15)):
        for _ in range(count):
            c = base(n, kind)
            if kind == "HN-remap":
                c.ports[0].exposed = rnd.choice([8080, 8443, 3000, 5000])
            elif kind == "HN-perrep":
                c.storage = [Storage("ReadWriteOnce", "5Gi", "perReplica")]
                c.replicas = rnd.randint(2, 5)
            elif kind == "HN-bound":
                c.ports[0].node_port = rnd.choice([30000, 32767])
            elif kind == "HN-multi":
                c.container_ports = [c.container_ports[0], rnd.choice([9100, 9464, 15020])]
            elif kind == "HN-single":
                c.storage = [Storage("ReadWriteOnce", "5Gi", "shared")]
                c.replicas = 1
            c.expected = None
            cfgs.append(c)
            n += 1
    # --- misconfigured -------------------------------------------------------
    for cat in CATS_A:
        for _ in range(20):
            c = base(n, cat)
            if cat == "M1":
                c.storage = [Storage("ReadWriteOnce", "5Gi", "shared")]
                c.replicas = rnd.randint(2, 5)
            elif cat == "M2":
                c.ports[0].target = c.container_ports[0] + rnd.choice([1, 10, 1000, -1])
            elif cat == "M3":
                c.ports[0].node_port = rnd.randint(1, 29999)
            elif cat == "M4":
                c.ports[0].node_port = rnd.randint(32768, 65535)
            elif cat == "M5":
                c.storage = [Storage("ReadWriteOnce", "5Gi", "shared")]
                c.replicas = 0
            c.expected = cat
            cfgs.append(c)
            n += 1
    rnd.shuffle(cfgs)
    return cfgs


def generate_deployments(seed=SEED + 1):
    """Part B: deployment units of three microservices (see the module docstring)."""
    rnd = random.Random(seed)
    units = []

    def unit(uid, platform, kind):
        cfgs = {}
        svc = {"web": ("MyWebApp", "web"), "pay": ("PaymentProcessor", "payment"),
               "agg": ("DataAggregator", "aggregator")}
        for key, (base, dns) in svc.items():
            b = BASES[base]
            mode, size = b["storage"]
            c = Config(id=f"{uid}/{key}", platform=platform, deployment=uid, source=base, image=b["image"],
                       image_tag=image_tag(b["image"]), memory_limit=b["memory"], container_ports=[b["port"]],
                       replicas=1 if mode == "ReadWriteOnce" else rnd.randint(1, 5),
                       storage=[Storage(mode, size, "shared")])
            cfgs[key] = c
            if platform == "kubernetes":
                c.ports = [Port(exposed=b["port"], target=b["port"])]
                if rnd.random() < 0.5:
                    c.probe_ports = [b["port"]]
                cfgs[key + "@ep"] = Config(id=f"{uid}/{key}@ep", platform=platform, kind="endpoint", deployment=uid,
                                           names=[f"{dns}.default"], selector=True, routes_to=[c.id],
                                           ports=[Port(exposed=b["port"], name="http")])
            elif platform == "compose":
                c.ports = [Port(exposed=8080, target=80, host_port=8080)] if key == "web" else []
                cfgs[key + "@ep"] = Config(id=f"{uid}/{key}@ep", platform=platform, kind="endpoint", deployment=uid,
                                           names=[dns], routes_to=[c.id], ports=[Port(exposed=b["port"])])
            elif key == "web":                 # ECS: load-balancer target of the web service
                c.ports = [Port(target=b["port"])]
                cfgs[key + "@ep"] = Config(id=f"{uid}/{key}@ep", platform=platform, kind="endpoint", deployment=uid,
                                           selector=True, routes_to=[c.id])
        web = cfgs["web"]
        if platform == "compose":
            web.replicas = 1                   # web publishes a host port
        if platform == "kubernetes":
            web.dependencies = [Dependency("payment.default", 8080, key="PAYMENT_ADDR"),
                                Dependency("aggregator.default", 9090, key="AGGREGATOR_ADDR")]
            if rnd.random() < 0.5:             # NodePort Service for web
                np_ = rnd.randint(30000, 32767)
                web.ports[0].node_port = np_
                cfgs["web@ep"].ports[0].node_port = np_
            if rnd.random() < 0.5 or kind in ("M13", "HN-portname"):
                cfgs["ing"] = Config(id=f"{uid}/ing", platform=platform, kind="ingress", deployment=uid,
                                     dependencies=[Dependency("web.default", 80, key="backend")])
        elif platform == "compose":
            web.dependencies = [Dependency("payment", 8080, key="PAYMENT_ADDR"),
                                Dependency("aggregator", 9090, key="AGGREGATOR_ADDR")]
        return cfgs

    k8s_only = {"M12", "M13", "M14", "M15", "HN-hostport", "HN-nodeport", "HN-svcport", "HN-external",
                "HN-probemulti", "HN-rwop", "HN-portname"}
    plan = [("V", 50)] + [(h, 15 if h == "HN-svcport" else 10) for h in HN_B] + [(m, 20) for m in CATS_B]
    n = 0
    for kind, count in plan:
        for _ in range(count):
            uid = f"D{n:03d}_{kind}"
            if kind in k8s_only:
                platform = "kubernetes"
            elif kind in ("M8", "M9", "M10"):
                platform = rnd.choice(["kubernetes", "compose"])
            else:
                platform = rnd.choice(["kubernetes", "compose", "ecs"])
            u = unit(uid, platform, kind)
            web, pay = u["web"], u["pay"]
            if kind == "HN-hostport":
                web.ports.append(Port(host_port=8080))
                web.replicas = rnd.choice([None, 1])
            elif kind == "HN-nodeport":
                for key, np_ in zip(("web", "pay"), rnd.sample(range(30000, 32768), 2)):
                    u[key].ports[0].node_port = np_
                    u[key + "@ep"].ports[0].node_port = np_
            elif kind == "HN-svcport":
                u["pay@ep"].ports[0].exposed = 80
                pay.ports[0].exposed = 80
                web.dependencies[0].port = 80
            elif kind == "HN-external":
                u["db@ep"] = Config(id=f"{uid}/db@ep", platform=platform, kind="endpoint", deployment=uid,
                                    names=["db.default"], ports=[Port(exposed=5432)])
                web.dependencies.append(Dependency("db.default", 5432, key="DB_ADDR"))
            elif kind == "HN-latest":
                web.image, web.image_tag, web.replicas = "nginx", "latest", 1
                if platform == "compose":
                    pass
            elif kind == "HN-probemulti":
                web.container_ports = [80, 9100]
                web.probe_ports = [9100]
            elif kind == "HN-rwop":
                pay.storage = [Storage("ReadWriteOncePod", "5Gi", "perReplica")]
                pay.replicas = rnd.randint(2, 5)
            elif kind == "HN-portname":
                u["ing"].dependencies = [Dependency("web.default", None, "http", key="backend")]
            elif kind == "M7":
                web.replicas = rnd.randint(2, 5)
                if platform == "kubernetes":
                    web.ports.append(Port(host_port=8080))
                elif platform == "ecs":           # bridge network mode with a fixed host port
                    web.ports.append(Port(host_port=80))
            elif kind == "M8":
                if platform == "kubernetes":
                    np_ = rnd.randint(30000, 32767)
                    for key in ("web", "pay"):
                        u[key].ports[0].node_port = np_
                        u[key + "@ep"].ports[0].node_port = np_
                else:
                    pay.ports = [Port(exposed=8080, target=8080, host_port=8080)]
            elif kind == "M9":
                if platform == "kubernetes":    # client uses the targetPort of a remapped Service
                    u["pay@ep"].ports[0].exposed = 80
                    pay.ports[0].exposed = 80
                else:
                    web.dependencies[0].port = 8081
            elif kind == "M10":
                web.dependencies[0].host = web.dependencies[0].host.replace("payment", "paymnet")
            elif kind == "M11":
                web.image, web.image_tag = rnd.choice([("nginx", "latest"), ("nginx:latest", "latest")])
                web.replicas = rnd.randint(2, 5)
            elif kind == "M12":
                if platform == "kubernetes":    # the selector of the payment Service matches no pod
                    u["pay@ep"].routes_to = []
                    pay.ports = []
                else:
                    u["web@ep"].routes_to = []
            elif kind == "M13":
                d = u["ing"].dependencies[0]
                if rnd.random() < 0.5:
                    d.port = 81
                else:
                    d.host = "webb.default"
            elif kind == "M14":
                web.probe_ports = [81]
            elif kind == "M15":
                pay.storage = [Storage("ReadWriteOncePod", "5Gi", "shared")]
                pay.replicas = rnd.randint(2, 5)
            elif kind == "M16":
                u[rnd.choice(["web", "pay", "agg"])].memory_limit = None
            for c in u.values():
                c.expected = kind if kind in CATS_B else None
            units.append((uid, kind, list(u.values())))
            n += 1
    rnd.shuffle(units)
    return units


def unit_of(c):
    return c.deployment or c.id


def metrics(cfgs, flagged, rules=None):
    """Detection per deployment unit. rules (element id -> constraints) is used to check that
    each detected fault is attributed to the constraint that targets it."""
    units = {}
    for c in cfgs:
        u = units.setdefault(unit_of(c), dict(expected=None, kind=None, flagged=False, rules=set()))
        if c.expected:
            u["expected"] = c.expected
        u["kind"] = c.id.rsplit("_", 1)[0] if c.deployment is None else c.deployment.split("_", 1)[1]
        u["flagged"] |= c.id in flagged
        u["rules"] |= set((rules or {}).get(c.id, ()))
    valid = [u for u in units.values() if u["expected"] is None]
    fp = sum(u["flagged"] for u in valid)
    tn = len(valid) - fp
    rows = {}
    groups = {cat: [u for u in units.values() if u["expected"] == cat] for cat in CATS}
    for cat, items in list(groups.items()) + [("Overall", [u for u in units.values() if u["expected"]]),
                                              ("Part A", [u for u in units.values() if u["expected"] in CATS_A]),
                                              ("Part B", [u for u in units.values() if u["expected"] in CATS_B])]:
        tp = sum(u["flagged"] for u in items)
        fn = len(items) - tp
        prec = tp / (tp + fp) if tp + fp else 0.0
        rec = tp / (tp + fn) if tp + fn else 0.0
        f1 = 2 * prec * rec / (prec + rec) if prec + rec else 0.0
        rows[cat] = dict(TP=tp, FP=fp, FN=fn, TN=tn, precision=round(prec, 3), recall=round(rec, 3),
                         F1=round(f1, 3), FPR=round(fp / (fp + tn), 3) if fp + tn else 0.0)
        if rules is not None and cat in EXPECTED_RULE:
            rows[cat]["attributed"] = sum(1 for u in items if EXPECTED_RULE[cat] in u["rules"]
                                          or (cat == "M2" and "S2" in u["rules"]))
    hn = {}
    for u in valid:
        k = u["kind"] if u["kind"].startswith("HN") else "V"
        hn[k] = hn.get(k, 0) + u["flagged"]
    return rows, hn


def summary(xs):
    return dict(mean_ms=round(1000 * statistics.mean(xs), 2), sd_ms=round(1000 * statistics.pstdev(xs), 2),
                median_ms=round(1000 * statistics.median(xs), 2))


def per_config(part_a, v, tmpdir):
    """Time of validating each configuration of part A in a separate invocation."""
    tp, ts, to, th = [], [], [], []
    for c in part_a:
        f = tmpdir / "one.owl"
        _, t1, e1 = v.validate([c], keep_file=f)
        assert e1 is None, e1
        tp.append(t1)
        ts.append(shacl_validate(f)[1])
        to.append(opa_validate([c])[1])
        th.append(hybrid_validate([c], tmpdir)[1])
    return dict(pellet=summary(tp), shacl=summary(ts), opa=summary(to), hybrid=summary(th))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default=str(ROOT / "results" / "benchmark.json"))
    args = ap.parse_args()
    part_a = generate()
    units = generate_deployments()
    part_b = [c for _, _, items in units for c in items]
    cfgs = part_a + part_b
    (ROOT / "results").mkdir(exist_ok=True)
    with open(ROOT / "results" / "benchmark_configs.json", "w") as f:
        json.dump([c.to_json() for c in cfgs], f, indent=1)

    tbox = ROOT / "ontology" / "microwl-tbox.owl"
    tbox_nos2 = ROOT / "ontology" / "microwl-tbox-noS2.owl"
    v = PelletValidator(tbox)
    tmpdir = Path(tempfile.mkdtemp())

    # --- verdicts on parts A and B -------------------------------------------------
    # pySHACL grows super-linearly with the size of the data graph, so the shapes are evaluated on
    # chunks of whole deployment units (every constraint is evaluated within one unit)
    pel, _, err = v.validate(cfgs)
    assert err is None, err
    pel_rules = dict(v.rules)
    sh_rules = shacl_rules_chunked(cfgs, tbox, SHAPES, tmpdir)
    shc_rules = shacl_rules_chunked(cfgs, tbox, SHAPES_CLOSED, tmpdir)
    opa_r = opa_rules(cfgs, REGO, "data.microwl.violation")
    opc_rules = opa_rules(cfgs)
    # hybrid validator: Pellet, then the hybrid shapes over the reasoned graph, per chunk of units
    hyb_rules, err_h = hybrid_rules_chunked(cfgs)
    assert err_h is None, err_h
    sh, opa, shc, opc, hyb = (set(x) for x in (sh_rules, opa_r, shc_rules, opc_rules, hyb_rules))

    # --- batch times on part A (250 configurations) -------------------------------
    abox = tmpdir / "batch.owl"
    _, t_pel, _ = v.validate(part_a, keep_file=abox)
    _, t_sh = shacl_validate(abox)
    _, t_opa = opa_validate(part_a)
    t0 = time.perf_counter()
    shacl_validate(abox, SHAPES_CLOSED)
    t_shc = time.perf_counter() - t0
    _, t_opc = opa_closed(part_a)
    _, t_hyb = hybrid_validate(part_a, tmpdir)
    tb_abl = ROOT / "ontology" / "microwl-ablated-tbox.owl"
    if not tb_abl.exists():
        make_tbox(ROOT / "ontology" / "microwl-ablated.owl", tb_abl)
    abl, _, err_abl = PelletValidatorAblated(tb_abl).validate(part_a)
    assert err_abl is None, err_abl
    res = {"n_configs": len(part_a), "n_units_b": len(units), "n_elements_b": len(part_b),
           "batch_seconds_part_a": dict(pellet=round(t_pel, 3), shacl=round(t_sh, 3), opa=round(t_opa, 3),
                                 shacl_closed=round(t_shc, 3), opa_closed=round(t_opc, 3), hybrid=round(t_hyb, 3))}
    for name, fl, rl, data in (("pellet", pel, pel_rules, cfgs), ("shacl", sh, sh_rules, cfgs),
                               ("opa", opa, opa_r, cfgs), ("shacl_closed", shc, shc_rules, cfgs),
                               ("opa_closed", opc, opc_rules, cfgs), ("hybrid", hyb, hyb_rules, cfgs),
                               ("ablated", abl, None, part_a)):
        rows, hn = metrics(data, fl, rl)
        res[name] = dict(metrics=rows, hard_negatives_flagged=hn)
    res["agreement"] = dict(pellet_vs_shacl=pel == sh, pellet_vs_opa=pel == opa,
                            hybrid_vs_shacl_closed=hyb == shc, hybrid_vs_opa_closed=hyb == opc,
                            rules_hybrid_vs_opa_closed=hyb_rules == opc_rules)

    # --- per configuration (part A) --------------------------------------------------
    res["per_config"] = per_config(part_a, v, tmpdir)
    with open(args.out, "w") as f:
        json.dump(res, f, indent=1)
    print(json.dumps({k: res[k] for k in ("batch_seconds_part_a", "agreement", "per_config")}, indent=1))
    for k in ("pellet", "shacl", "opa", "shacl_closed", "opa_closed", "hybrid", "ablated"):
        print(k, json.dumps(res[k]["metrics"]["Overall"]), res[k]["hard_negatives_flagged"])


if __name__ == "__main__":
    main()
