# Deploiement

## Phase 0 - prerequis DSI

- VM 2 vCPU, 2 Go RAM, 20 Go disque.
- NIC etudiante : IP de service, exposee sur 443.
- NIC admin : sortie uniquement vers le vhost interne de la gateway admin.
- DNS : `llm-students.univ-pau.fr`.
- Certificat TLS public pour le vhost etudiant.
- Certificat client `gw-student.crt` signe par la PKI UPPA.
- Vhost interne gateway admin avec `ssl_verify_client on`.

## Phase 1 - installation applicative

```bash
sudo useradd --system --home /opt/llm-gateway-student --shell /usr/sbin/nologin llmstudent
sudo mkdir -p /opt/llm-gateway-student /etc/llm-gateway-student /var/lib/llm-gateway-student /var/log/llm-gateway-student
sudo chown -R llmstudent:llmstudent /opt/llm-gateway-student /var/lib/llm-gateway-student /var/log/llm-gateway-student
sudo cp deploy/env.example /etc/llm-gateway-student/env
sudo install -m 0644 deploy/llm-gateway-student.service /etc/systemd/system/
```

Installer ensuite le code dans `/opt/llm-gateway-student`, creer le venv, puis :

```bash
sudo -u llmstudent /opt/llm-gateway-student/venv/bin/python cli.py init-db
sudo systemctl daemon-reload
sudo systemctl enable --now llm-gateway-student
```

## Phase 2 - host hardening

```bash
sudo install -m 0644 deploy/sysctl.conf /etc/sysctl.d/99-llm-gw-student.conf
sudo sysctl --system
sudo install -m 0644 deploy/nftables.conf /etc/nftables.d/llm-gateway-student.conf
```

Adapter les noms d'interfaces et IP avant activation nftables.

## Phase 3 - nginx

```bash
sudo install -m 0644 deploy/nginx.conf /etc/nginx/sites-available/llm-gateway-student
sudo ln -sf /etc/nginx/sites-available/llm-gateway-student /etc/nginx/sites-enabled/llm-gateway-student
sudo nginx -t
sudo systemctl reload nginx
```

## Pre-flight prod

- `curl /health` depuis VLAN etudiant.
- Tentative `/admin/dashboard` -> 404.
- Requete avec modele non allowlist -> 400.
- Requete avec body > 64k -> 413 nginx.
- Requete sans mTLS vers vhost interne admin -> rejet.
- `systemd-analyze security llm-gateway-student` inspecte.
- `nft list ruleset` confirme deny-by-default.

