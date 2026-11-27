"""Shared code for the MicrOWL experiments.

* `Config`: platform-agnostic description of one microservice deployment
  (the information captured by the MicrOWL abstract layer plus the platform
  binding needed by the rules).
* `make_tbox`: strip the case-study individuals from an ontology file.
* `PelletValidator`: populate an ABox with configurations, run Pellet through
  Owlready2, and return the microservices classified as InconsistentDeployment.
* `explain`: obtain Pellet justifications for one inferred classification.
"""
from __future__ import annotations

import os
import re
import subprocess
import tempfile
import time
from dataclasses import dataclass, field, asdict
from pathlib import Path
from typing import Optional

import rdflib
from rdflib import RDF, OWL

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
MW = "http://www.khaos.uma.es/microwl/"
OMSAC = "http://www.semanticweb.org/ontologyOfMicroservicesArchitecture/"
PLATFORMS = ("kubernetes", "compose", "ecs")
# severity of every constraint (as in the constraint catalogue of the ontology, build_microwl.py)
SEVERITY = {"S1": "error", "S2": "error", "S2c": "error", "S3": "error", "S5": "warning", "S7": "error",
            "S8": "error", "S9": "error", "S10": "error", "S11": "warning", "S12": "error", "S13": "error",
            "S14": "error", "S15": "error", "S16": "warning"}


@dataclass
class Storage:
    access_mode: Optional[str] = None          # ReadWriteOnce, ReadWriteMany, ...
    size: Optional[str] = None
    scope: str = "shared"                      # shared | perReplica


@dataclass
class Port:
    exposed: Optional[int] = None              # port published by the Service / host
    target: Optional[int] = None               # container port traffic is forwarded to
    node_port: Optional[int] = None            # Kubernetes NodePort (if any)
    host_port: Optional[int] = None            # port bound on the node/host by every replica
    name: Optional[str] = None                 # port name (endpoints)


@dataclass
class Dependency:
    host: str                                  # canonical name of the endpoint the client connects to
    port: Optional[int] = None
    port_name: Optional[str] = None
    key: Optional[str] = None                  # provenance (environment variable, Ingress backend)


@dataclass
class Config:
    """One element of a deployment. kind = "workload" (a microservice), "endpoint" (a named entry
    point: Kubernetes Service, Compose service name, ECS load-balancer target) or "ingress"."""
    id: str
    platform: str
    kind: str = "workload"
    deployment: Optional[str] = None           # deployment unit (descriptor group); None = on its own
    replicas: Optional[int] = None
    image: Optional[str] = None
    image_tag: Optional[str] = None            # "latest" when the image reference has no tag or digest
    container_ports: list = field(default_factory=list)
    probe_ports: list = field(default_factory=list)
    memory_limit: Optional[str] = None         # declared for every container of the workload
    ports: list = field(default_factory=list)  # list[Port]
    storage: list = field(default_factory=list)  # list[Storage]
    dependencies: list = field(default_factory=list)  # list[Dependency]
    names: list = field(default_factory=list)  # endpoint: names under which it is reachable
    routes_to: list = field(default_factory=list)  # endpoint: ids of the workloads it routes to
    selector: bool = False                     # endpoint: expected to route to some workload
    incomplete: bool = False                   # the deployment unit has descriptors that were not translated
    expected: Optional[str] = None             # ground-truth label (benchmark only)
    source: Optional[str] = None               # provenance (corpus file)
    notes: list = field(default_factory=list)

    def to_json(self):
        return asdict(self)


def make_tbox(src: Path, dst: Path) -> Path:
    """Remove every named individual (and its triples) from an ontology."""
    g = rdflib.Graph()
    g.parse(str(src), format="xml")
    for ind in set(g.subjects(RDF.type, OWL.NamedIndividual)):
        g.remove((ind, None, None))
        g.remove((None, None, ind))
    g.serialize(str(dst), format="xml")
    return dst


