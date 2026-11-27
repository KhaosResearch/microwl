"""Build the MicrOWL ontology (microwl.owl) from the ablated variant
(microwl-ablated.owl), which lacks the design decisions evaluated in the
ablation study of the paper.

Every design decision is applied programmatically, so the difference between
the ablated variant and the full knowledge base is fully documented here:

 1. Remove the illegal punning of `resourceValue` and `hasDescription`, which
    were declared both as annotation and as datatype properties.
 2. Typed resources: add `NodePortResource` (subclass of PlatformResource) with
    the functional datatype property `hasNodePortValue` (xsd:integer). The
    NodePort rules (S3a/S3b) now compare integers instead of strings, which in
    the ablated variant makes them unable to fire.
 3. Storage scope: add `SharedStorageSpecification` and
    `PerReplicaStorageSpecification` (disjoint subclasses of
    StorageSpecification). S1 only applies to shared storage, so per-replica
    storage (e.g. StatefulSet volumeClaimTemplates) is not flagged.
 4. Target port: add `hasTargetPort` (xsd:integer) on NetworkSpecification.
    S2 now compares the port to which traffic is forwarded with the container
    port, so legitimate remappings (8080:80, port/targetPort) are not flagged.
 5. Error classes: InconsistentDeployment becomes a subclass of
    omsac:Microservice and NodePortOutOfRange a subclass of KubernetesConfig,
    so that classified individuals keep a meaningful type.
 6. Functional properties for single-valued attributes (replicas, ports,
    access mode, storage size, image).
 7. Update the case-study ABox accordingly and rewrite S1, S2, S3a, S3b.
 8. S2 is kept for comparison but annotated as disabled (swrla:isRuleEnabled
    false): its open-world formulation flags multi-port containers, and the
    MicrOWL validator replaces it by the closed-world SHACL shape S2c. Pellet
    ignores this annotation, so the hybrid validator removes S2 explicitly
    (microwl-tbox-noS2.owl) before reasoning.
 9. Deployment-level vocabulary and constraints S7-S16: deployment units,
    service endpoints (named entry points with their ports and the
    microservices they route to), service dependencies (declared in
    environment variables or Ingress backends), host ports, image tags, probe
    ports and memory limits. The SWRL rules S7, S8a, S8b, S11 and S15 and the
    dependency-resolution rules D1 and D2 are added here; S9, S10, S12, S13,
    S14 and S16 need negation as failure and are SHACL shapes
    (experiments/shapes/microwl-shapes-hybrid.ttl).
10. Every rule records the constraint it checks with the data property
    violatesConstraint, so that each engine reports the violated constraint.
11. Severities: a catalogue of the constraints (individuals of ErrorConstraint
    or WarningConstraint, disjoint subclasses of ValidationConstraint). Rule V
    links each element to the constraints it violates (violates), and the
    defined classes DeploymentError (violates some ErrorConstraint) and
    DeploymentWarning (violates some WarningConstraint) classify it. The
    catalogue individuals are not declared owl:NamedIndividual, so that
    make_tbox keeps them in the TBox. A DeploymentUnit can be marked as
    incomplete (isIncomplete), and the closed-world shapes S10, S12 and S13 are
    not evaluated on incomplete units.
12. Case study: the three microservices form one deployment unit with an
    endpoint each; MyWebApp depends on PaymentProcessor and DataAggregator, and
    DataAggregator on a Kafka broker that is not part of the unit.
13. Annotations (rdfs:comment) for the classes and properties inherited from the ablated
    variant that had none.
14. Metadata: title and license (CC BY 4.0) of the ontology.

Usage: python build_microwl.py microwl-ablated.owl microwl.owl
"""
import sys
import rdflib
from rdflib import RDF, RDFS, OWL, XSD, URIRef, Literal, BNode
from rdflib.collection import Collection

SRC, DST = sys.argv[1], sys.argv[2]
MW = rdflib.Namespace("http://www.khaos.uma.es/microwl/")
OMSAC = rdflib.Namespace("http://www.semanticweb.org/ontologyOfMicroservicesArchitecture/")
SWRL = rdflib.Namespace("http://www.w3.org/2003/11/swrl#")
SWRLB = rdflib.Namespace("http://www.w3.org/2003/11/swrlb#")
SWRLA = rdflib.Namespace("http://swrl.stanford.edu/ontologies/3.3/swrla.owl#")
ONT = URIRef("http://www.khaos.uma.es/microwl")

