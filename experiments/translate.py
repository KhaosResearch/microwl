"""Translators from concrete deployment descriptors to MicrOWL `Config` objects.

* Kubernetes manifests (Deployment, StatefulSet, ReplicaSet, ReplicationController,
  DaemonSet + Service + PersistentVolumeClaim + Ingress + LimitRange), grouped by directory.
* Docker Compose files (services, ports, deploy.replicas, expose, named volumes).
* AWS ECS task definitions (JSON) and CloudFormation templates with ECS services.

Besides one workload per microservice, the translators emit the endpoints through which
the workloads are reached (Kubernetes Services, Compose service names, ECS load-balancer
targets) and the Kubernetes Ingresses, all tagged with their deployment unit (the group,
file or template), so that names and ports can be resolved within a unit.

The translators only read the files (yaml.safe_load / json.load); nothing is executed.
"""
import copy
import json
import re
from collections import defaultdict
from pathlib import Path

import yaml

from microwl_lib import Config, Port, Storage, Dependency

WORKLOADS = {"Deployment", "StatefulSet", "ReplicaSet", "ReplicationController", "DaemonSet"}
POD_PRODUCERS = {"Pod", "Job", "CronJob"}      # selectable by Services, but not microservices


# ----------------------------------------------------------------------------- shared helpers
def image_tag(image):
    """Tag of an image reference: the digest if pinned, "latest" if neither tag nor digest."""
    if not isinstance(image, str) or not image or "$" in image or "{" in image:
        return None
    if "@" in image:
        return image.split("@", 1)[1]
    last = image.rsplit("/", 1)[-1]
    return last.split(":", 1)[1] if ":" in last else "latest"


HOST_KEY = re.compile(r"_?(HOST|HOSTNAME|ADDR|ADDRESS|SERVER|SERVICE|ENDPOINT)$", re.I)
URL_RE = re.compile(r"^[a-z][a-z0-9+.-]*://(?:[^@/]*@)?(?P<host>[^:/?#,@]+)(?::(?P<port>\d+))?(?:[/?#].*)?$", re.I)
HOSTPORT_RE = re.compile(r"^(?P<host>[A-Za-z0-9._-]+):(?P<port>\d+)$")
LABEL = re.compile(r"^[a-z0-9]([a-z0-9_-]*[a-z0-9])?$")
LOCAL_HOSTS = {"localhost", "0.0.0.0", "127.0.0.1"}
NOT_HOSTS = {"true", "false", "yes", "no", "on", "off", "none", "null", "enabled", "disabled"}


def env_dependencies(env, canon):
    """Dependencies declared in environment variables (dict name -> literal value).

    A value is read as a reference to another service when it is a URL with a host, a
    host:port pair, or, for variables whose name ends in HOST, ADDR, SERVER, SERVICE or ENDPOINT,
    a bare host name (the port is then taken from the variable with the same prefix and the
    suffix PORT, if any). canon() maps a host name to the canonical name of an endpoint of the
    platform, or None for hosts that cannot name a service of the deployment (IP addresses,
    localhost, external domains). Values with variable references are ignored."""
    deps = []
    for key, value in env.items():
        if not isinstance(value, str) or "$" in value or "{" in value:
            continue
        for item in value.split(","):
            item = item.strip()
            m = URL_RE.match(item) or HOSTPORT_RE.match(item)
            if m:
                host, port = m.group("host"), m.group("port")
            elif HOST_KEY.search(key) and re.fullmatch(r"[A-Za-z0-9._-]+", item):
                host, port = item, None
                prefix = HOST_KEY.sub("", key)
                sib = env.get(f"{prefix}_PORT") if prefix else None
                if isinstance(sib, str) and sib.isdigit():
                    port = sib
            else:
                continue
            if host.lower() in LOCAL_HOSTS | NOT_HOSTS or host.replace(".", "").isdigit():
                continue
            name = canon(host)
            if name:
                deps.append(Dependency(name, int(port) if port else None, None, key))
    return deps


def k8s_canon(ns, namespaces):
    """Canonical Kubernetes Service name (name.namespace) of a host, resolved from namespace ns."""
    def canon(host):
        h = host.lower().rstrip(".")
        for suffix in (".svc.cluster.local", ".svc"):
            if h.endswith(suffix):
                parts = h[: -len(suffix)].split(".")
                return f"{parts[-2]}.{parts[-1]}" if len(parts) >= 2 and LABEL.match(parts[-2]) else None
        parts = h.split(".")
        if len(parts) == 1 and LABEL.match(h) and "_" not in h:
            return f"{h}.{ns}"
        if len(parts) == 2 and parts[1] in namespaces and LABEL.match(parts[0]):
            return h
        return None
    return canon


