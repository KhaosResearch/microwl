package microwl

# Rego counterpart of the SWRL rules of MicrOWL with the same (open-world) semantics, including
# the open-world S2 (S3 covers S3a, S3b and S4; S8 covers S8a and S8b).
# Input: {"configs": [Config...]} (workloads, endpoints and Ingresses).

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

violation contains [c.id, "S2"] if {
	some c in workloads
	some cp in c.container_ports
	some p in c.ports
	p.target != null
	cp != p.target
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

flagged contains id if some [id, _] in violation