g = rdflib.Graph()
g.parse(SRC, format="xml")

# --- 1. punning --------------------------------------------------------------
for p in (MW.resourceValue, MW.hasDescription):
    g.remove((p, RDF.type, OWL.AnnotationProperty))
# hasDescription is used as an annotation on individuals: keep it as annotation only
g.remove((MW.hasDescription, RDF.type, OWL.DatatypeProperty))
g.add((MW.hasDescription, RDF.type, OWL.AnnotationProperty))


def cls(c, parent, comment):
    g.add((c, RDF.type, OWL.Class))
    g.add((c, RDFS.subClassOf, parent))
    g.add((c, RDFS.comment, Literal(comment, lang="en")))


def dprop(p, domain, rng, comment, functional=True):
    g.add((p, RDF.type, OWL.DatatypeProperty))
    if functional:
        g.add((p, RDF.type, OWL.FunctionalProperty))
    g.add((p, RDFS.domain, domain))
    g.add((p, RDFS.range, rng))
    g.add((p, RDFS.comment, Literal(comment, lang="en")))


# --- 2. typed NodePort resource ---------------------------------------------
cls(MW.NodePortResource, MW.PlatformResource,
    "Kubernetes NodePort assigned to a Service; its value is a typed integer.")
dprop(MW.hasNodePortValue, MW.NodePortResource, XSD.integer,
      "Port number of a NodePortResource.")

# --- 3. storage scope --------------------------------------------------------
cls(MW.SharedStorageSpecification, MW.StorageSpecification,
    "A single volume shared by all replicas of the microservice "
    "(e.g. one PVC referenced by a Deployment, a named Compose volume, an EFS volume).")
cls(MW.PerReplicaStorageSpecification, MW.StorageSpecification,
    "Storage provisioned independently for each replica "
    "(e.g. StatefulSet volumeClaimTemplates).")
g.add((MW.SharedStorageSpecification, OWL.disjointWith, MW.PerReplicaStorageSpecification))

# --- 4. target port ----------------------------------------------------------
dprop(MW.hasTargetPort, MW.NetworkSpecification, XSD.integer,
      "Port of the container to which the exposed port forwards traffic.")

# --- 5. error classes ----------------------------------------------------------
g.remove((MW.InconsistentDeployment, RDFS.subClassOf, MW.DeploymentError))
g.remove((MW.NodePortOutOfRange, RDFS.subClassOf, MW.DeploymentError))
g.add((MW.InconsistentDeployment, RDFS.subClassOf, OMSAC.Microservice))
g.add((MW.NodePortOutOfRange, RDFS.subClassOf, MW.KubernetesConfig))

# --- 6. functional properties --------------------------------------------------
for p in (MW.hasReplicas, MW.hasContainerImage, MW.hasExposedPort,
          MW.hasAccessMode, MW.hasStorageSize):
    g.add((p, RDF.type, OWL.FunctionalProperty))

# --- 7a. ABox of the case studies ---------------------------------------------
g.remove((MW.MyWebApp_K8s_NodePort, RDF.type, MW.PlatformResource))
g.add((MW.MyWebApp_K8s_NodePort, RDF.type, MW.NodePortResource))
g.remove((MW.MyWebApp_K8s_NodePort, MW.resourceValue, None))
g.add((MW.MyWebApp_K8s_NodePort, MW.hasNodePortValue, Literal(30080, datatype=XSD.integer)))
for ms, port in (("MyWebApp", 80), ("PaymentProcessor", 8080), ("DataAggregator", 9090)):
    g.add((MW[f"{ms}_StorageSpec"], RDF.type, MW.SharedStorageSpecification))
    g.add((MW[f"{ms}_NetworkSpec"], MW.hasTargetPort, Literal(port, datatype=XSD.integer)))

# --- 7b. rules -------------------------------------------------------------------
def var(name):
    v = MW[name]
    g.add((v, RDF.type, SWRL.Variable))
    return v