def compose_canon(host):
    return host if LABEL.match(host) and "." not in host else None


# ----------------------------------------------------------------------------- k8s
def load_k8s_docs(path: Path):
    text = path.read_text(errors="ignore")
    if "{{" in text:                     # Helm/Go templates are not plain manifests
        return []
    docs = []
    try:
        for d in yaml.safe_load_all(text):
            if isinstance(d, dict) and d.get("kind") == "List":
                docs.extend(i for i in d.get("items", []) if isinstance(i, dict))
            elif isinstance(d, dict) and "kind" in d:
                docs.append(d)
    except yaml.YAMLError:
        return []
    return docs


def k8s_group_to_configs(docs, source, platform="kubernetes", incomplete=False):
    """Translate the manifests of one group (directory or file) to Configs: one workload per
    workload manifest, one endpoint per Service and one ingress per Ingress."""
    ns = lambda d: (d.get("metadata") or {}).get("namespace", "default")
    namespaces = {ns(d) for d in docs} | {(d.get("metadata") or {}).get("name") for d in docs
                                          if d.get("kind") == "Namespace"}
    svc_by_key = {}
    for d in docs:                                # a Service defined twice is one object (last wins)
        if d.get("kind") == "Service":
            svc_by_key[((d.get("metadata") or {}).get("name"), ns(d))] = d
    services = list(svc_by_key.values())
    pvcs = {((d.get("metadata") or {}).get("name"), ns(d)): d for d in docs if d.get("kind") == "PersistentVolumeClaim"}
    configmaps = {((d.get("metadata") or {}).get("name"), ns(d)): {k: v for k, v in (d.get("data") or {}).items()
                                                                if isinstance(v, str)}
                  for d in docs if d.get("kind") == "ConfigMap"}
    default_memory = {ns(d) for d in docs if d.get("kind") == "LimitRange" and any(
        ((l.get("default") or {}).get("memory")) for l in ((d.get("spec") or {}).get("limits") or [])
        if isinstance(l, dict) and l.get("type", "Container") == "Container")}
    cfgs, selectable = [], []                     # selectable: (labels, namespace, workload id or None)
    for d in docs:
        kind = d.get("kind")
        if kind in POD_PRODUCERS:
            spec = d.get("spec") or {}
            if kind == "CronJob":
                spec = ((spec.get("jobTemplate") or {}).get("spec")) or {}
            labels = ((d.get("metadata") or {}).get("labels") if kind == "Pod"
                      else ((spec.get("template") or {}).get("metadata") or {}).get("labels")) or {}
            selectable.append((labels, ns(d), None))
        if kind not in WORKLOADS:
            continue
        spec = d.get("spec") or {}
        tmpl = spec.get("template") or {}
        pspec = tmpl.get("spec") or {}
        labels = (tmpl.get("metadata") or {}).get("labels") or {}
        containers = [ct for ct in pspec.get("containers") or [] if isinstance(ct, dict)]
        if not containers:
            continue
        name = (d.get("metadata") or {}).get("name", "unnamed")
        c = Config(id=f"{source}::{kind}/{name}", platform=platform, source=source, deployment=source)
        c.replicas = None if kind == "DaemonSet" else spec.get("replicas", 1)
        c.image = containers[0].get("image")
        c.image_tag = image_tag(c.image)
        named = {}
        host_ports = []
        for ct in containers:
            for p in ct.get("ports") or []:
                if "containerPort" in p:
                    c.container_ports.append(p["containerPort"])
                    if p.get("name"):
                        named[p["name"]] = p["containerPort"]
                    if pspec.get("hostNetwork"):
                        host_ports.append(p["containerPort"])
                    elif p.get("hostPort"):
                        host_ports.append(p["hostPort"])
        for ct in containers:
            for probe in ("livenessProbe", "readinessProbe", "startupProbe"):
                for handler in ("httpGet", "tcpSocket", "grpc"):
                    port = ((ct.get(probe) or {}).get(handler) or {}).get("port")
                    if isinstance(port, str) and not port.isdigit():
                        if port in named:
                            c.probe_ports.append(named[port])
                        else:
                            c.notes.append(f"unresolved named probe port {port}")
                    elif port is not None:
                        c.probe_ports.append(int(port))
        limits = [((ct.get("resources") or {}).get("limits") or {}).get("memory") for ct in containers]
        if all(limits):
            c.memory_limit = str(limits[0])
        elif ns(d) in default_memory:
            c.memory_limit = "LimitRange default"
        env = {}
        for ct in containers:
            for ef in ct.get("envFrom") or []:            # all keys of a ConfigMap of the group
                ref = (ef or {}).get("configMapRef") or {}
                cm = configmaps.get((ref.get("name"), ns(d)))
                if cm is not None:
                    prefix = (ef or {}).get("prefix", "")
                    env.update({f"{prefix}{k}": v for k, v in cm.items()})
                elif ref:
                    c.notes.append(f"ConfigMap {ref.get('name')} not found in group")
            for e in ct.get("env") or []:
                if not isinstance(e, dict):
                    continue
                if "value" in e:
                    env[e.get("name")] = e["value"]
                ref = ((e.get("valueFrom") or {}).get("configMapKeyRef")) or {}
                cm = configmaps.get((ref.get("name"), ns(d))) if ref else None
                if cm is not None and ref.get("key") in cm:
                    env[e.get("name")] = cm[ref["key"]]
        c.dependencies = env_dependencies(env, k8s_canon(ns(d), namespaces))
        if len(containers) > 1:
            c.notes.append("multi-container pod")
        selectable.append((labels, ns(d), c.id))
        # services selecting the pods
        for s in services:
            sel = (s.get("spec") or {}).get("selector") or {}
            if not sel or ns(s) != ns(d) or any(labels.get(k) != v for k, v in sel.items()):
                continue
            stype = (s.get("spec") or {}).get("type", "ClusterIP")
            for p in (s.get("spec") or {}).get("ports") or []:
                tp = p.get("targetPort", p.get("port"))
                if isinstance(tp, str):
                    if tp in named:
                        tp = named[tp]
                    elif tp.isdigit():
                        tp = int(tp)
                    else:
                        c.notes.append(f"unresolved named targetPort {tp}")
                        tp = None
                np = p.get("nodePort") if stype in ("NodePort", "LoadBalancer") else None
                c.ports.append(Port(exposed=p.get("port"), target=tp, node_port=np))
        c.ports += [Port(host_port=hp) for hp in host_ports]
        # storage
        for v in pspec.get("volumes") or []:
            claim = (v.get("persistentVolumeClaim") or {}).get("claimName")
            if claim is None:
                continue
            pvc = pvcs.get((claim, ns(d)))
            if pvc is None:
                c.storage.append(Storage(None, None, "shared"))
                c.notes.append(f"PVC {claim} not found in group")
                continue
            modes = (pvc.get("spec") or {}).get("accessModes") or []
            size = (((pvc.get("spec") or {}).get("resources") or {}).get("requests") or {}).get("storage")
            mode = "ReadWriteMany" if "ReadWriteMany" in modes else (modes[0] if modes else None)
            c.storage.append(Storage(mode, size, "shared"))
        for vct in spec.get("volumeClaimTemplates") or []:
            modes = (vct.get("spec") or {}).get("accessModes") or []
            size = (((vct.get("spec") or {}).get("resources") or {}).get("requests") or {}).get("storage")
            c.storage.append(Storage(modes[0] if modes else None, size, "perReplica"))
        cfgs.append(c)
    # endpoints: one per Service
    for s in services:
        sname = (s.get("metadata") or {}).get("name", "unnamed")
        sspec = s.get("spec") or {}
        sel = sspec.get("selector") or {}
        stype = sspec.get("type", "ClusterIP")
        e = Config(id=f"{source}::Service/{sname}", platform=platform, kind="endpoint", source=source,
                   deployment=source, names=[f"{sname}.{ns(s)}"], selector=bool(sel) and stype != "ExternalName")
        for p in sspec.get("ports") or []:
            np = p.get("nodePort") if stype in ("NodePort", "LoadBalancer") else None
            e.ports.append(Port(exposed=p.get("port"), node_port=np, name=p.get("name")))
            tp = p.get("targetPort")
            if sspec.get("clusterIP") == "None" and isinstance(tp, int) and tp != p.get("port"):
                e.ports.append(Port(exposed=tp))      # headless: clients reach the pods directly
        if e.selector:
            matches = [wid for labels, lns, wid in selectable
                       if lns == ns(s) and all(labels.get(k) == v for k, v in sel.items())]
            e.routes_to = [w for w in matches if w is not None]
            if matches and not e.routes_to:
                e.selector = False
                e.notes.append("selects pods that are not workloads")
        cfgs.append(e)
    # a microservice does not depend on itself: drop dependencies on an endpoint that routes to it
    own = defaultdict(set)
    for e in cfgs:
        if e.kind == "endpoint":
            for w in e.routes_to:
                own[w].update(e.names)
    for c in cfgs:
        if c.kind == "workload" and c.dependencies:
            c.dependencies = [x for x in c.dependencies if x.host not in own[c.id]]
    # ingresses
    for d in docs:
        if d.get("kind") != "Ingress":
            continue
        iname = (d.get("metadata") or {}).get("name", "unnamed")
        ispec = d.get("spec") or {}
        i = Config(id=f"{source}::Ingress/{iname}", platform=platform, kind="ingress", source=source,
                   deployment=source)
        backends = [ispec.get("defaultBackend"), ispec.get("backend")]
        for rule in ispec.get("rules") or []:
            for path in ((rule or {}).get("http") or {}).get("paths") or []:
                backends.append((path or {}).get("backend"))
        for b in backends:
            if not isinstance(b, dict):
                continue
            if "service" in b:
                svc = b["service"] or {}
                host, port = svc.get("name"), svc.get("port") or {}
                pnum, pname = port.get("number"), port.get("name")
            elif "serviceName" in b:
                host, sp = b.get("serviceName"), b.get("servicePort")
                pnum = int(sp) if isinstance(sp, int) or (isinstance(sp, str) and sp.isdigit()) else None
                pname = sp if isinstance(sp, str) and not sp.isdigit() else None
            else:
                continue
            if isinstance(host, str) and "{" not in host:
                i.dependencies.append(Dependency(f"{host}.{ns(d)}", pnum, pname, "backend"))
        cfgs.append(i)
    for c in cfgs:
        c.incomplete = incomplete
    return cfgs


