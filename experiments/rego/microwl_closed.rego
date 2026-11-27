package microwl_closed

# Closed-world variant of every MicrOWL constraint: S2c instead of S2, plus the constraints that
# need negation as failure (S9, S10, S12, S13, S14, S16). S10, S12 and S13 are not evaluated on
# deployment units marked as incomplete. Input: {"configs": [Config...]}.

workloads contains c if {
	some c in input.configs
	c.kind == "workload"
}

endpoints contains e if {
	some e in input.configs
	e.kind == "endpoint"
}

violation contains [c.id, "S1"] if {
	some c in workloads
	c.replicas > 1
	some s in c.storage
	s.scope == "shared"
	s.access_mode == "ReadWriteOnce"
}

violation contains [c.id, "S2c"] if {
	some c in workloads
	count(c.container_ports) > 0
	some p in c.ports
	p.target != null
	not p.target in c.container_ports
}

violation contains [c.id, "S3"] if {
	some c in workloads
	c.platform == "kubernetes"
	some p in c.ports
	p.node_port != null
	not valid_node_port(p.node_port)
}

valid_node_port(n) if {
	n >= 30000
	n <= 32767
}

violation contains [c.id, "S5"] if {
	some c in workloads
	c.replicas != null
	c.replicas < 1
	some s in c.storage
	s.access_mode == "ReadWriteOnce"
}

violation contains [c.id, "S7"] if {
	some c in workloads
	c.replicas > 1
	some p in c.ports
	p.host_port != null
}

violation contains [e.id, "S8"] if {
	some e in endpoints
	e.deployment != null
	some e2 in endpoints
	e2.deployment == e.deployment
	e2.id != e.id
	some p in e.ports
	p.node_port != null
	some p2 in e2.ports
	p2.node_port == p.node_port
}

violation contains [c.id, "S8"] if {
	some c in workloads
	c.platform == "compose"
	c.deployment != null
	some c2 in workloads
	c2.platform == "compose"
	c2.deployment == c.deployment
	c2.id != c.id
	some p in c.ports
	p.host_port != null
	some p2 in c2.ports
	p2.host_port == p.host_port
}

violation contains [c.id, "S11"] if {
	some c in workloads
	c.replicas > 1
	c.image_tag == "latest"
}

violation contains [c.id, "S15"] if {
	some c in workloads
	c.replicas > 1
	some s in c.storage
	s.scope == "shared"
	s.access_mode == "ReadWriteOncePod"
}

# ---- constraints that need negation as failure -------------------------------------

named(e, h) if {
	some n in e.names
	n == h
}

resolved(c, d) := {e |
	some e in endpoints
	e.deployment == c.deployment
	named(e, d.host)
}

unresolved(c, d) if {
	c.deployment != null
	not c.incomplete
	count(resolved(c, d)) == 0
}

wrong_port(c, d) if {
	d.port != null
	some e in resolved(c, d)
	count([p | some p in e.ports; p.exposed != null]) > 0
	not serves(e, d.port)
}

wrong_port(c, d) if {
	d.port_name != null
	some e in resolved(c, d)
	not serves_name(e, d.port_name)
}

serves(e, port) if {
	some p in e.ports
	p.exposed == port
}

serves_name(e, name) if {
	some p in e.ports
	p.name == name
}

violation contains [c.id, "S9"] if {
	some c in workloads
	c.deployment != null
	some d in c.dependencies
	wrong_port(c, d)
}

violation contains [c.id, "S10"] if {
	some c in workloads
	some d in c.dependencies
	unresolved(c, d)
}

violation contains [e.id, "S12"] if {
	some e in endpoints
	e.selector
	not e.incomplete
	count(e.routes_to) == 0
}

violation contains [i.id, "S13"] if {
	some i in input.configs
	i.kind == "ingress"
	i.deployment != null
	some d in i.dependencies
	unresolved(i, d)
}

violation contains [i.id, "S13"] if {
	some i in input.configs
	i.kind == "ingress"
	i.deployment != null
	some d in i.dependencies
	wrong_port(i, d)
}

violation contains [c.id, "S14"] if {
	some c in workloads
	count(c.container_ports) > 0
	some pp in c.probe_ports
	not pp in c.container_ports
}

violation contains [c.id, "S16"] if {
	some c in workloads
	c.memory_limit == null
}

flagged contains id if some [id, _] in violation