def atom(kind, pred, *args):
    a = BNode()
    if kind == "class":
        g.add((a, RDF.type, SWRL.ClassAtom))
        g.add((a, SWRL.classPredicate, pred))
        g.add((a, SWRL.argument1, args[0]))
    elif kind in ("obj", "data"):
        g.add((a, RDF.type, SWRL.IndividualPropertyAtom if kind == "obj" else SWRL.DatavaluedPropertyAtom))
        g.add((a, SWRL.propertyPredicate, pred))
        g.add((a, SWRL.argument1, args[0]))
        g.add((a, SWRL.argument2, args[1]))
    elif kind == "builtin":
        g.add((a, RDF.type, SWRL.BuiltinAtom))
        g.add((a, SWRL.builtin, pred))
        lst = BNode()
        Collection(g, lst, list(args))
        g.add((a, SWRL.arguments, lst))
    return a


def rule(label, body, head, enabled=True, comment=None):
    # remove the rule of the ablated variant with the same label
    for r in list(g.subjects(RDF.type, SWRL.Imp)):
        if str(g.value(r, RDFS.label)) == label:
            g.remove((r, None, None))
    r = BNode()
    g.add((r, RDF.type, SWRL.Imp))
    g.add((r, RDFS.label, Literal(label)))
    g.add((r, SWRLA.isRuleEnabled, Literal(enabled)))
    if comment:
        g.add((r, RDFS.comment, Literal(comment)))
    b, h = BNode(), BNode()
    Collection(g, b, body)
    Collection(g, h, head)
    g.add((r, SWRL.body, b))
    g.add((r, SWRL.head, h))


m, ws, st, r_, ct, nt, cp, tp, k8s, res, p = (var(x) for x in
    ("m", "ws", "st", "r", "ct", "nt", "cport", "tport", "k8s", "res", "p"))
INT = lambda n: Literal(n, datatype=XSD.integer)

rule("S1_StorageAccessModeConsistency",
     [atom("class", OMSAC.Microservice, m), atom("obj", MW.hasWorkloadSpec, m, ws),
      atom("data", MW.hasReplicas, ws, r_), atom("builtin", SWRLB.greaterThan, r_, INT(1)),
      atom("obj", MW.hasStorageSpec, m, st), atom("class", MW.SharedStorageSpecification, st),
      atom("data", MW.hasAccessMode, st, Literal("ReadWriteOnce"))],
     [atom("class", MW.InconsistentDeployment, m), atom("data", MW.violatesConstraint, m, Literal("S1"))])
rule("S2_PortAlignmentConsistency",
     [atom("class", OMSAC.Microservice, m), atom("obj", MW.hasContainerSpec, m, ct),
      atom("data", MW.hasContainerPort, ct, cp), atom("obj", MW.hasNetworkSpec, m, nt),
      atom("data", MW.hasTargetPort, nt, tp), atom("builtin", SWRLB.notEqual, cp, tp)],
     [atom("class", MW.InconsistentDeployment, m), atom("data", MW.violatesConstraint, m, Literal("S2"))],
     enabled=False,
     comment="Open-world formulation kept for comparison; it flags containers that listen "
             "on several ports. The MicrOWL validator uses the closed-world SHACL shape S2c "
             "instead (experiments/shapes/microwl-shapes-hybrid.ttl).")
for label, bi, bound in (("S3a_NodePortLowerBound", SWRLB.lessThan, 30000),
                         ("S3b_NodePortUpperBound", SWRLB.greaterThan, 32767)):
    rule(label,
         [atom("class", OMSAC.Microservice, m), atom("obj", MW.hasPlatformConfig, m, k8s),
          atom("class", MW.KubernetesConfig, k8s), atom("obj", MW.hasResource, k8s, res),
          atom("class", MW.NodePortResource, res), atom("data", MW.hasNodePortValue, res, p),
          atom("builtin", bi, p, INT(bound))],
         [atom("class", MW.NodePortOutOfRange, k8s)])

# --- 7c. S4 and S5 rewritten with the constraint annotation of step 10 ------------
rule("S4_PropagateInconsistency",
     [atom("class", MW.NodePortOutOfRange, k8s), atom("obj", MW.platformConfigOf, k8s, m),
      atom("class", OMSAC.Microservice, m)],
     [atom("class", MW.InconsistentDeployment, m), atom("data", MW.violatesConstraint, m, Literal("S3"))])