def is_templated(path: Path):
    return "{{" in path.read_text(errors="ignore")


def k8s_corpus(root: Path, files):
    """Group manifest files by directory and translate every group. A group that contains
    templated manifests, which are skipped, is marked as incomplete."""
    groups = defaultdict(list)
    for f in files:
        for d in load_k8s_docs(f):
            groups[f.parent].append(d)
    incomplete = {f.parent for f in files if is_templated(f)}
    cfgs = []
    for directory, docs in sorted(groups.items()):
        cfgs.extend(k8s_group_to_configs(docs, str(directory.relative_to(root)),
                                         incomplete=directory in incomplete))
    return cfgs, groups


# ----------------------------------------------------------------------------- compose
PORT_RE = re.compile(r"^(?:(?P<ip>\d+\.\d+\.\d+\.\d+):)?(?:(?P<host>[\d-]+):)?(?P<cont>[\d-]+)(?:/(?:tcp|udp))?$")


def merge_compose(base, override):
    """Merge a Compose override file into a base file (services merged key by key)."""
    out = copy.deepcopy(base)
    for k, v in (override or {}).items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            for name, svc in v.items():
                if isinstance(svc, dict) and isinstance(out[k].get(name), dict):
                    out[k][name] = {**out[k][name], **svc}
                else:
                    out[k][name] = svc
        else:
            out[k] = v
    return out


