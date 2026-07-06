# Securite

## Modele de menace

On suppose un etudiant sur le VLAN etudiant capable de forger du HTTP, voler une
cle API, envoyer des bodies enormes, ouvrir des streams longs, injecter des
headers, ou chercher un pivot vers le reseau admin.

## Defenses

| Couche | Mesure |
|---|---|
| Reseau | VM bi-NIC, pas de forwarding kernel, ACL DSI |
| Host | nftables en deny-by-default, sysctl anti-forwarding |
| TLS | TLS public cote etudiants, mTLS cote gateway admin |
| nginx | `client_max_body_size`, `limit_req`, `limit_conn`, buffering SSE coupe |
| App | Bearer `llmstu-*`, validation JSON stricte, quotas |
| Egress | URL upstream fixe, `trust_env=False`, cert client, bearer interne |
| systemd | FS readonly, user dedie, no devices, IPAddressDeny/Allow |
| Audit | JSON sans prompt/reponse, IP hashee HMAC |

## Parametres interdits ou bornes

`policy.py` applique une allowlist stricte (`ALLOWED_FIELDS`) : tout champ hors
liste est **silencieusement supprime** avant l'appel upstream. Sont donc ecartes,
entre autres :

- `ignore_eos`, `cache_prompt`, `id_slot`, `system_prompt`
- `samplers`, `mirostat*`, `dry_*`, `xtc_*`
- `logit_bias`, `grammar`, `json_schema`, `response_format` en MVP

Les champs conserves sont bornes : `max_tokens` (plafonne a
`MAX_COMPLETION_TOKENS`), `temperature`, `top_p`, `top_k`, `repeat_penalty`,
`seed`, `stop`, `tools`. Le champ `user` est **toujours ecrase** par
`student:<user_id>`.

### Contenu texte uniquement (anti-SSRF)

Le multimodal n'est pas supporte et est **rejete en 400**. La validation du
contenu (`_validate_content_structure`) n'autorise qu'une chaine, ou une liste
d'items strictement `{"type": "text", "text": <str>}` — allowlist stricte,
aucune autre cle admise. Tout `type` different (`image_url`, `input_audio`, …)
est refuse. C'est une defense anti-SSRF : si un modele vision etait actif cote
gateway admin, un `image_url` pointant vers une ressource interne pourrait etre
suivi cote upstream ; la gateway etudiante coupe ce vecteur a la racine.

### Bornage du champ `stop`

`stop` accepte une chaine ou une liste, mais est borne :

- au plus `MAX_STOP_SEQUENCES` sequences (defaut `4`, aligne OpenAI) → sinon 400 ;
- chaque sequence est une chaine ≤ `MAX_STOP_SEQUENCE_CHARS` caracteres
  (defaut `64`) → sinon 400.

### Content-Type strict

`POST /v1/chat/completions` exige `Content-Type: application/json`. Un autre
type retourne **415** (defense en profondeur, conforme OpenAI) avant tout
parsing du body.

### Comptabilisation anti-contournement de quota

En streaming, si l'etudiant **coupe le flux avant le chunk `usage` final**, la
gateway estime le volume genere a partir des deltas recus
(`~EST_CHARS_PER_TOKEN` caracteres/token, defaut `4`) et impute cette estimation
au quota — la generation GPU n'etant pas gratuite, on ne compte jamais `0` sur
une coupure. Si l'usage exact arrive, il prime (pas de double comptage). La
comptabilisation (`log_usage`) et l'audit sont en **fire-and-forget** : hors du
chemin de reponse, ils n'ajoutent pas de latence.

## Points a auditer avant production

- La gateway admin doit exposer un vhost interne distinct avec verification mTLS.
- La route reseau NIC admin de la VM student doit permettre uniquement l'IP de la
  gateway admin.
- Les logs applicatifs et nginx doivent exclure les bodies.
- Les tests de policy doivent etre enrichis par fuzzing JSON avant ouverture large.