def safe(s: str) -> str:
    return "".join(ch if ch.isalnum() or ch == "_" else "_" for ch in s)


class PelletValidator:
    """Populate the MicrOWL ABox with configurations and classify it with Pellet."""

    def __init__(self, tbox_path: Path):
        self.tbox_path = Path(tbox_path).resolve()

    def _populate(self, onto, cfgs):
        mw = onto.get_namespace(MW)
        om = onto.get_namespace(OMSAC)
        platform_cls = {"kubernetes": mw.KubernetesConfig, "compose": mw.DockerComposeConfig,
                        "ecs": mw.ECSConfig}
        units = {}

        def unit(c):
            if c.deployment is None:
                return None
            if c.deployment not in units:
                units[c.deployment] = mw.DeploymentUnit(f"du_{safe(c.deployment)}")
                if c.incomplete:
                    units[c.deployment].isIncomplete = True
            return units[c.deployment]

        def dependencies(owner, n, c):
            for j, d in enumerate(c.dependencies):
                dep = mw.ServiceDependency(f"dep_{n}_{j}")
                owner.hasDependency.append(dep)
                dep.dependsOnHost = d.host
                if d.port is not None:
                    dep.dependsOnPort = d.port
                if d.port_name:
                    dep.dependsOnPortName = d.port_name

        with onto:
            for c in cfgs:
                if c.kind != "workload":
                    continue
                n = safe(c.id)
                ms = om.Microservice(f"ms_{n}", namespace=mw)
                ms.hasIdentifier = c.id
                if c.deployment is not None:
                    ms.inDeployment = unit(c)
                ws = mw.WorkloadSpecification(f"ws_{n}")
                ms.hasWorkloadSpec = [ws]
                if c.replicas is not None:
                    ws.hasReplicas = c.replicas
                ct = mw.ContainerSpecification(f"ct_{n}")
                ms.hasContainerSpec = [ct]
                if c.image:
                    ct.hasContainerImage = c.image
                if c.image_tag:
                    ct.hasImageTag = c.image_tag
                if c.memory_limit:
                    ct.hasMemoryLimit = c.memory_limit
                ct.hasContainerPort = list(c.container_ports)
                ct.hasProbePort = list(c.probe_ports)
                for j, p in enumerate(c.ports):
                    nt = mw.NetworkSpecification(f"nt_{n}_{j}")
                    ms.hasNetworkSpec.append(nt)
                    if p.exposed is not None:
                        nt.hasExposedPort = p.exposed
                    if p.target is not None:
                        nt.hasTargetPort = p.target
                    if p.host_port is not None:
                        nt.hasHostPort = [p.host_port]
                for j, s in enumerate(c.storage):
                    cls = mw.SharedStorageSpecification if s.scope == "shared" else mw.PerReplicaStorageSpecification
                    st = cls(f"st_{n}_{j}")
                    ms.hasStorageSpec.append(st)
                    if s.access_mode:
                        st.hasAccessMode = s.access_mode
                    if s.size:
                        st.hasStorageSize = s.size
                pc = platform_cls[c.platform](f"pc_{n}")
                ms.hasPlatformConfig = [pc]
                for j, p in enumerate(c.ports):
                    if p.node_port is not None:
                        r = mw.NodePortResource(f"np_{n}_{j}")
                        r.resourceType = ["nodePort"]
                        r.hasNodePortValue = p.node_port
                        pc.hasResource.append(r)
                dependencies(ms, n, c)
            for c in cfgs:
                n = safe(c.id)
                if c.kind == "endpoint":
                    ep = mw.ServiceEndpoint(f"ep_{n}")
                    ep.hasIdentifier = c.id
                    if c.deployment is not None:
                        ep.inDeployment = unit(c)
                    ep.hasEndpointName = list(c.names)
                    ep.hasSelector = bool(c.selector)
                    ep.hasEndpointPort = sorted({p.exposed for p in c.ports if p.exposed is not None})
                    ep.hasEndpointPortName = sorted({p.name for p in c.ports if p.name})
                    ep.hasEndpointNodePort = sorted({p.node_port for p in c.ports if p.node_port is not None})
                    ep.routesTo = [om.Microservice(f"ms_{safe(r)}", namespace=mw) for r in c.routes_to]
                elif c.kind == "ingress":
                    ing = mw.KubernetesIngress(f"ing_{n}")
                    ing.hasIdentifier = c.id
                    if c.deployment is not None:
                        ing.inDeployment = unit(c)
                    dependencies(ing, n, c)

    def validate(self, cfgs, keep_file: Optional[Path] = None):
        """Return (flagged ids, elapsed seconds for reasoning, error message or None).
        After the call, `self.rules` maps every flagged id to the constraints it violates
        (inferred values of violatesConstraint) and `self.depends_on` holds the inferred
        dependsOn pairs."""
        from owlready2 import World, sync_reasoner_pellet
        self.rules, self.depends_on = {}, set()
        w = World()
        onto = w.get_ontology("file://" + str(self.tbox_path)).load()
        self._populate(onto, cfgs)
        if keep_file:
            onto.save(file=str(keep_file), format="rdfxml")
        t0 = time.perf_counter()
        try:
            with onto:
                sync_reasoner_pellet(w, infer_property_values=True,
                                     infer_data_property_values=True, debug=0)
        except Exception as e:  # e.g. ontology inconsistent (functional property clash)
            return None, time.perf_counter() - t0, str(e).strip().splitlines()[-1]
        elapsed = time.perf_counter() - t0
        self.world, self.onto = w, onto
        mw = onto.get_namespace(MW)
        by_id = element_names(cfgs)
        flagged = set()
        for cls in ("InconsistentDeployment", "InconsistentEndpoint", "InconsistentIngress"):
            if getattr(mw, cls, None) is None:
                continue
            for i in getattr(mw, cls).instances():
                if i.name in by_id:
                    flagged.add(by_id[i.name])
                    if hasattr(i, "violatesConstraint"):
                        self.rules.setdefault(by_id[i.name], set()).update(i.violatesConstraint)
        if getattr(mw, "dependsOn", None) is not None:
            for i in mw.dependsOn.get_relations() if hasattr(mw.dependsOn, "get_relations") else []:
                a, b = i
                if a.name in by_id and b.name in by_id:
                    self.depends_on.add((by_id[a.name], by_id[b.name]))
        return flagged, elapsed, None