rule("S5_MinimumReplicaForStateful",
     [atom("class", OMSAC.Microservice, m), atom("obj", MW.hasStorageSpec, m, st),
      atom("data", MW.hasAccessMode, st, Literal("ReadWriteOnce")), atom("obj", MW.hasWorkloadSpec, m, ws),
      atom("data", MW.hasReplicas, ws, r_), atom("builtin", SWRLB.lessThan, r_, INT(1))],
     [atom("class", MW.InconsistentDeployment, m), atom("data", MW.violatesConstraint, m, Literal("S5"))])

# --- 9. deployment-level vocabulary --------------------------------------------------
cls(MW.DeploymentUnit, MW.AbstractConcept,
    "Set of elements deployed together (a directory of Kubernetes manifests, a Compose file, "
    "a CloudFormation template); names and host ports are resolved within a unit.")
cls(MW.ServiceEndpoint, MW.AbstractConcept,
    "Named entry point through which clients reach microservices of the same deployment unit "
    "(Kubernetes Service, Compose service name, ECS load-balancer target).")
cls(MW.ServiceDependency, MW.AbstractConcept,
    "Reference from a microservice (environment variable) or an Ingress (backend) to a host name "
    "and, optionally, a port.")
cls(MW.InconsistentEndpoint, MW.ServiceEndpoint, "Service endpoint that violates a constraint.")
cls(MW.InconsistentIngress, MW.KubernetesIngress, "Ingress that violates a constraint.")


def oprop(p, domain, rng, comment, functional=False):
    g.add((p, RDF.type, OWL.ObjectProperty))
    if functional:
        g.add((p, RDF.type, OWL.FunctionalProperty))
    if domain is not None:
        g.add((p, RDFS.domain, domain))
    g.add((p, RDFS.range, rng))
    g.add((p, RDFS.comment, Literal(comment, lang="en")))


oprop(MW.inDeployment, None, MW.DeploymentUnit, "Deployment unit of a microservice, endpoint or Ingress.", True)
oprop(MW.hasDependency, None, MW.ServiceDependency, "Dependency declared by a microservice or an Ingress.")
oprop(MW.routesTo, MW.ServiceEndpoint, OMSAC.Microservice, "Microservice to which an endpoint routes traffic.")
oprop(MW.resolvesTo, MW.ServiceDependency, MW.ServiceEndpoint,
      "Endpoint of the same deployment unit whose name matches the host of a dependency (inferred by D1).")
oprop(MW.dependsOn, OMSAC.Microservice, OMSAC.Microservice,
      "A microservice depends on another one (inferred by D2).")
dprop(MW.hasIdentifier, OWL.Thing, XSD.string, "Identifier of a microservice, endpoint or Ingress.")
dprop(MW.hasHostPort, MW.NetworkSpecification, XSD.integer,
      "Port bound on the node or host by every replica (hostPort, published Compose port, "
      "ECS hostPort in bridge or host network mode).", functional=False)
dprop(MW.hasImageTag, MW.ContainerSpecification, XSD.string,
      "Tag of the container image (\"latest\" when the reference has neither tag nor digest).")
dprop(MW.hasProbePort, MW.ContainerSpecification, XSD.integer,
      "Port checked by a liveness, readiness or startup probe.", functional=False)
dprop(MW.hasMemoryLimit, MW.ContainerSpecification, XSD.string,
      "Memory limit declared for the containers of the microservice.")
dprop(MW.dependsOnHost, MW.ServiceDependency, XSD.string, "Canonical host name of a dependency.")
dprop(MW.dependsOnPort, MW.ServiceDependency, XSD.integer, "Port of a dependency.")
dprop(MW.dependsOnPortName, MW.ServiceDependency, XSD.string, "Port name of a dependency.")
dprop(MW.hasEndpointName, MW.ServiceEndpoint, XSD.string, "Name under which an endpoint is reachable.",
      functional=False)
dprop(MW.hasEndpointPort, MW.ServiceEndpoint, XSD.integer, "Port on which clients reach an endpoint.",
      functional=False)
dprop(MW.hasEndpointPortName, MW.ServiceEndpoint, XSD.string, "Name of an endpoint port.", functional=False)
dprop(MW.hasEndpointNodePort, MW.ServiceEndpoint, XSD.integer, "NodePort opened by an endpoint.",
      functional=False)
dprop(MW.hasSelector, MW.ServiceEndpoint, XSD.boolean,
      "True when the endpoint selects the microservices it routes to (Kubernetes selector, ECS container name).")