def compose_to_configs(path: Path, root: Path, doc=None):
    """Translate a Compose file (or an already merged document `doc` identified by `path`)."""
    if doc is None:
        try:
            doc = yaml.safe_load(path.read_text(errors="ignore"))
        except yaml.YAMLError:
            return []
    if not isinstance(doc, dict):
        return []
    named_volumes = set((doc.get("volumes") or {}).keys())
    src = str(path.relative_to(root))
    cfgs, endpoints = [], []
    for sname, s in (doc.get("services") or {}).items():
        if not isinstance(s, dict):
            continue
        c = Config(id=f"{src}::{sname}", platform="compose", source=src, deployment=src)
        c.image = s.get("image")
        c.image_tag = image_tag(c.image)
        c.replicas = ((s.get("deploy") or {}).get("replicas"))
        for e in s.get("expose") or []:
            try:
                c.container_ports.append(int(str(e).split("/")[0]))
            except ValueError:
                pass
        for p in s.get("ports") or []:
            if isinstance(p, dict):
                pub = int(p["published"]) if str(p.get("published", "")).isdigit() else None
                c.ports.append(Port(exposed=pub, target=int(p["target"]) if "target" in p else None, host_port=pub))
                continue
            m = PORT_RE.match(str(p).strip())
            if not m or "-" in str(p):
                c.notes.append(f"unparsed port {p}")
                continue
            host, cont = m.group("host"), int(m.group("cont"))
            c.ports.append(Port(exposed=int(host) if host else None, target=cont,
                                host_port=int(host) if host else None))
        for v in s.get("volumes") or []:
            vname = v.get("source") if isinstance(v, dict) else str(v).split(":")[0]
            if vname in named_volumes:
                c.storage.append(Storage(None, None, "shared"))
        mem = s.get("mem_limit") or ((((s.get("deploy") or {}).get("resources") or {}).get("limits") or {}).get("memory"))
        c.memory_limit = str(mem) if mem else None
        env = s.get("environment") or {}
        if isinstance(env, list):
            env = dict(str(x).split("=", 1) for x in env if "=" in str(x))
        c.dependencies = env_dependencies(env if isinstance(env, dict) else {}, compose_canon)
        cfgs.append(c)
        # the service is reachable by its name (and container name and network aliases) on the
        # container ports
        names = [sname] + ([s["container_name"]] if isinstance(s.get("container_name"), str) else [])
        nets = s.get("networks")
        if isinstance(nets, dict):
            for n in nets.values():
                names += [a for a in ((n or {}).get("aliases") or []) if isinstance(a, str)]
        c.dependencies = [x for x in c.dependencies if x.host not in names]   # not on itself
        for link in s.get("links") or []:
            if ":" in str(link):
                endpoints.append((str(link).split(":")[0], str(link).split(":")[1]))
        ports = sorted(set(c.container_ports) | {p.target for p in c.ports if p.target is not None})
        cfgs.append(Config(id=f"{src}::{sname}@endpoint", platform="compose", kind="endpoint", source=src,
                           deployment=src, names=names, ports=[Port(exposed=p) for p in ports],
                           routes_to=[c.id]))
    for target, alias in endpoints:               # link aliases name the linked service
        for e in cfgs:
            if e.kind == "endpoint" and target in e.names and alias not in e.names:
                e.names.append(alias)
    return cfgs