PREFIX = {"workload": "ms_", "endpoint": "ep_", "ingress": "ing_"}


def element_names(cfgs):
    """Local name of the individual that represents each element -> element id."""
    return {f"{PREFIX[c.kind]}{safe(c.id)}": c.id for c in cfgs}


def pellet_classpath() -> str:
    import owlready2
    d = Path(owlready2.__file__).parent / "pellet"
    return ":".join(str(p) for p in sorted(d.glob("*.jar")))


def explain(owl_file: Path, individual_iri: str, class_iri: str, max_explanations: int = 1) -> str:
    """Run `pellet explain --instance` and return its textual justification(s)
    (at most `max_explanations`; Pellet's default is one)."""
    out = subprocess.run(["java", "-Xmx2000M", "-cp", pellet_classpath(), "pellet.Pellet",
                          "explain", "--max", str(max_explanations),
                          "--instance", f"{individual_iri},{class_iri}",
                          "file://" + str(Path(owl_file).resolve())],
                         capture_output=True, text=True, check=True)
    return out.stdout


SHAPES = HERE / "shapes" / "microwl-shapes.ttl"
REGO = HERE / "rego" / "microwl.rego"


def shacl_validate(abox, shapes_file: Path = None):
    """Validate an ABox with the SHACL shapes. `abox` is an RDF/XML file (asserted facts) or an
    rdflib graph (e.g. the reasoned graph of the hybrid validator).
    Returns (flagged element local names, elapsed seconds)."""
    import pyshacl
    if isinstance(abox, rdflib.Graph):
        data = abox
    else:
        data = rdflib.Graph()
        data.parse(str(abox), format="xml")
    shapes = rdflib.Graph()
    shapes.parse(str(shapes_file or SHAPES), format="turtle")
    t0 = time.perf_counter()
    conforms, report, _ = pyshacl.validate(data, shacl_graph=shapes, inference="none",
                                           advanced=True, allow_warnings=True)
    elapsed = time.perf_counter() - t0
    SH = rdflib.Namespace("http://www.w3.org/ns/shacl#")
    SHACL_RULES.clear()
    for r in report.subjects(SH.focusNode, None):
        name = re.split(r"[/#]", str(report.value(r, SH.focusNode)))[-1]
        msg = str(report.value(r, SH.resultMessage) or "")
        SHACL_RULES.setdefault(name, set()).add(msg.split(":", 1)[0])
    return set(SHACL_RULES), elapsed


