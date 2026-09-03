# ADR-003 — ReplicaGroup et placement multi-GPU

- **Statut :** accepté pour la roadmap, à valider par benchmark terrain
- **Date :** 2026-09-03

## Contexte

Le cluster actuel place un modèle entier sur un seul nœud. Cette représentation
ne suffit pas pour plusieurs réplicas ni pour un grand modèle réparti entre
plusieurs GPU ou nœuds.

## Décision

- L’unité de placement cible devient un `ReplicaGroup`, réservé comme un tout.
- Une réservation suit `prepare → commit → rollback` et porte une génération,
  une lease et une deadline.
- Le premier chemin multi-GPU est intra-nœud, avec inventaire par UUID et
  topologie.
- Le premier chemin multi-nœud privilégie TP intra-nœud et PP entre nœuds,
  après validation de la fabric réseau.
- Un seul rang défaillant rend le groupe entier non routable.
- Le RPC multi-hôte expérimental de llama.cpp reste hors production par défaut.

## Conséquences

Le mapping mémoire `model → node` devra migrer vers
`deployment → replicas → ranks → GPU UUID`. Le scheduler, le protocole agent,
les métriques et les tests E2E doivent évoluer ensemble.