dprop(MW.violatesConstraint, OWL.Thing, XSD.string, "Constraint violated by an element (inferred).",
      functional=False)

# --- 9b. rules S7, S8a, S8b, S11, S15 and dependency resolution ---------------------
m2, nt2, hp, hp2, u, i1, i2, c1, c2, e, e2, d, h, tag = (var(x) for x in
    ("m2", "nt2", "hp", "hp2", "u", "i1", "i2", "c1", "c2", "e", "e2", "d", "h", "tag"))
FLAG = lambda cls_, x, s: [atom("class", cls_, x), atom("data", MW.violatesConstraint, x, Literal(s))]
REPLICAS_GT1 = lambda: [atom("class", OMSAC.Microservice, m), atom("obj", MW.hasWorkloadSpec, m, ws),
                atom("data", MW.hasReplicas, ws, r_), atom("builtin", SWRLB.greaterThan, r_, INT(1))]
rule("S7_HostPortReplicas",
     REPLICAS_GT1() + [atom("obj", MW.hasNetworkSpec, m, nt), atom("data", MW.hasHostPort, nt, hp)],
     FLAG(MW.InconsistentDeployment, m, "S7"),
     comment="Several replicas bind the same fixed host port: at most one replica can run on each node.")
rule("S8a_DuplicateNodePort",
     [atom("data", MW.hasEndpointNodePort, e, hp), atom("obj", MW.inDeployment, e, u),
      atom("obj", MW.inDeployment, e2, u), atom("data", MW.hasEndpointNodePort, e2, hp2),
      atom("builtin", SWRLB.equal, hp, hp2), atom("data", MW.hasIdentifier, e, i1),
      atom("data", MW.hasIdentifier, e2, i2), atom("builtin", SWRLB.notEqual, i1, i2),
      atom("class", MW.ServiceEndpoint, e), atom("class", MW.ServiceEndpoint, e2)],
     FLAG(MW.InconsistentEndpoint, e, "S8"),
     comment="Two endpoints of the same deployment unit open the same NodePort.")
rule("S8b_DuplicateHostPort",
     [atom("data", MW.hasHostPort, nt, hp), atom("obj", MW.networkSpecOf, nt, m),
      atom("obj", MW.inDeployment, m, u), atom("obj", MW.inDeployment, m2, u),
      atom("obj", MW.hasNetworkSpec, m2, nt2), atom("data", MW.hasHostPort, nt2, hp2),
      atom("builtin", SWRLB.equal, hp, hp2), atom("data", MW.hasIdentifier, m, i1),
      atom("data", MW.hasIdentifier, m2, i2), atom("builtin", SWRLB.notEqual, i1, i2),
      atom("obj", MW.hasPlatformConfig, m, c1), atom("class", MW.DockerComposeConfig, c1),
      atom("obj", MW.hasPlatformConfig, m2, c2), atom("class", MW.DockerComposeConfig, c2),
      atom("class", OMSAC.Microservice, m), atom("class", OMSAC.Microservice, m2)],
     FLAG(MW.InconsistentDeployment, m, "S8"),
     comment="Two services of the same Compose file, which run on a single host, publish the same host port.")
rule("S11_MutableImageTag",
     REPLICAS_GT1() + [atom("obj", MW.hasContainerSpec, m, ct),
                     atom("data", MW.hasImageTag, ct, Literal("latest"))],
     FLAG(MW.InconsistentDeployment, m, "S11"),
     comment="Replicas started from a mutable tag may run different image versions.")
rule("S15_ReadWriteOncePodReplicas",
     REPLICAS_GT1() + [atom("obj", MW.hasStorageSpec, m, st), atom("class", MW.SharedStorageSpecification, st),
                     atom("data", MW.hasAccessMode, st, Literal("ReadWriteOncePod"))],
     FLAG(MW.InconsistentDeployment, m, "S15"),
     comment="A ReadWriteOncePod volume can be mounted by a single pod, so only one replica can start.")
x = var("x")
rule("D1_ResolveDependency",
     [atom("obj", MW.hasDependency, x, d), atom("data", MW.dependsOnHost, d, h),
      atom("obj", MW.inDeployment, x, u), atom("class", MW.ServiceEndpoint, e),
      atom("obj", MW.inDeployment, e, u), atom("data", MW.hasEndpointName, e, h)],
     [atom("obj", MW.resolvesTo, d, e)],
     comment="A dependency resolves to the endpoint of the same deployment unit with the same name.")