SHACL_RULES = {}   # local name -> constraints reported by the last shacl_validate call


def shacl_rules(cfgs):
    """Constraints reported by the last shacl_validate call, keyed by element id."""
    by_name = element_names(cfgs)
    return {by_name[n]: r for n, r in SHACL_RULES.items() if n in by_name}


def opa_validate(cfgs, policy: Path = None, query: str = "data.microwl.flagged"):
    """Evaluate the Rego policy with `opa eval`. Returns (flagged ids, elapsed seconds)."""
    import json
    payload = {"configs": [c.to_json() for c in cfgs]}
    fd, tmp = tempfile.mkstemp(suffix=".json")
    with os.fdopen(fd, "w") as f:
        json.dump(payload, f)
    t0 = time.perf_counter()
    out = subprocess.run(["opa", "eval", "--format", "json", "-d", str(policy or REGO), "-i", tmp,
                          query], capture_output=True, text=True, check=True)
    elapsed = time.perf_counter() - t0
    os.remove(tmp)
    res = json.loads(out.stdout)["result"][0]["expressions"][0]["value"]
    return set(res), elapsed


def shacl_flagged_ids(focus_names, cfgs):
    by_name = element_names(cfgs)
    return {by_name[n] for n in focus_names if n in by_name}


SHAPES_CLOSED = HERE / "shapes" / "microwl-shapes-closed.ttl"
SHAPES_HYBRID = HERE / "shapes" / "microwl-shapes-hybrid.ttl"
SHAPES_HYBRID_ASSERTED = HERE / "shapes" / "microwl-shapes-hybrid-asserted.ttl"
REGO_CLOSED = HERE / "rego" / "microwl_closed.rego"


def make_tbox_without_rule(src: Path, dst: Path, label: str) -> Path:
    """Copy a TBox removing the SWRL rule with the given rdfs:label."""
    from rdflib import RDFS, Namespace
    SWRL_NS = Namespace("http://www.w3.org/2003/11/swrl#")
    g = rdflib.Graph()
    g.parse(str(src), format="xml")
    for r in list(g.subjects(RDF.type, SWRL_NS.Imp)):
        if str(g.value(r, RDFS.label)) == label:
            g.remove((r, None, None))
    g.serialize(str(dst), format="xml")
    return dst


def opa_closed(cfgs):
    return opa_validate(cfgs, REGO_CLOSED, "data.microwl_closed.flagged")


HYBRID_RULES = {}  # element id -> constraints reported by the last hybrid_validate call


