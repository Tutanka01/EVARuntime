# Recommandations

## Recommandation principale

Mettre en production une gateway etudiante separee, deployee sur une VM bi-NIC,
et ne jamais exposer la gateway admin existante au VLAN etudiant.

Cette approche conserve l'existant, limite la surface d'attaque du serveur GPU et
donne une zone de controle dediee aux usages etudiants.

## Decisions recommandees pour le MVP

| Sujet | Decision recommandee |
|---|---|
| API | `/v1/models` et `/v1/chat/completions` uniquement |
| Auth | cles `llmstu-*`, base SQLite separee, expiration obligatoire |
| Modeles | petits/modeles TP uniquement, pas de 70B par defaut |
| Quotas | 10 RPM, 100k tokens/jour, 1 stream concurrent |
| Prompt | 32 KB max par requete, 8 KB max par message |
| Egress | mTLS obligatoire vers vhost interne gateway admin |
| Logs | audit JSON sans contenu, IP hashee HMAC |
| Provisioning | CLI pour MVP, SSO Shibboleth en cible |

## Points ameliores dans ce sous-projet

- Le code est autonome : pas d'import depuis `../gateway`, donc aucun risque de
  casser le service existant par dependance implicite.
- Les fichiers de deploiement sont fournis comme exemples editables :
  nginx, systemd, nftables, sysctl et env.
- Le validateur est allowlist-first : les parametres non prevus ne partent pas
  vers la gateway admin.
- La documentation separe architecture, securite, API, deploiement et operations.

## Points a ne pas faire en MVP

- Pas de dashboard admin sur la gateway etudiante.
- Pas de cache de reponses.
- Pas de proxy generique.
- Pas d'acces aux endpoints natifs llama.cpp.
- Pas de routage kernel entre les deux interfaces.
- Pas de logs de prompts, completions ou payloads tools.

## Checklist de validation

- La DSI valide la topologie bi-NIC et les ACL.
- Le vhost interne admin refuse tout client sans certificat mTLS valide.
- Les secrets sont remplaces avant lancement : l'application refuse de demarrer
  si `UPSTREAM_API_KEY` ou `AUDIT_HMAC_SECRET` correspondent a un defaut connu,
  commencent par le prefixe `CHANGE_ME`, ou font moins de 32 caracteres.
- Les noms d'interfaces `eth0`/`eth1` et IP dans `deploy/` sont adaptes.
- Un test de charge court verifie que les limites nginx et applicatives coupent
  avant saturation GPU.
- Un test securite verifie : body trop gros, modele interdit, `/admin/*`, cle
  invalide, cle expiree, upstream coupe.