rule("D2_DependsOn",
     [atom("class", OMSAC.Microservice, m), atom("obj", MW.hasDependency, m, d),
      atom("obj", MW.resolvesTo, d, e), atom("obj", MW.routesTo, e, m2)],
     [atom("obj", MW.dependsOn, m, m2)],
     comment="A microservice depends on the microservices to which its dependencies are routed.")

# --- 11. severities and incomplete units ---------------------------------------------
cls(MW.ValidationConstraint, MW.AbstractConcept, "Validation constraint of the knowledge base.")
cls(MW.ErrorConstraint, MW.ValidationConstraint,
    "Constraint whose violation can make the deployment, or the traffic it serves, fail.")
cls(MW.WarningConstraint, MW.ValidationConstraint,
    "Constraint whose violation indicates a risky or incomplete design (best practice).")
g.add((MW.ErrorConstraint, OWL.disjointWith, MW.WarningConstraint))
dprop(MW.hasConstraintId, MW.ValidationConstraint, XSD.string, "Identifier of a constraint (S1, S2c, ...).")
oprop(MW.violates, None, MW.ValidationConstraint, "Constraint violated by an element (inferred by rule V).")
dprop(MW.isIncomplete, MW.DeploymentUnit, XSD.boolean,
      "True when some descriptors of the unit could not be translated (e.g. templated manifests).")
CATALOGUE = {
    "S1": ("error", "Replicas share a ReadWriteOnce volume."),
    "S2": ("error", "A target port differs from a container port (open-world formulation, disabled)."),
    "S2c": ("error", "A target port matches none of the container ports."),
    "S3": ("error", "A NodePort lies outside the range of the cluster."),
    "S5": ("warning", "ReadWriteOnce storage is requested with zero replicas."),
    "S7": ("error", "Several replicas bind a fixed host port."),
    "S8": ("error", "Two endpoints or services of a unit bind the same node or host port."),
    "S9": ("error", "A dependency uses a port that its endpoint does not serve."),
    "S10": ("error", "A dependency names no endpoint of its deployment unit."),
    "S11": ("warning", "Several replicas use an image without tag or with the tag latest."),
    "S12": ("error", "An endpoint with a selector routes to no microservice."),
    "S13": ("error", "An Ingress backend names no endpoint, or a port it does not serve."),
    "S14": ("error", "A probe checks a port that is not a container port."),
    "S15": ("error", "Several replicas share a ReadWriteOncePod volume."),
    "S16": ("warning", "No memory limit is declared."),
}
for cid, (sev, comment) in CATALOGUE.items():
    c = MW[f"Constraint_{cid}"]
    g.add((c, RDF.type, MW.ErrorConstraint if sev == "error" else MW.WarningConstraint))
    g.add((c, MW.hasConstraintId, Literal(cid)))
    g.add((c, RDFS.comment, Literal(comment, lang="en")))
cvar, cid_ = var("c"), var("cid")
rule("V_ViolatedConstraint",
     [atom("data", MW.violatesConstraint, x, cid_), atom("class", MW.ValidationConstraint, cvar),
      atom("data", MW.hasConstraintId, cvar, cid_)],
     [atom("obj", MW.violates, x, cvar)],
     comment="Links an element to the catalogue entry of each constraint it violates.")


def some(prop, filler):
    r = BNode()
    g.add((r, RDF.type, OWL.Restriction))
    g.add((r, OWL.onProperty, prop))
    g.add((r, OWL.someValuesFrom, filler))
    return r


for name, filler, comment in (("DeploymentError", MW.ErrorConstraint, "Element that violates an error constraint."),
                              ("DeploymentWarning", MW.WarningConstraint,
                               "Element that violates a warning constraint.")):
    g.remove((MW[name], OWL.equivalentClass, None))
    g.add((MW[name], RDF.type, OWL.Class))
    g.add((MW[name], OWL.equivalentClass, some(MW.violates, filler)))
    g.add((MW[name], RDFS.comment, Literal(comment, lang="en")))

# --- 12. case study: one deployment unit with endpoints and dependencies -----------------
def ind(i, *types):
    g.add((i, RDF.type, OWL.NamedIndividual))
    for t in types:
        g.add((i, RDF.type, t))
    return i