def hybrid_validate(cfgs, tmpdir: Path):
    """MicrOWL hybrid validator: Pellet with the SWRL rules (all except the disabled S2), then the
    closed-world SHACL shapes (S2c, S9, S10, S12, S13, S14, S16) over the reasoned graph, so that
    S9, S10 and S13 use the resolvesTo relations inferred by D1.
    Returns (flagged ids, seconds); HYBRID_RULES gives the constraints of every flagged element."""
    tbox = ROOT / "ontology" / "microwl-tbox-noS2.owl"
    if not tbox.exists():
        make_tbox_without_rule(ROOT / "ontology" / "microwl-tbox.owl", tbox, "S2_PortAlignmentConsistency")
    v = PelletValidator(tbox)
    pel, t1, err = v.validate(cfgs)
    if err:
        raise RuntimeError(err)
    t0 = time.perf_counter()
    g = reasoned_graph(v)
    t_copy = time.perf_counter() - t0
    focus, t2 = shacl_validate(g, SHAPES_HYBRID)
    t2 += t_copy
    HYBRID_RULES.clear()
    HYBRID_RULES.update({k: set(r) for k, r in v.rules.items()})
    for k, r in shacl_rules(cfgs).items():
        HYBRID_RULES.setdefault(k, set()).update(r)
    return pel | shacl_flagged_ids(focus, cfgs), t1 + t2


def hybrid_validate_asserted(cfgs, tmpdir: Path):
    """Ablation of the hybrid validator: Pellet, then the closed-world shapes that resolve dependency
    names in SPARQL, evaluated over the asserted ABox (no inferred facts). Returns (flagged ids, seconds)."""
    tbox = ROOT / "ontology" / "microwl-tbox-noS2.owl"
    abox = Path(tmpdir) / "hybrid_asserted.owl"
    v = PelletValidator(tbox)
    pel, t1, err = v.validate(cfgs, keep_file=abox)
    if err:
        raise RuntimeError(err)
    focus, t2 = shacl_validate(abox, SHAPES_HYBRID_ASSERTED)
    return pel | shacl_flagged_ids(focus, cfgs), t1 + t2


def chunks_by_unit(cfgs, size=150):
    """Split elements into chunks of whole deployment units (every constraint is evaluated
    within one unit, so validating the chunks separately gives the same result)."""
    units = {}
    for c in cfgs:
        units.setdefault(c.deployment or c.id, []).append(c)
    out, cur = [], []
    for items in units.values():
        if cur and len(cur) + len(items) > size:
            out.append(cur)
            cur = []
        cur = cur + items
    if cur:
        out.append(cur)
    return out


def shacl_rules_chunked(cfgs, tbox: Path, shapes, tmpdir: Path, size=150):
    """Rule attribution of a SHACL shapes file (or of a list of shapes files, returning one result
    per file), validating chunks of whole deployment units."""
    from owlready2 import World
    many = isinstance(shapes, (list, tuple))
    files = list(shapes) if many else [shapes]
    res = [dict() for _ in files]
    for i, chunk in enumerate(chunks_by_unit(cfgs, size)):
        w = World()
        onto = w.get_ontology("file://" + str(Path(tbox).resolve())).load()
        PelletValidator(tbox)._populate(onto, chunk)
        f = Path(tmpdir) / f"chunk{i}.owl"
        onto.save(file=str(f), format="rdfxml")
        for r, sf in zip(res, files):
            shacl_validate(f, sf)
            r.update(shacl_rules(chunk))
    return res if many else res[0]


def pellet_rules_chunked(cfgs, tbox: Path, size=3000):
    """Pellet rule attribution on chunks of whole deployment units (every rule is evaluated
    within one unit). Returns (rules by element id, first error or None)."""
    rules, err = {}, None
    v = PelletValidator(tbox)
    for chunk in chunks_by_unit(cfgs, size):
        _, _, e = v.validate(chunk)
        err = err or e
        rules.update({k: set(r) for k, r in v.rules.items()})
    return rules, err


def reasoned_graph(v):
    """In-memory copy of the ontology of a PelletValidator after validate(): asserted facts plus
    the facts inferred by Pellet (class memberships, inverse properties, resolvesTo, dependsOn)."""
    g = rdflib.Graph()
    for t in v.world.as_rdflib_graph():
        g.add(t)
    return g