# ----------------------------------------------------------------------------- ecs
def ecs_to_configs(path: Path, root: Path):
    try:
        doc = json.loads(path.read_text(errors="ignore"))
    except json.JSONDecodeError:
        return []
    defs = doc.get("containerDefinitions") if isinstance(doc, dict) else None
    if not defs:
        return []
    src = str(path.relative_to(root))
    c = Config(id=f"{src}::{doc.get('family', path.stem)}", platform="ecs", source=src, deployment=src)
    c.image = defs[0].get("image")
    c.image_tag = image_tag(c.image)
    mode = ecs_network_mode(doc.get("networkMode"), doc.get("requiresCompatibilities"))
    for d in defs:
        for pm in d.get("portMappings") or []:
            if "containerPort" in pm:
                c.container_ports.append(pm["containerPort"])
                c.ports.append(Port(exposed=pm.get("hostPort", pm["containerPort"]), target=pm["containerPort"],
                                    host_port=ecs_host_port(mode, pm.get("hostPort"), pm["containerPort"])))
    if doc.get("memory") or all(d.get("memory") for d in defs):
        c.memory_limit = str(doc.get("memory") or defs[0].get("memory"))
    for v in doc.get("volumes") or []:
        c.storage.append(Storage(None, None, "shared"))
    if len(defs) > 1:
        c.notes.append("multi-container task")
    return [c]


def ecs_network_mode(mode, compatibilities):
    """Network mode of a task definition: bridge by default, awsvpc for Fargate."""
    if isinstance(mode, str):
        return mode
    return "awsvpc" if isinstance(compatibilities, list) and "FARGATE" in compatibilities else "bridge"