unit = ind(MW.CaseStudyUnit, MW.DeploymentUnit)
CASE = {"MyWebApp": ("web", 80, "256Mi", "alpine"), "PaymentProcessor": ("payment", 8080, "512Mi", "2.1.0"),
        "DataAggregator": ("aggregator", 9090, "2Gi", "1.0.0")}
for ms, (dns, port, mem, tag) in CASE.items():
    g.add((MW[ms], MW.inDeployment, unit))
    g.add((MW[ms], MW.hasIdentifier, Literal(ms)))
    g.add((MW[f"{ms}_ContainerSpec"], MW.hasMemoryLimit, Literal(mem)))
    g.add((MW[f"{ms}_ContainerSpec"], MW.hasImageTag, Literal(tag)))
    ep = ind(MW[f"{ms}_Endpoint"], MW.ServiceEndpoint)
    g.add((ep, MW.inDeployment, unit))
    g.add((ep, MW.hasIdentifier, Literal(f"{ms}_Endpoint")))
    g.add((ep, MW.hasEndpointName, Literal(dns)))
    g.add((ep, MW.hasEndpointPort, Literal(port, datatype=XSD.integer)))
    g.add((ep, MW.hasSelector, Literal(True)))
    g.add((ep, MW.routesTo, MW[ms]))
for owner, key, value, host, port in (("MyWebApp", "PAYMENT_URL", "http://payment:8080/api", "payment", 8080),
                                      ("MyWebApp", "AGGREGATOR_ADDR", "aggregator:9090", "aggregator", 9090),
                                      ("DataAggregator", None, None, "kafka", 9092)):
    if key:
        ev = ind(MW[f"{owner}_EnvVar_{key}"], MW.EnvironmentVariable)
        g.add((ev, MW.hasEnvKey, Literal(key)))
        g.add((ev, MW.hasEnvValue, Literal(value)))
        g.add((MW[f"{owner}_ContainerSpec"], MW.hasEnvironmentVariable, ev))
    dp = ind(MW[f"{owner}_Dependency_{host}"], MW.ServiceDependency)
    g.add((dp, MW.dependsOnHost, Literal(host)))
    g.add((dp, MW.dependsOnPort, Literal(port, datatype=XSD.integer)))
    g.add((MW[owner], MW.hasDependency, dp))

# --- 13. annotations of the entities inherited from the ablated variant --------------------
COMMENTS = {
    "InconsistentDeployment": "Microservice that violates at least one constraint checked by a SWRL rule; the "
                              "severity is given by DeploymentError and DeploymentWarning.",
    "NodePortOutOfRange": "Kubernetes configuration with a NodePort outside the range of the cluster (S3a, S3b).",
    "hasAntiPattern": "Anti-pattern (OMSAC) present in a microservice.",
    "hasQualityMetric": "Quality metric that applies to a microservice.",
    "implements": "Abstract concept implemented by a microservice.",
    "configurationFor": "Cloud provider for which a deployment configuration is written.",
    "throws": "Deployment error raised by a deployment configuration.",
    "utilisePattern": "Pattern (OMSAC) used by a deployment pattern.",
}
for name, inverse in (("antiPatternOf", "hasAntiPattern"), ("qualityMetricOf", "hasQualityMetric"),
                      ("workloadSpecOf", "hasWorkloadSpec"), ("containerSpecOf", "hasContainerSpec"),
                      ("networkSpecOf", "hasNetworkSpec"), ("storageSpecOf", "hasStorageSpec"),
                      ("platformConfigOf", "hasPlatformConfig"), ("resourceOf", "hasResource")):
    COMMENTS[name] = f"Inverse of {inverse}; its domain and range follow from those of {inverse}."
for name, text in COMMENTS.items():
    if (MW[name], RDFS.comment, None) not in g:
        g.add((MW[name], RDFS.comment, Literal(text, lang="en")))

# --- 14. ontology metadata: title and license --------------------------------------
DCTERMS = rdflib.Namespace("http://purl.org/dc/terms/")
g.bind("dcterms", DCTERMS)
g.set((ONT, DCTERMS.title, Literal("MicrOWL: an ontology for the multi-platform validation of microservice deployments", lang="en")))
g.set((ONT, DCTERMS.license, URIRef("https://creativecommons.org/licenses/by/4.0/")))

g.serialize(DST, format="xml")
print("written", DST, len(g), "triples")