def hybrid_rules_chunked(cfgs, size=150):
    """Hybrid validator on chunks of whole deployment units: Pellet, then the hybrid shapes over the
    reasoned graph of each chunk. Returns (rules by element id, first error or None)."""
    tbox = ROOT / "ontology" / "microwl-tbox-noS2.owl"
    rules, err = {}, None
    v = PelletValidator(tbox)
    for chunk in chunks_by_unit(cfgs, size):
        _, _, e = v.validate(chunk)
        if e:
            err = err or e
            continue
        r = {k: set(x) for k, x in v.rules.items()}
        shacl_validate(reasoned_graph(v), SHAPES_HYBRID)
        for k, x in shacl_rules(chunk).items():
            r.setdefault(k, set()).update(x)
        rules.update(r)
    return rules, err


def hybrid_validate_by_unit(cfgs, tmpdir: Path, size=150):
    """Hybrid validator evaluated on chunks of whole deployment units (Pellet and the shapes run on
    each chunk). Returns (flagged ids, seconds); HYBRID_RULES as in hybrid_validate."""
    t0 = time.perf_counter()
    rules, err = hybrid_rules_chunked(cfgs, size)
    if err:
        raise RuntimeError(err)
    HYBRID_RULES.clear()
    HYBRID_RULES.update(rules)
    return set(rules), time.perf_counter() - t0


def opa_rules(cfgs, policy: Path = None, query: str = "data.microwl_closed.violation"):
    """Rule attribution of a Rego policy: element id -> set of constraints."""
    import json
    payload = {"configs": [c.to_json() for c in cfgs]}
    fd, tmp = tempfile.mkstemp(suffix=".json")
    with os.fdopen(fd, "w") as f:
        json.dump(payload, f)
    out = subprocess.run(["opa", "eval", "--format", "json", "-d", str(policy or REGO_CLOSED), "-i", tmp,
                          query], capture_output=True, text=True, check=True)
    os.remove(tmp)
    res = {}
    for cid, rule in json.loads(out.stdout)["result"][0]["expressions"][0]["value"]:
        res.setdefault(cid, set()).add(rule)
    return res


class PelletValidatorAblated(PelletValidator):
    """Populate the ABox with the vocabulary of the ablated variant (exposed port compared
    with the container port, storage not classified as shared or per-replica, NodePort as
    a string-valued PlatformResource) to measure the effect of these design decisions."""

    def _populate(self, onto, cfgs):
        mw = onto.get_namespace(MW)
        om = onto.get_namespace(OMSAC)
        platform_cls = {"kubernetes": mw.KubernetesConfig, "compose": mw.DockerComposeConfig,
                        "ecs": mw.ECSConfig}
        with onto:
            for c in cfgs:
                if c.kind != "workload":
                    continue
                n = safe(c.id)
                ms = om.Microservice(f"ms_{n}", namespace=mw)
                ws = mw.WorkloadSpecification(f"ws_{n}")
                ms.hasWorkloadSpec = [ws]
                if c.replicas is not None:
                    ws.hasReplicas = [c.replicas]
                ct = mw.ContainerSpecification(f"ct_{n}")
                ms.hasContainerSpec = [ct]
                ct.hasContainerPort = list(c.container_ports)
                for j, p in enumerate(c.ports):
                    nt = mw.NetworkSpecification(f"nt_{n}_{j}")
                    ms.hasNetworkSpec.append(nt)
                    if p.exposed is not None:
                        nt.hasExposedPort = [p.exposed]
                for j, s in enumerate(c.storage):
                    st = mw.StorageSpecification(f"st_{n}_{j}")
                    ms.hasStorageSpec.append(st)
                    if s.access_mode:
                        st.hasAccessMode = [s.access_mode]
                pc = platform_cls[c.platform](f"pc_{n}")
                ms.hasPlatformConfig = [pc]
                for j, p in enumerate(c.ports):
                    if p.node_port is not None:
                        r = mw.PlatformResource(f"np_{n}_{j}")
                        r.resourceType = ["nodePort"]
                        r.resourceValue = [str(p.node_port)]
                        pc.hasResource.append(r)