def ecs_host_port(mode, host_port, container_port):
    """Port bound on the container instance by every task (none in awsvpc mode or with a dynamic port)."""
    if mode == "host":
        return container_port
    if mode == "bridge" and isinstance(host_port, int) and host_port > 0:
        return host_port
    return None


# ----------------------------------------------------------------------------- ecs (CloudFormation)
class CfnLoader(yaml.SafeLoader):
    """SafeLoader that accepts the CloudFormation short-form intrinsic functions (!Ref, !Sub, ...)."""


def _cfn_tag(loader, suffix, node):
    if isinstance(node, yaml.ScalarNode):
        value = loader.construct_scalar(node)
    elif isinstance(node, yaml.SequenceNode):
        value = loader.construct_sequence(node, deep=True)
    else:
        value = loader.construct_mapping(node, deep=True)
    return {"Ref" if suffix == "Ref" else f"Fn::{suffix}": value}


CfnLoader.add_multi_constructor("!", _cfn_tag)
UNRESOLVED = object()


def _resolve(v, params):
    """Literal value, or the Default of a referenced parameter; UNRESOLVED otherwise."""
    if isinstance(v, dict):
        if set(v) == {"Ref"} and isinstance(v["Ref"], str):
            p = params.get(v["Ref"])
            return p["Default"] if isinstance(p, dict) and "Default" in p else UNRESOLVED
        return UNRESOLVED
    return v


def _int(v, params):
    v = _resolve(v, params)
    if isinstance(v, bool) or v is UNRESOLVED:
        return None
    try:
        return int(v)
    except (TypeError, ValueError):
        return None


def ecs_cfn_to_configs(path: Path, root: Path):
    """One Config per AWS::ECS::Service whose task definition is defined in the same template.

    The task definition provides image, container ports and task volumes; the service provides the
    replicas (DesiredCount, 1 when omitted, none for the DAEMON strategy), the container ports targeted
    by its load balancers and service registries, and the EBS volumes attached to each task.
    EFS volumes can be mounted read-write by every task (shared, ReadWriteMany); managed EBS volumes
    are created for each task (per-replica, ReadWriteOnce).
    """
    doc = load_cfn(path)
    return ecs_cfn_doc_to_configs(doc, str(path.relative_to(root))) if doc is not None else []


def load_cfn(path: Path):
    """Parsed CloudFormation template (dict with Resources), or None."""
    try:
        text = path.read_text(errors="ignore")
        doc = json.loads(text) if path.suffix == ".json" else yaml.load(text, Loader=CfnLoader)
    except (json.JSONDecodeError, yaml.YAMLError):
        return None
    return doc if isinstance(doc, dict) and isinstance(doc.get("Resources"), dict) else None


