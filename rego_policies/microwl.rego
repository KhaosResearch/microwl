# =============================================================================
# MicrOWL policies in Rego
# -----------------------------------------------------------------------------
# This file implements, as OPA policies, the same six cross-resource
# constraints that are encoded as SWRL rules in the MicrOWL ontology.
#
# Corresponding SWRL rules:
#   S1  : replicas > 1 AND accessMode = "ReadWriteOnce" -> InconsistentDeployment
#   S2  : containerPort != exposedPort                  -> InconsistentDeployment
#   S3a : kubernetes.nodePort < 30000                   -> NodePortOutOfRange
#   S3b : kubernetes.nodePort > 32767                   -> NodePortOutOfRange
#   S4  : NodePortOutOfRange on k8s config              -> InconsistentDeployment
#   S5  : replicas < 1 AND accessMode = "ReadWriteOnce" -> InconsistentDeployment
#
# Input schema (single JSON object per microservice):
# {
#   "microservice": "ms_legit_000",
#   "replicas": 3,
#   "image": "nginx:alpine",
#   "containerPort": 80,
#   "exposedPort": 80,
#   "storageSize": "10Gi",
#   "accessMode": "ReadWriteOnce" | "ReadWriteMany",
#   "kubernetes": {
#     "serviceType": "NodePort" | "ClusterIP" | "LoadBalancer",
#     "nodePort": 30080         # optional; only for NodePort services
#   }
# }
#
# Package: data.microwl
# =============================================================================

package microwl

# -----------------------------------------------------------------------------
# Rule S1: replica vs. storage access mode consistency
# -----------------------------------------------------------------------------
inconsistent if {
    input.replicas > 1
    input.accessMode == "ReadWriteOnce"
}

# -----------------------------------------------------------------------------
# Rule S2: container port vs. exposed port alignment
# -----------------------------------------------------------------------------
inconsistent if {
    input.containerPort != input.exposedPort
}

# -----------------------------------------------------------------------------
# Rule S3a: NodePort lower bound
# -----------------------------------------------------------------------------
node_port_out_of_range if {
    input.kubernetes
    input.kubernetes.nodePort
    input.kubernetes.nodePort < 30000
}

# -----------------------------------------------------------------------------
# Rule S3b: NodePort upper bound
# -----------------------------------------------------------------------------
node_port_out_of_range if {
    input.kubernetes
    input.kubernetes.nodePort
    input.kubernetes.nodePort > 32767
}

# -----------------------------------------------------------------------------
# Rule S4: propagate NodePortOutOfRange to the microservice
# -----------------------------------------------------------------------------
inconsistent if {
    node_port_out_of_range
}

# -----------------------------------------------------------------------------
# Rule S5: minimum replica for stateful services
# -----------------------------------------------------------------------------
inconsistent if {
    input.replicas < 1
    input.accessMode == "ReadWriteOnce"
}

# -----------------------------------------------------------------------------
# Decision exposed to the caller: the microservice is inconsistent if any
# of the rules above fires.
# -----------------------------------------------------------------------------
deny := true if inconsistent

# -----------------------------------------------------------------------------
# Optional: explanation of which rule fired (for parity with Pellet's
# explanation facility).
# -----------------------------------------------------------------------------
reasons contains "S1_replica_access_mode" if {
    input.replicas > 1
    input.accessMode == "ReadWriteOnce"
}

reasons contains "S2_port_alignment" if {
    input.containerPort != input.exposedPort
}

reasons contains "S3a_nodeport_lower_bound" if {
    input.kubernetes
    input.kubernetes.nodePort
    input.kubernetes.nodePort < 30000
}

reasons contains "S3b_nodeport_upper_bound" if {
    input.kubernetes
    input.kubernetes.nodePort
    input.kubernetes.nodePort > 32767
}

reasons contains "S5_minimum_replica_stateful" if {
    input.replicas < 1
    input.accessMode == "ReadWriteOnce"
}
