# Architecture cible

## Principe

La gateway etudiante est une passerelle edge bi-reseau. Elle ne remplace pas la
gateway admin : elle la consomme comme backend unique et controle tout ce qui
vient du reseau etudiant.

```text
Etudiants
  |
  | HTTPS TLS 1.3
  v
nginx edge sur VM gw-student, NIC etudiante
  |
  | HTTP local 127.0.0.1:8001
  v
FastAPI gateway-student
  |  auth + quotas + policy + audit
  |
  | HTTPS mTLS, URL fixe, bearer interne
  v
nginx interne gateway admin
  |
  v
gateway/ existante -> llama-server -> GPU L40S
```

## Invariants

1. Aucun paquet du VLAN etudiant n'atteint directement le serveur GPU.
2. Aucune route `/admin/*` n'existe dans la gateway etudiante.
3. Le code applicatif ne construit jamais une URL upstream depuis la requete
   entrante.
4. La base etudiante est separee de la base admin.
5. Une cle etudiante compromise reste bornee par expiration, RPM, tokens/jour et
   concurrence.
6. Le contenu des prompts et generations n'est jamais logge.

## Surface d'API exposee

```text
GET  /health
GET  /v1/models
POST /v1/chat/completions
```

Endpoints explicitement exclus du MVP :

- `/admin/*`
- `/v1/completions`
- `/completion`
- `/v1/tokenize`
- `/v1/detokenize`
- websocket ou proxy generique

## Flux applicatif

1. nginx rejette les gros bodies, les connexions lentes et les chemins inconnus.
2. FastAPI verifie le bearer `llmstu-*`.
3. Le rate limiter applique successivement burst, RPM, tokens/heure,
   tokens/jour, puis la limite de concurrence par etudiant.
4. `policy.py` normalise le JSON et supprime les champs hors allowlist.
5. `upstream.py` relaie vers `UPSTREAM_BASE_URL/v1/chat/completions`.
6. `audit.py` emet une ligne JSON sans contenu sensible.

## Choix important

Le sous-projet ne duplique pas `model_manager` et ne connait pas les ports
`llama-server`. La selection, le chargement et l'eviction GPU restent uniquement
dans la gateway admin existante.

