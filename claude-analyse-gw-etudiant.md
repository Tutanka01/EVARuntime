# Analyse — Gateway étudiante (edge proxy bi-réseau) pour EVARuntime

> **Objectif** : exposer le service d'inférence LLM EVA (actuellement hébergé sur le
> réseau d'administration UPPA) au **réseau étudiant**, sans déplacer la gateway
> existante hors du réseau admin et sans ouvrir directement le L40S vers les
> étudiants.
>
> **Solution proposée** : un second service — la **gw-étudiante** — déployé sur
> une VM/host dédiée à **deux interfaces réseau** (une patte côté étudiant, une
> patte côté admin). Ce service agit comme **edge proxy durci** qui parle en
> sortie à la gateway EVA classique (`llm.eva.univ-pau.fr`) sur le réseau admin.
>
> Ce document décrit la **menace**, l'**architecture cible**, les **invariants
> de sécurité**, l'**implémentation recommandée** (réutiliser ~70% du code
> existant) et un **plan de déploiement** étape par étape.

---

## 1. Contexte et contraintes

### Existant (rappel rapide)

- `EVARuntime/gateway` est une **FastAPI + nginx** qui :
  - termine TLS,
  - authentifie via `Bearer llmgw-…` (SHA-256 en base SQLite WAL),
  - applique un rate limit sliding window in-memory,
  - proxie vers des sous-processus `llama-server` locaux,
  - expose `/admin/*` filtré par IP campus.
- Cette gateway tourne sur le **serveur GPU L40S**, **dans le réseau admin**.
  Elle est la dernière ligne de défense devant le moteur d'inférence.

### Pourquoi ne pas exposer cette gateway au réseau étudiant ?

1. **Surface d'attaque locale** : la gateway co-réside avec le L40S et avec
   `llama-server` sur `127.0.0.1`. Une RCE ou SSRF dans la gateway = accès direct
   au moteur d'inférence et au GPU.
2. **Couplage opérationnel** : `/admin/*`, le dashboard HTML, le hot-reload de
   `llama_params`, la route `/admin/unload`, le CLI… vivent dans le même process.
   Tout filtrage par IP est **un seul mauvais `location` nginx** loin d'une fuite.
3. **Modèle de menace différent** : le réseau étudiant est **non-confiance** (BYOD,
   wifi, possiblement compromis). On ne lui doit ni confidentialité des
   paramètres internes, ni accès aux mêmes modèles, ni aux mêmes quotas.
4. **Souveraineté réseau** : la DSI veut éviter qu'une machine du VLAN admin
   réponde à des paquets venant du VLAN étudiant. La règle d'or réseau (« un
   service = un VLAN ») doit être respectée.

### Cahier des charges de la gw-étudiante

| Exigence | Détail |
|---|---|
| **Bi-NIC** | une IP côté VLAN étudiant, une IP côté VLAN admin (ou DMZ↔admin) |
| **Fail-closed** | si la gw-admin est injoignable → 503, jamais de fallback dégradé |
| **Sous-ensemble strict de l'API** | seulement les endpoints OpenAI sûrs et nécessaires |
| **Pas d'accès admin** | `/admin/*`, `/admin/dashboard`, hot-reload : **interdit** |
| **Authentification dédiée** | clés étudiantes séparées, base de données séparée |
| **Quotas étudiants** | RPM, taille de prompt, tokens/jour, longueur du stream |
| **Hardening défensif** | TLS, mTLS interne, isolation systemd, nftables, sysctl |
| **Auditabilité** | log immuable de chaque requête (qui, quoi, quand, combien) |
| **Faible empreinte** | aucun GPU requis, ~256 MB RAM, mono-process |
| **Indépendance opérationnelle** | redémarrer la gw-étudiante n'impacte pas la gw-admin |

---

## 2. Modèle de menace

On suppose un **étudiant motivé sur le VLAN étudiant**, capable de :

- forger n'importe quelle requête HTTP/HTTPS,
- envoyer du trafic à haut débit (DoS L7 type slowloris, bodies géants),
- voler la clé API d'un autre étudiant (phishing, dépôt git public),
- tenter des injections (path traversal, header smuggling, JSON déformé),
- tenter de faire pivoter via la gateway vers le réseau admin (SSRF, header
  `Host`, redirection ouverte),
