"""
Cluster — pilotage multi-nœuds (opt-in via CLUSTER_MODE=cluster).

Ce package est court-circuité quand CLUSTER_MODE=local (mode par défaut).
Aucun import depuis ici n'est obligatoire pour un déploiement mono-nœud.

Sous-modules :
  node_protocol  — DTOs Pydantic du protocole orchestrateur ↔ agent
  scheduler      — logique pure de placement (best-fit + éviction simulée)
  nodes_config   — chargement / validation de nodes.yaml
  node_client    — RemoteNodeClient (HTTPS) + LocalNodeAdapter (in-process)
  cluster_manager — orchestrateur multi-nœuds, équivalent du LocalModelManager
"""
