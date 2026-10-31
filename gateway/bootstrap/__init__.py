"""
`eva-bootstrap` — planificateur d'amorçage (Lot B, jalon M1).

Les renvois `§N` de ces modules visent le plan de consolidation retiré du dépôt,
consultable par `git log -- codex-analyse.md`.

Ce paquet est la couche NON privilégiée du parcours « machine vierge → premier
token ». `gateway/deploy/install.sh` reste la couche
privilégiée : utilisateurs système, répertoires, venv, systemd, nginx. Ici on ne
fait qu'inventorier, résoudre, recommander et **écrire un plan** — jamais
l'appliquer. Aucun module de ce paquet ne doit acquérir de privilèges, ni écrire
hors d'un chemin explicitement fourni par l'appelant.

Deux jalons, deux couches. **M1 — planifier** : dire ce qui sera installé, et
pourquoi. **M2 — appliquer** : le faire, et en rendre compte. Les deux couches
ne se mélangent pas ; un producteur de M1 n'exécute jamais rien, un exécuteur de
M2 ne décide jamais de ce qu'il y a à faire.

M1 — un module par producteur :

| Module               | Item    | Rôle                                           |
|----------------------|---------|------------------------------------------------|
| `schema`             | AUT-001 | Contrat du plan : structure, validation, rendu |
| `inventory`          | AUT-002 | Inventaire matériel (CPU/RAM/disque/GPU)       |
| `runtime_resolver`   | AUT-003 | Résolution de `llama-server` et provenance     |
| `llmfit`             | AUT-004 | Adaptateur JSON du conseiller LLMfit           |
| `catalog`            | AUT-005 | Catalogue de modèles approuvés et licences     |
| `gguf_meta`          | AUT-013 | Lecture du header GGUF                         |
| `planner`            | AUT-001 | Assemblage du plan à partir des producteurs    |

M2 — un module par exécuteur, plus le contrat et les deux documents finaux :

| Module               | Item    | Rôle                                           |
|----------------------|---------|------------------------------------------------|
| `execution`          | AUT-015 | Contrat d'exécution : modes, journal, registre |
| `runtime_installer`  | AUT-016 | Pose de l'artefact `llama-server` vérifié      |
| `downloader`         | AUT-006 | Licences, téléchargement et vérification GGUF  |
| `registry_writer`    | AUT-007 | Écriture de `models.yaml`, activation sur preuve |
| `calibration`        | AUT-008 | Chargement réel : pics, TTFT, `vram_gb` proposé |
| `first_token`        | AUT-009 | Recette du premier token par le chemin public  |
| `warmup`             | AUT-010 | Pré-chauffage avant ouverture du trafic        |
| `install_report`     | AUT-011 | Rapport d'installation : ce qui tourne, d'où   |
| `applier`            | AUT-015 | Assemblage : câblage, chaîne des preuves       |
| `production`         | AUT-017 | Clients/sondes réels et relecture du runtime   |

La règle d'indépendance, telle qu'elle est réellement appliquée
---------------------------------------------------------------
Ni les producteurs ni les exécuteurs ne s'importent **entre pairs**. C'est ce
qui permet d'en faire évoluer un — ou de le mettre en échec — sans casser les
autres, et c'est ce qui a permis de mener six chantiers en parallèle contre des
contrats figés.

Ce qui est en revanche permis, et pratiqué :

- **importer un contrat** — `schema` (le plan) et `execution` (le journal). Ce
  sont des contrats, pas des pairs : ils n'exécutent rien et ne dépendent de
  personne ;
- **importer le module qui possède la DÉCISION qu'on applique**.
  `runtime_installer` importe `runtime_resolver` parce qu'il pose l'artefact que
  celui-ci a choisi : réécrire chez lui la forme d'un manifeste de provenance
  aurait créé une seconde définition, et c'est toujours celle qui n'a pas été
  mise à jour qui installe le mauvais binaire. La dépendance va de l'exécuteur
  vers le décideur, jamais l'inverse ;
- **importer un module de la gateway** quand c'est lui qui fait autorité :
  `registry_writer` valide son candidat avec `model_registry.ModelRegistry`
  lui-même plutôt qu'avec une reformulation de ses règles.

Ce qui reste interdit : qu'un exécuteur en importe un autre. `registry_writer`
n'importe donc ni `calibration` ni `first_token`, alors même qu'il consomme
leurs preuves — elles lui sont FOURNIES. Seuls `planner` (côté M1) et `applier`
(côté M2) connaissent tout le monde, et c'est là que se font tous les raccords.
"""