- tenter d'épuiser le GPU (prompt énorme, `n_predict` géant, contexte saturé,
  flot de connexions streaming jamais lues).

**Hors-scope** (accepté comme risque résiduel) :

- attaque physique sur la machine,
- compromission de la PKI UPPA,
- attaque sur le contenu généré par le LLM (prompt injection contre l'app cliente,
  pas contre nous) — on n'en est pas responsable.

**Invariants à préserver** (ce qu'on défend) :

1. Aucun paquet venant du VLAN étudiant n'atteint le L40S directement.
2. Aucun étudiant ne peut atteindre `/admin/*` de la gw-admin.
3. Aucun étudiant ne peut faire faire à la gw-étudiante une requête sortante
   arbitraire (SSRF) vers une autre destination que la gw-admin.
4. Une clé étudiante compromise est révocable instantanément, sans impact sur
   les utilisateurs admin.
5. Un étudiant ne peut pas **monopoliser** le GPU (taille, RPM, concurrence,
   tokens/jour bornés).
6. Le contenu généré pour un étudiant n'est jamais visible par un autre
   étudiant ni par un admin (pas de cache partagé, pas de log du body).

---

## 3. Architecture cible

### Topologie réseau

```
┌────────────────────────────────────────────────────────────────────────┐
│  VLAN étudiant (10.20.0.0/16)                                          │
│                                                                        │
│  Étudiants ─── HTTPS (TLS 1.3) ────────────┐                          │
│  (laptop/wifi)                              │                          │
└─────────────────────────────────────────────┼──────────────────────────┘
                                              │
                              ┌───────────────▼────────────────┐
                              │  gw-étudiante (VM dédiée)       │
                              │  ─ NIC0  10.20.0.50  (étudiant) │
                              │  ─ NIC1  10.10.0.50  (admin)    │
                              │                                 │
                              │  nginx (TLS, limits, WAF)       │
                              │     │                           │
                              │  FastAPI edge proxy (port 8001) │
                              │     │ HTTPS + mTLS              │
                              └─────┼───────────────────────────┘
                                    │
┌───────────────────────────────────┼──────────────────────────────────┐
│  VLAN admin (10.10.0.0/16)        │                                  │
│                                   ▼                                  │
│  ┌──────────────────────────────────────────────────────────────┐   │
│  │  gw-admin (existante) — llm.eva.univ-pau.fr                  │   │
│  │  nginx → FastAPI → llama-server → L40S                       │   │
│  └──────────────────────────────────────────────────────────────┘   │
│                                                                      │
│  Personnels/doctorants ─── HTTPS ────► gw-admin directement          │
└──────────────────────────────────────────────────────────────────────┘
```

### Règles de filtrage réseau (nftables sur la VM gw-étudiante)

- **NIC0 (étudiant)** :
  - input : 443/tcp uniquement, tout le reste droppé,
  - forward : drop (la machine n'est pas un routeur — `net.ipv4.ip_forward=0`),
  - output sur NIC0 : seulement vers les clients qui ont initié la connexion
    (conntrack `established,related`).
- **NIC1 (admin)** :
  - output : autorisé uniquement vers l'IP de gw-admin sur 443/tcp,
  - input : seulement réponses `established,related`.
- **Pas de routage** entre les deux NICs au niveau kernel. Le seul lien entre
  les deux mondes est le **process FastAPI** : c'est un point de contrôle unique,
  testable, auditable.

### Couches de défense (defense-in-depth)

| Couche | Rôle | Technologie |
|---|---|---|
| 1. Réseau | isolation VLAN, ACL switch | DSI / firewall campus |
| 2. Host | pas de forwarding kernel, nftables strict | sysctl + nftables |
| 3. TLS edge | TLS 1.3, HSTS, ciphers modernes | nginx |
| 4. WAF L7 | limites de body, headers, conn., burst | nginx (`limit_req`, `limit_conn`) |
| 5. App | auth Bearer, rate limit, schema validation | FastAPI + Pydantic |
| 6. Quota métier | RPM, tokens/jour, taille prompt | applicatif |
| 7. Egress | mTLS vers gw-admin uniquement, allowlist | httpx + cert client |
| 8. Process | systemd hardening, user dédié, FS readonly | systemd unit |
| 9. Audit | log immuable signé, journald → rsyslog distant | journald + rsyslog |

---

## 4. Implémentation — réutiliser EVARuntime

**Principe directeur : ne pas réécrire ce qui marche.** La gw-étudiante est une
**variante simplifiée** de la gw-admin. On garde tout ce qui est solide
(`auth.py`, `rate_limiter.py`, `database.py`, `schemas.py`) et on **enlève**
tout ce qui parle au GPU.

### Arborescence proposée

```
gateway-student/                       # nouveau package, séparé de gateway/
├── main.py                            # FastAPI minimal, 5 routes max
├── config.py                          # settings dédié (.env distinct)
├── database.py                        # repris tel quel (users/keys/usage)
├── auth.py                            # repris (Bearer SHA-256)
├── rate_limiter.py                    # repris + extensions (tokens/jour)
├── upstream.py                        # NOUVEAU : client httpx vers gw-admin (mTLS, allowlist)
├── policy.py                          # NOUVEAU : validation body, modèles autorisés, limites
├── audit.py                           # NOUVEAU : log structuré sans contenu
├── schemas.py                         # sous-ensemble (chat completions seulement)
├── cli.py                             # gestion users/keys étudiants
├── requirements.txt
└── deploy/
    ├── install.sh
    ├── llm-gateway-student.service    # systemd hardening renforcé
    └── nginx.conf                     # config nginx étudiante
```

### Composants clés

#### 4.1 `main.py` — surface d'API minimaliste

Routes exposées **et c'est tout** :

```
GET  /v1/models                  ← liste filtrée (allowlist)
POST /v1/chat/completions        ← seul endpoint d'inférence, streaming OK
GET  /health                     ← liveness (pas d'info sensible)
```

Pas de `/v1/completions` legacy. Pas de `/v1/completion` natif llama.cpp
(trop riche, paramètres dangereux comme `n_predict` non bornés). Pas de
`/v1/tokenize` ni `/v1/detokenize` (utiles mais non essentiels — on peut les
ouvrir plus tard si besoin). **Aucun** préfixe `/admin/*`.

#### 4.2 `policy.py` — validateur de requêtes

Avant tout appel upstream, on **valide et normalise** le body. Toute requête
qui ne passe pas → 400 sans la transmettre.

Règles strictes :

- `model` : ∈ allowlist (sous-ensemble de la registry de la gw-admin, ex:
  uniquement `llama-3.1-8b-instruct` et `qwen-9b` — pas le 70B pour les étudiants).
- `messages` : présent, liste non vide, longueur ≤ 32, chaque `content` ≤ 8 KB.
- Total du prompt (somme des `content`) ≤ une limite (ex: 32 KB) ; mesuré côté gw,
  pas confiance dans `Content-Length`.
- `max_tokens` / `n_predict` : forcé à `min(demandé, 2048)`.
- `stream` : autorisé (`true` ou `false`).
- `temperature`, `top_p`, `top_k`, `repeat_penalty`, `seed`, `stop` : autorisés
  dans des bornes.
- `tools`, `tool_choice` : autorisés (passe-through), borne sur la taille JSON
  des tools (≤ 16 KB).
- **Champs ignorés/strippés** : `logit_bias`, `mirostat*`, `dry_*`, `xtc_*`,
  `ignore_eos`, `samplers`, `cache_prompt`, `system_prompt` (slot global llama),
  `id_slot`, `t_max_*`, tout champ inconnu.
- `user` : injecté côté gw avec l'ID étudiant (jamais ce que le client a envoyé).
- Headers entrants : seuls `Authorization`, `Content-Type`, `Accept` sont lus.
  Tout le reste est **drop avant forward**.

Implémentation : Pydantic strict (`extra="forbid"`) pour le schéma, plus une
validation sémantique manuelle pour la taille prompt et les bornes numériques.

#### 4.3 `upstream.py` — egress contrôlé

C'est **le seul endroit** du process qui ouvre une socket sortante. Verrouillé :

- URL upstream **hardcodée par config** (`UPSTREAM_BASE_URL=https://10.10.0.10`),
  jamais dérivée du body, des headers ou de l'URL entrante.
- Client `httpx.AsyncClient` partagé, configuré avec :
  - `verify=/etc/llm-gateway-student/ca-uppa.pem` (pin du CA UPPA),
  - `cert=(client.crt, client.key)` (mTLS — la gw-admin doit valider notre
    certificat client),
  - `trust_env=False` (ignore `HTTP_PROXY`/env, pas de pivot via proxy),
  - timeouts explicites (connect 5s, read 600s pour stream, write 30s),
  - `max_connections=64`, `max_keepalive=16`.
- Headers sortants reconstruits à la main : `Authorization: Bearer <KEY_GW_STUDENT>`,
  `Content-Type: application/json`, `X-Forwarded-User: <user_id_anonymisé>`.
  **Ne pas forwarder** `X-Forwarded-For` brut (privacy : on remplace par un hash).
- Body sortant = body **revalidé** par `policy.py`, jamais le body brut entrant.

L'invariant clé : **un attaquant ne peut pas faire émettre à `upstream.py` une
requête vers une autre IP que celle de la gw-admin** — l'URL n'est ni dans le
body, ni dans la query, ni dans les headers.

#### 4.4 `rate_limiter.py` — quotas étudiants renforcés

On garde le sliding window in-memory pour le RPM (par user). On **ajoute** :

- Quota **tokens/jour** (compteur en base SQLite, atomique). Décrémenté après
  chaque réponse à partir du `usage` upstream.
- Quota **concurrence** : N streams simultanés max par user (par défaut 1 ou 2).
  Implémenté avec un `asyncio.Semaphore` par `user_id`.
- Quota **prompt total/heure** en KB (anti-abus de prompts massifs).

Le rate limit **applicatif** double celui de nginx (`limit_req` + `limit_conn`).
nginx coupe les abus grossiers, l'app coupe les abus fins par utilisateur.

#### 4.5 `audit.py` — traçabilité sans fuite

Log structuré JSON par requête, vers journald (puis rsyslog distant immuable
côté DSI). **Champs loggés** :

- `ts`, `request_id` (uuid), `student_user_id`, `key_id_hash8`,
- `model_requested`, `model_resolved`,
- `prompt_chars`, `prompt_tokens`, `completion_tokens`, `duration_ms`,
- `status`, `error_class` (jamais le message brut),
- `client_ip_hash` (HMAC + sel quotidien — pas l'IP en clair pour RGPD).

**Jamais loggés** : `messages[*].content`, body brut, valeurs des tools,
contenu généré. Si la DSI veut du forensic pour incident, on peut activer un
**mode debug** par feature flag, signé temporellement, avec rotation 24h.

#### 4.6 `database.py` — base étudiante séparée

Schéma identique à la gw-admin (réutiliser le code), **fichier différent** :
`/var/lib/llm-gateway-student/students.db`. Aucune référence croisée. Cela
permet :

- révocation indépendante (un compromis sur la base étudiante n'expose pas les
  clés admin/personnels),
- politique de mots de passe et durée de vie des clés différente (ex: clé
  étudiante valable 1 semestre, expiration `expires_at` obligatoire),
- export/destruction RGPD séparé (fin de scolarité = `DELETE FROM users WHERE …`).

---

## 5. Hardening — détail des couches

### 5.1 nginx (gw-étudiante)

```nginx
# /etc/nginx/sites-available/llm-gateway-student

# Limites par IP
limit_req_zone  $binary_remote_addr zone=stu_req:10m rate=30r/m;
limit_conn_zone $binary_remote_addr zone=stu_conn:10m;

server {
    listen 10.20.0.50:443 ssl http2;
    server_name llm-students.univ-pau.fr;

    ssl_certificate     /etc/ssl/certs/llm-students.crt;
    ssl_certificate_key /etc/ssl/private/llm-students.key;
    ssl_protocols       TLSv1.3;                # 1.3 only — clients étudiants modernes
    ssl_ciphers         TLS_AES_256_GCM_SHA384:TLS_CHACHA20_POLY1305_SHA256;
    ssl_session_cache   shared:SSL:10m;

    # Headers de sécurité
    add_header Strict-Transport-Security "max-age=63072000; includeSubDomains; preload" always;
    add_header X-Content-Type-Options nosniff always;
    add_header X-Frame-Options DENY always;
    add_header Content-Security-Policy "default-src 'none'" always;
    add_header Referrer-Policy no-referrer always;

    # Bornes brutes
    client_max_body_size       64k;     # bien plus serré que la gw-admin (10m)
    client_body_buffer_size    16k;
    client_header_buffer_size  4k;
    large_client_header_buffers 2 8k;
    keepalive_timeout          15s;
    send_timeout               20s;

    # Anti-slowloris
    client_body_timeout        10s;
    client_header_timeout      10s;

    location = /v1/models {
        limit_req  zone=stu_req burst=5 nodelay;
        limit_conn stu_conn 4;
        proxy_pass http://127.0.0.1:8001;
        include /etc/nginx/snippets/proxy-common.conf;
    }

    location = /v1/chat/completions {
        limit_req  zone=stu_req burst=5 nodelay;
        limit_conn stu_conn 2;            # max 2 streams simultanés par IP côté nginx
        proxy_pass http://127.0.0.1:8001;
        include /etc/nginx/snippets/proxy-sse.conf;   # buffering off, X-Accel-Buffering no, read 600s
    }

    location = /health {
        proxy_pass http://127.0.0.1:8001;
        access_log off;
    }

    # Tout le reste — y compris /admin/* — interdit
    location / { return 404; }
}
```

Points-clé :

- `client_max_body_size 64k` : un prompt de 64 KB en JSON couvre largement
  l'enseignement standard ; tout body plus grand est rejeté avant de toucher
  Python.
- `limit_conn 2` : empêche un client unique d'ouvrir 50 streams pour épuiser
  le pool de slots de `llama-server`.
- TLS 1.3 only : 2026, on peut s'offrir ça (vérifier la compat des clients
  étudiants Python ≥ 3.7 — OK depuis OpenSSL 1.1.1).

### 5.2 systemd unit

```ini
[Unit]
Description=LLM Inference Gateway STUDENT (FastAPI edge proxy)
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=llmstudent
Group=llmstudent
WorkingDirectory=/opt/llm-gateway-student
EnvironmentFile=/etc/llm-gateway-student/env

ExecStart=/opt/llm-gateway-student/venv/bin/uvicorn \
    main:app --host 127.0.0.1 --port 8001 --workers 1 --loop uvloop --access-log

# Hardening agressif (plus que la gw-admin — pas de GPU à toucher ici)
NoNewPrivileges=true
PrivateTmp=true
PrivateDevices=true                 # pas de /dev/nvidia*, on n'en a pas besoin
ProtectSystem=strict
ProtectHome=true
ProtectKernelTunables=true
ProtectKernelModules=true
ProtectControlGroups=true
ProtectClock=true
ProtectHostname=true
RestrictNamespaces=true
RestrictRealtime=true
RestrictSUIDSGID=true
LockPersonality=true
MemoryDenyWriteExecute=true
SystemCallArchitectures=native
SystemCallFilter=@system-service
SystemCallFilter=~@privileged @resources @mount @reboot @swap @debug
CapabilityBoundingSet=
AmbientCapabilities=
ReadWritePaths=/var/lib/llm-gateway-student /var/log/llm-gateway-student

# IP egress whitelisting au niveau systemd (Linux ≥ 5.0)
IPAddressDeny=any
IPAddressAllow=10.10.0.10            # IP de gw-admin uniquement
IPAddressAllow=127.0.0.0/8

LimitNOFILE=8192
LimitNPROC=512
TasksMax=256
MemoryHigh=512M
MemoryMax=1G

Restart=on-failure
RestartSec=5s

[Install]
WantedBy=multi-user.target
```

`IPAddressDeny=any` + `IPAddressAllow=10.10.0.10` est une **deuxième barrière**
en plus de nftables : même si un bug Python tente de contacter `evil.com`, le
kernel coupe au niveau cgroup. Ceinture **et** bretelles.

### 5.3 sysctl (host)

```
# /etc/sysctl.d/99-llm-gw-student.conf
net.ipv4.ip_forward = 0                  # la machine n'est PAS un routeur
net.ipv6.conf.all.forwarding = 0
net.ipv4.conf.all.rp_filter = 1          # anti-spoofing
net.ipv4.conf.default.rp_filter = 1
net.ipv4.conf.all.accept_redirects = 0
net.ipv4.conf.all.send_redirects = 0
net.ipv4.tcp_syncookies = 1
net.ipv4.tcp_max_syn_backlog = 4096
kernel.kptr_restrict = 2
kernel.dmesg_restrict = 1
fs.protected_hardlinks = 1
fs.protected_symlinks = 1
```

### 5.4 nftables (extrait)

```
table inet filter {
  chain input {
    type filter hook input priority 0; policy drop;
    ct state established,related accept
    iif lo accept
    iif "eth0" tcp dport 443 accept       # eth0 = NIC étudiant
    # tout le reste (NIC admin, autres ports) = drop
  }
  chain output {
    type filter hook output priority 0; policy drop;
    ct state established,related accept
    oif lo accept
    oif "eth1" ip daddr 10.10.0.10 tcp dport 443 accept   # eth1 = NIC admin → gw-admin
    # DNS/NTP : autoriser explicitement vers serveurs DSI internes uniquement
    oif "eth1" ip daddr 10.10.0.53 udp dport 53 accept
  }
  chain forward {
    type filter hook forward priority 0; policy drop;     # AUCUN forwarding
  }
}
```

### 5.5 mTLS sortant (gw-étudiante → gw-admin)

Pour que la gw-admin **rejette toute requête venant d'autre chose que la
gw-étudiante** (en plus du Bearer token), on ajoute un certificat client :

- DSI émet un cert client `gw-student.crt` signé par la PKI UPPA.
- Côté gw-admin nginx :
  ```nginx
  ssl_client_certificate /etc/ssl/certs/uppa-ca.pem;
  ssl_verify_client on;
  ssl_verify_depth 2;
  ```
  …mais seulement sur un **vhost dédié** (`llm-internal.eva.univ-pau.fr`)
  réservé à l'inter-gateway, pas sur le vhost public actuel.
- La gw-admin se trouve donc avec **deux entrées** : la publique (clients
  campus authentifiés) et l'interne mTLS (uniquement la gw-étudiante).

Cela ferme un scénario : un étudiant qui obtiendrait par phishing une clé
`Bearer` valide sur la gw-admin **ne peut pas l'utiliser depuis le réseau
étudiant** sans le cert client correspondant.

---

## 6. Authentification & gestion des utilisateurs étudiants

### Modèle de clé

- Format : `llmstu-<26 chars base32>` (préfixe distinct de `llmgw-` pour ne
  pas confondre les bases).
- Hashées SHA-256 (réutilise `database.py`).
- **Expiration obligatoire** (`expires_at NOT NULL`) — fin de semestre par défaut.
- Renouvellement via portail self-service (out-of-scope de ce doc, mais à
  prévoir avec la DSI : SSO Shibboleth UPPA → génération de clé).

### Provisioning

Deux options, à arbitrer avec la DSI :

1. **Manuel CLI** (MVP) : l'admin crée les comptes/clés via `cli.py`, distribue
   par mail UPPA. Simple, traçable, lent.
2. **Self-service via SSO** : portail web qui valide l'authent Shibboleth
   (CAS UPPA), crée le compte côté gw-étudiante, retourne la clé. Plus
   ergonomique, plus de dev. **Recommandé en cible**, pas en MVP.

### Quotas par défaut suggérés

| Quota | Valeur | Justification |
|---|---|---|
| RPM | 10 | usage TP/exo, pas un service prod |
| Tokens/jour | 100 000 | équivalent ~50 conversations longues |
| Streams concurrents | 1 | empêche l'épuisement de slots du modèle |
| Prompt max | 8 KB | tient un énoncé d'exo + un peu de contexte |
| Reply max | 2 048 tokens | borne `max_tokens` |
| Modèles autorisés | `llama-3.1-8b`, `qwen-9b` | pas de 70B aux étudiants |

Tout cela est par-utilisateur dans la base, ajustable au cas par cas pour les
TER/PFE qui ont besoin de plus.

---

## 7. Compatibilité streaming SSE

Le streaming est le point le plus délicat — les chunks doivent traverser :

```
client ── nginx-edge ── FastAPI-edge ── nginx-admin ── FastAPI-admin ── llama-server
```

Cinq sauts. Pour qu'un token sorte en temps réel :

- nginx-edge : `proxy_buffering off`, `X-Accel-Buffering: no` (on a ces
  headers déjà dans la gw-admin, à reproduire).
- FastAPI-edge : reproduire le pattern `_stream_proxy` de `proxy.py` —
  passer chaque ligne `data: …` telle quelle, sans buffering applicatif.
- httpx en streaming : `client.stream("POST", …) → response.aiter_lines()`,
  ne **pas** appeler `response.read()`.
- Pas de transformation du JSON sauf la réécriture du champ `model` (déjà
  fait dans la gw-admin) — éviter de re-bufferiser pour reformater.
- `_INFERENCE_TIMEOUT` côté edge ≥ celui côté admin (sinon timeout edge avant
  fin du stream admin → SSE coupé brutalement).

Le code de `_stream_proxy` (`gateway/proxy.py:211-330`) est directement
adaptable — c'est ~80 lignes à recopier en simplifiant (pas besoin du mode
`tools` bufferisé si on n'autorise pas les tools côté étudiant en MVP).

### Pin/unpin et concurrence GPU

La gw-étudiante **n'a aucune connaissance** du `model_manager` côté admin.
C'est volontaire : le pin/unpin reste **internal** à la gw-admin, qui voit
chaque requête edge comme un client normal. Le `Semaphore` étudiant côté gw
edge garantit que les étudiants n'ouvrent jamais plus de N streams concurrents,
ce qui est la borne supérieure du nombre de slots étudiants jamais réservés
sur le L40S.

---

## 8. Feuille de route — déploiement par phases

### Phase 0 — Préparation (sans code)

1. Demander à la DSI :
   - VM 2 vCPU / 2 GB RAM / 20 GB disque, deux NICs (VLAN étudiant + VLAN admin).
   - DNS interne : `llm-students.univ-pau.fr` → IP NIC0.
   - Cert TLS public pour `llm-students.univ-pau.fr`.
   - Cert client `gw-student.crt` signé par CA UPPA pour mTLS interne.
   - Vhost interne sur la gw-admin (`llm-internal.eva.univ-pau.fr`) ouvert
     uniquement à l'IP NIC1 de la gw-étudiante.
   - Plage IP étudiante autorisée à atteindre NIC0:443.
2. Décider de la liste des **modèles exposés aux étudiants** (par défaut :
   `llama-3.1-8b-instruct`, `qwen-9b` — pas de 70B).

### Phase 1 — MVP fonctionnel (1-2 semaines)

1. Créer `gateway-student/` à côté de `gateway/`.
2. Copier `database.py`, `auth.py`, `rate_limiter.py`, `schemas.py` adaptés.
3. Écrire `policy.py` (validation stricte du body).
4. Écrire `upstream.py` (httpx mTLS, allowlist URL, streaming pass-through).
5. Écrire `main.py` (3 routes : `/health`, `/v1/models`, `/v1/chat/completions`).
6. Tests :
   - unitaire : `policy.py` rejette les bodies hors-bornes, modèles non-allowlist,
     champs interdits.
   - intégration : un `httpx.AsyncClient` mock simulant la gw-admin, vérifier
     pass-through streaming + non-streaming.
   - sécurité : essayer SSRF (`model: "http://evil"`), header smuggling,
     body 1 MB, JSON malformé, Bearer absent → toujours 4xx, jamais 5xx.

### Phase 2 — Hardening (1 semaine)

1. nginx config + cert TLS public.
2. systemd unit avec hardening complet.
3. nftables + sysctl.
4. mTLS configuré côté gw-admin (vhost interne).
5. Pen-test interne : un collègue ou audit DSI tape sur `llm-students.univ-pau.fr`
   avec OWASP ZAP / nuclei / fuzz custom. Corriger ce qui sort.

### Phase 3 — Provisioning et exploitation (en parallèle de la phase 2)

1. CLI étudiant : `add-student`, `create-key`, `revoke-key`, `usage-report`.
2. Procédure d'incident documentée (qui appelle qui, comment révoquer en
   masse, comment basculer la gw-étudiante en mode lecture seule).
3. Dashboard admin **côté gw-admin** : vue "étudiants" agrégée (le `user`
   forwardé en mTLS sert à attribuer les requêtes étudiantes).

### Phase 4 — Self-service SSO (mois suivant, hors MVP)

Portail web qui authentifie via Shibboleth UPPA et génère des clés.

---

## 9. Évaluation de risque résiduel

| Risque | Probabilité | Impact | Mitigation |
|---|---|---|---|
| Clé étudiante volée | élevée | faible (quotas serrés, expiration) | révocation rapide, expiration semestrielle |
| DoS L7 d'un étudiant | moyenne | moyen | nginx limits + applicatif + `IPAddressAllow` au cgroup |
| Bug dans `policy.py` (validateur incomplet) | moyenne | élevé (SSRF, élévation) | tests fuzz + revue indépendante avant prod |
| Compromission de la VM gw-étudiante | faible | élevé (pivot vers gw-admin) | mTLS + bearer interne + gw-admin garde son auth/rate-limit |
| Compromission de la gw-admin | faible | très élevé | hors-scope (existait déjà avant ce projet) |
| Fuite RGPD (logs avec IP/contenu) | moyenne | moyen | hash IP, pas de log de contenu, rétention bornée |

---

## 10. Décisions à valider avec toi avant code

1. **Modèles exposés aux étudiants** : quel sous-ensemble ? (recommandation :
   `llama-3.1-8b`, `qwen-9b` ; pas le 70B).
2. **Provisioning MVP** : CLI manuel ou direct self-service SSO ?
3. **mTLS interne entre gw-étudiante et gw-admin** : OK pour demander un cert
   client à la DSI ? (sinon on retombe sur Bearer + IP allowlist côté nginx-admin,
   moins fort mais faisable).
4. **Quotas étudiants par défaut** : valeurs proposées section 6 OK, ou plus
   serrées ?
5. **Logs** : journald → rsyslog distant immuable côté DSI, ou fichier local
   rotaté ? (recommandation : rsyslog distant pour audit forensic).
6. **Vie de la base étudiante** : politique de purge en fin de semestre — la
   DSI / le DPO ont-ils une exigence formelle (RGPD) ?
7. **Streaming concurrent par étudiant** : 1 ou 2 ? (recommandation : 1, on
   relâche si les retours utilisateurs le justifient).

Une fois ces points tranchés, le code Phase 1 peut être écrit en quelques
jours en réutilisant largement `gateway/`.

---

## Annexe A — Ce qu'on **ne fait pas** (et pourquoi)

- **Pas de cache de réponses** : un étudiant pose la même question qu'un autre →
  on régénère. Pas de fuite croisée, pas de surprise sur la fraîcheur.
- **Pas de filtrage de contenu (modération)** : ce n'est pas le rôle de
  l'edge proxy. Si un jour la DSI le veut, c'est un middleware additionnel
  qui s'insère dans `policy.py`, pas une refonte.
- **Pas de WebSocket** : streaming SSE seulement, surface plus petite, pas
  besoin de WS pour OpenAI.
- **Pas de multi-tenant fin** (groupes, classes) : ajouté plus tard si besoin.
  Au début, un étudiant = un user = des quotas.
- **Pas de duplication du `model_manager`** : la gw-étudiante ne sait pas que
  le GPU existe. Si la gw-admin tombe, la gw-étudiante répond 503 propre. Pas
  de logique GPU dupliquée = pas de désynchronisation possible.
- **Pas de réécriture en Rust/Go** : le code Python existant marche, est
  audité, est familier à l'équipe. Réutilisation > réécriture, pour réduire
  le risque d'introduire de nouveaux bugs sur un service de sécurité critique.

---

## Annexe B — Schéma de flux d'une requête étudiante

```
1. Étudiant ──HTTPS──> nginx-edge:443 (NIC0)
   │                    ├─ TLS termine
   │                    ├─ limit_req (30 r/min/IP)
   │                    ├─ limit_conn (2 streams/IP)
   │                    └─ body ≤ 64 KB
2. nginx-edge ──HTTP──> uvicorn 127.0.0.1:8001
3. FastAPI edge :
   a. auth.py        → Bearer SHA-256 → user étudiant
   b. rate_limiter   → RPM + tokens/jour + concurrence
   c. policy.py      → valide body, normalise, strip champs interdits
                       borne max_tokens, vérifie modèle ∈ allowlist
   d. upstream.py    → POST https://llm-internal.eva.univ-pau.fr/v1/chat/completions
                       avec mTLS + Bearer interne + body normalisé
4. nginx-admin (vhost interne, mTLS requis) → uvicorn gw-admin
5. FastAPI admin → ModelManager → llama-server → L40S
6. Réponse remonte le chemin inverse, streaming sans buffering à chaque saut
7. Audit log côté gw-étudiante : tokens, durée, status (jamais le contenu)
```

---

*Document préparé pour ouvrir la discussion avant de coder. Une fois les
décisions section 10 validées, je peux écrire le squelette `gateway-student/`
et la première version de `policy.py` + tests.*
