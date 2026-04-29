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

`policy.py` supprime les champs inconnus et les champs llama.cpp dangereux :

- `ignore_eos`, `cache_prompt`, `id_slot`, `system_prompt`
- `samplers`, `mirostat*`, `dry_*`, `xtc_*`
- `logit_bias`, `grammar`, `json_schema`, `response_format` en MVP

Les champs conserves sont bornes : `max_tokens`, `temperature`, `top_p`, `top_k`,
`repeat_penalty`, `seed`, `stop`, `tools`.

## Points a auditer avant production

- La gateway admin doit exposer un vhost interne distinct avec verification mTLS.
- La route reseau NIC admin de la VM student doit permettre uniquement l'IP de la
  gateway admin.
- Les logs applicatifs et nginx doivent exclure les bodies.
- Les tests de policy doivent etre enrichis par fuzzing JSON avant ouverture large.