def ecs_cfn_doc_to_configs(doc, src):
    res, params = doc["Resources"], doc.get("Parameters") or {}
    cfgs, endpoints = [], []
    for lid, r in res.items():
        if not isinstance(r, dict) or r.get("Type") != "AWS::ECS::Service":
            continue
        props = r.get("Properties") or {}
        td_ref = props.get("TaskDefinition")
        td = res.get(td_ref["Ref"]) if isinstance(td_ref, dict) and isinstance(td_ref.get("Ref"), str) else None
        if not isinstance(td, dict) or td.get("Type") != "AWS::ECS::TaskDefinition":
            continue                         # task definition not defined in this template
        tdp = td.get("Properties") or {}
        defs = [d for d in tdp.get("ContainerDefinitions") or [] if isinstance(d, dict)]
        c = Config(id=f"{src}::{lid}", platform="ecs", source=src, deployment=src)
        if props.get("SchedulingStrategy") == "DAEMON":
            c.replicas = None
        elif "DesiredCount" in props:
            c.replicas = _int(props["DesiredCount"], params)
            if c.replicas is None:
                c.notes.append("unresolved DesiredCount")
        else:
            c.replicas = 1
        image = _resolve(defs[0].get("Image"), params) if defs else None
        c.image = image if isinstance(image, str) else None
        c.image_tag = image_tag(c.image)
        mode = ecs_network_mode(_resolve(tdp.get("NetworkMode"), params), tdp.get("RequiresCompatibilities"))
        if mode is UNRESOLVED:
            mode = None
        for d in defs:
            for pm in d.get("PortMappings") or []:
                cp = _int(pm.get("ContainerPort"), params) if isinstance(pm, dict) else None
                if cp is not None:
                    c.container_ports.append(cp)
                    hp = ecs_host_port(mode, _int(pm.get("HostPort"), params), cp)
                    if hp is not None:
                        c.ports.append(Port(host_port=hp))
        mem = _resolve(tdp.get("Memory"), params)
        cmem = [_resolve(d.get("Memory"), params) for d in defs]
        if mem not in (None, UNRESOLVED):
            c.memory_limit = str(mem)
        elif defs and all(m not in (None, UNRESOLVED) for m in cmem):
            c.memory_limit = str(cmem[0])
        cnames = {_resolve(d.get("Name"), params) for d in defs}
        host_ports, c.ports = c.ports, []
        for key in ("LoadBalancers", "ServiceRegistries"):
            for lb in props.get(key) or []:
                if isinstance(lb, dict) and "ContainerPort" in lb:
                    tp = _int(lb["ContainerPort"], params)
                    if tp is None:
                        c.notes.append(f"unresolved {key} ContainerPort")
                    else:
                        c.ports.append(Port(exposed=None, target=tp))
                if isinstance(lb, dict) and key == "LoadBalancers":
                    cname = _resolve(lb.get("ContainerName"), params)
                    if cname is UNRESOLVED:
                        continue
                    k = sum(1 for x in endpoints if x.id.startswith(c.id + "@lb"))
                    endpoints.append(Config(id=f"{c.id}@lb{k}", platform="ecs", kind="endpoint", source=src,
                                            deployment=src, selector=True,
                                            routes_to=[c.id] if cname is None or cname in cnames else []))
        for v in tdp.get("Volumes") or []:
            if not isinstance(v, dict):
                continue
            if "EFSVolumeConfiguration" in v:
                c.storage.append(Storage("ReadWriteMany", None, "shared"))
            elif "ConfiguredAtLaunch" in v:
                pass                         # provided by the service (managed EBS, below)
            else:
                c.storage.append(Storage(None, None, "shared"))
        for vc in props.get("VolumeConfigurations") or []:
            ebs = vc.get("ManagedEBSVolume") if isinstance(vc, dict) else None
            if isinstance(ebs, dict):
                size = _int(ebs.get("SizeInGiB"), params)
                c.storage.append(Storage("ReadWriteOnce", f"{size}Gi" if size else None, "perReplica"))
        c.ports += host_ports
        if len(defs) > 1:
            c.notes.append("multi-container task")
        cfgs.append(c)
    return cfgs + endpoints


# ----------------------------------------------------------------------------- coverage
FIELDS = {
    "hasReplicas": lambda c: c.replicas is not None,
    "hasContainerImage": lambda c: bool(c.image),
    "hasContainerPort": lambda c: bool(c.container_ports),
    "hasExposedPort": lambda c: any(p.exposed is not None for p in c.ports),
    "hasTargetPort": lambda c: any(p.target is not None for p in c.ports),
    "hasAccessMode": lambda c: any(s.access_mode for s in c.storage),
    "hasStorageSize": lambda c: any(s.size for s in c.storage),
    "hasHostPort": lambda c: any(p.host_port is not None for p in c.ports),
    "hasImageTag": lambda c: bool(c.image_tag),
    "hasProbePort": lambda c: bool(c.probe_ports),
    "hasMemoryLimit": lambda c: bool(c.memory_limit),
    "hasDependency": lambda c: bool(c.dependencies),
}


def coverage(cfgs):
    cfgs = [c for c in cfgs if c.kind == "workload"]
    n = len(cfgs)
    return {k: round(sum(1 for c in cfgs if f(c)) / n, 3) if n else None for k, f in FIELDS.items()}


def mutate(c: Config, cat: str):
    """Inject one misconfiguration into a translated Config (manifest-level semantics)."""
    m = copy.deepcopy(c)
    m.id = f"{c.id}#{cat}"
    m.expected = cat
    if cat == "M1":
        m.replicas = 3
        m.storage = [Storage("ReadWriteOnce", "1Gi", "shared")]
    elif cat == "M2":
        m.ports[0].target = m.container_ports[0] + 1
    elif cat == "M3":
        m.ports[0].node_port = 8080
    elif cat == "M4":
        m.ports[0].node_port = 40000
    elif cat == "M5":
        m.replicas = 0
        m.storage = [Storage("ReadWriteOnce", "1Gi", "shared")]
    return m
