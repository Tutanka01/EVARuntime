# EVARuntime node-agent

Le node-agent est **uniquement requis en mode multi-nœuds**. En mode local,
la gateway lance ses `llama-server` directement et conserve son auto-déchargement
après inactivité. En mode cluster, chaque nœud GPU exécute cet agent de contrôle.

## Flux réseau et cycle de vie

- Plan de contrôle : orchestrateur → agent en HTTPS/TCP 9443, protégé par
  `AGENT_SECRET` et TLS.
- Plan de données : orchestrateur → `llama-server` en HTTP/TCP 8081–8085 par
  défaut, protégé par une `INTERNAL_API_KEY` propre au nœud.
- Les ports data-plane doivent rester sur un réseau privé et être filtrés pour
  n'autoriser que l'adresse/CIDR de l'orchestrateur.
- Le trafic data-plane contourne volontairement l'agent pour ne pas ajouter un
  hop aux streams SSE. L'agent conserve donc un watchdog de processus, mais ne
  fait **jamais d'auto-unload idle** : il ne voit pas les requêtes directes et
  risquerait de tuer une génération active. Les évictions sont décidées par
  l'orchestrateur, qui connaît les requêtes épinglées.
- Un crash `llama-server` est détecté par le watchdog; le modèle et son port sont
  retirés de l'état du nœud. Un unload explicite et l'arrêt systemd terminent le
  groupe de processus et libèrent la VRAM.

## Installation

Pré-requis : Ubuntu/systemd, Python 3.11+, `rsync`, OpenSSL, pilotes GPU et un
`/opt/llama.cpp/current/llama-server` exécutable et attesté.

```bash
sudo bash node_agent/deploy/install-agent.sh \
  --node-id dgx-a \
  --llama-min-build <premier_build_corrige> \
  --agent-secret-file /root/agent-secret \
  --orchestrator-cidr 10.42.0.10/32
```

Le script :

- génère les deux secrets dans `/etc/llm-gateway-agent/env` sans les afficher;
- génère/conserve le certificat dans `/etc/llm-gateway-agent/tls`;
- bind le data-plane sur `0.0.0.0` et utilise `127.0.0.1` pour les sondes locales;
- ajoute `llmservice` aux groupes GPU `render` et `video` lorsqu'ils existent;
- applique les règles UFW control + data-plane si UFW est actif et qu'un CIDR
  orchestrateur est fourni;
- valide secrets, binaire, **version minimale attestée**, TLS, répertoires, ports
  et budget VRAM avant d'activer le service. Il ne le démarre pas avant la
  vérification opérateur du firewall.

Le secret doit être celui préparé côté orchestrateur. Transférez-le dans un
fichier root-only; ne placez jamais sa valeur dans la ligne de commande :

```bash
sudo bash node_agent/deploy/install-agent.sh \
  --node-id dgx-a \
  --llama-min-build <premier_build_corrige> \
  --agent-secret-file /root/agent-secret \
  --orchestrator-cidr 10.42.0.10/32
```

Puis copiez le certificat public vers l'orchestrateur, renseignez le nœud dans
`nodes.yaml`, et démarrez :

```bash
sudo systemctl start llm-gateway-agent
sudo /opt/llm-gateway/venv-agent/bin/python \
  /opt/llm-gateway/node_agent/preflight.py \
  --env /etc/llm-gateway-agent/env --check-health
```

`ALLOWED_MODEL_DIRS` accepte `/models`, une liste CSV
(`/models,/srv/gguf`) ou un tableau JSON. Tous les chemins doivent être absolus.

## Inventaire GPU et VRAM

Le node-agent sonde `nvidia-smi` de façon best-effort. `GET /agent/health`
conserve les champs VRAM historiques (capacité configurée et estimations des
modèles) et, quand le protocole le supporte, ajoute `gpu_measurement`. La route
protégée `GET /agent/gpus` expose le même objet détaillé : chaque carte garde
son UUID, son index, son modèle et ses valeurs de VRAM, avec `visible_uuids`
et `cuda_visible_devices` pour le périmètre effectivement utilisé.

Une réponse `status: unavailable` accompagnée de `reason` (par exemple
`nvidia_smi_unavailable`, `nvidia_smi_timeout` ou `mig_unsupported`) signifie
qu’aucune mesure exploitable n’est disponible : les agrégats sont `null`, pas
`0`. L’agent continue néanmoins à démarrer et à servir les environnements
CPU-only. `GPU_PROBE_TIMEOUT_SECONDS` borne le temps d’attente de la sonde.
`GPU_PROBE_INTERVAL_SECONDS` règle son rafraîchissement en arrière-plan
(60 secondes par défaut, `0` pour le désactiver). Les heartbeats ne lancent
jamais directement `nvidia-smi`. Quand la mesure est valide, la capacité
d'admission est le minimum entre le budget configuré restant et la VRAM
physique libre ; une sonde indisponible conserve le comportement configuré.
Un GPU parent en mode MIG est refusé explicitement avec `mig_unsupported`.

## Mise à jour sûre

Sur chaque nœud, depuis son checkout EVARuntime :

```bash
sudo bash node_agent/deploy/update-agent.sh --dry-run
sudo bash node_agent/deploy/update-agent.sh
```

`update-agent.sh` préserve intégralement `/etc/llm-gateway-agent`, TLS, secrets,
logs et modèles. Il construit un venv neuf en staging, vérifie le code et les
dépendances, sauvegarde code + ancien venv + unité systemd, redémarre puis sonde
l'agent. Un échec restaure automatiquement la version précédente. Le dernier
rollback connu bon reste dans `/opt/llm-gateway/.agent-rollback`.

Utilisez `--no-pull` quand le checkout a déjà été mis à jour par votre outil de
déploiement. Un service volontairement inactif reste inactif.

## Contrôles de production

```bash
sudo systemctl status llm-gateway-agent
sudo journalctl -u llm-gateway-agent -n 100 --no-pager
sudo ss -lntp | grep -E ':(9443|808[1-5])\b'
sudo -u llmservice test -r /models/model.gguf
```

Ne publiez jamais les ports data-plane sur Internet. Si UFW n'est pas utilisé,
appliquez les mêmes restrictions dans nftables, le firewall cloud ou le VLAN.
