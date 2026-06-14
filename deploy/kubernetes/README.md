# Omnigent on Kubernetes

Deploy Omnigent to any Kubernetes cluster using Kustomize. The manifests pull
the prebuilt image and wire up a persistent volume and health checks, plus an
**optional** Ingress with TLS. The Ingress — and the ingress-nginx + cert-manager
add-ons behind it — is only needed to expose the server on a public domain. For
local or dev use, skip it entirely and reach the server with `kubectl
port-forward` (see [Verify the deployment](#verify-the-deployment)).

## What gets provisioned

- **Deployment** — single-replica pod running
  `ghcr.io/omnigent-ai/omnigent-server`, served on port 8000.
- **Service** — ClusterIP on port 80 → 8000.
- **Ingress** *(optional)* — HTTPS via cert-manager (nginx ingress class by
  default); only for exposing the server on a public domain.
- **PVC** — 10 Gi volume at `/data/artifacts` for the artifact store, minted
  cookie secret, and admin credentials.
- **ConfigMap + Secret** — environment config and database credentials.

## Prerequisites

- A Kubernetes cluster (1.25+)
- `kubectl` with Kustomize support (`kubectl kustomize` or standalone `kustomize`)
- A PostgreSQL database (managed or in-cluster — see below)
- *Optional (public Ingress/TLS path only):* an Ingress controller (e.g.
  ingress-nginx) and cert-manager

### Install the cluster add-ons (optional)

Only needed for the public Ingress/TLS path — **skip this entirely** if you'll
reach the server with `kubectl port-forward` or terminate TLS upstream. If you do
want the Ingress and your cluster lacks an Ingress controller and cert-manager,
install them (pin the versions to taste):

```bash
# ingress-nginx — use the provider manifest that matches your cluster
# (this is the kind one; for EKS/GKE/AKS use that provider's manifest or Helm chart):
kubectl apply -f https://raw.githubusercontent.com/kubernetes/ingress-nginx/main/deploy/static/provider/kind/deploy.yaml

# cert-manager:
kubectl apply -f https://github.com/cert-manager/cert-manager/releases/latest/download/cert-manager.yaml

# wait until both are ready:
kubectl wait -n ingress-nginx --for=condition=Ready pod \
  -l app.kubernetes.io/component=controller --timeout=180s
kubectl wait -n cert-manager --for=condition=Available deployment --all --timeout=180s
```

### Create a cert-manager issuer (optional)

Only needed if you're using the Ingress. The Ingress requests its TLS cert from a
`ClusterIssuer` named `letsencrypt-prod`
(the `cert-manager.io/cluster-issuer` annotation in `base/ingress.yaml`). That
issuer is **not** shipped here — create one before deploying, or change the
annotation to match an issuer you already have. Two common choices:

```yaml
# Production — real certificates from Let's Encrypt
# (needs a public domain and an Ingress reachable from the internet):
apiVersion: cert-manager.io/v1
kind: ClusterIssuer
metadata:
  name: letsencrypt-prod
spec:
  acme:
    server: https://acme-v02.api.letsencrypt.org/directory
    email: you@example.com
    privateKeySecretRef:
      name: letsencrypt-prod
    solvers:
      - http01:
          ingress:
            ingressClassName: nginx
```

```yaml
# Local / dev — self-signed (no public DNS needed; browsers will warn):
apiVersion: cert-manager.io/v1
kind: ClusterIssuer
metadata:
  name: letsencrypt-prod
spec:
  selfSigned: {}
```

Apply your chosen issuer with `kubectl apply -f <file>`. Without it, cert-manager
logs `IssuerNotFound` and no certificate is issued (the server still runs — only
TLS is affected).

## Deploy with an external database

Use this path when you have a managed Postgres (RDS, Cloud SQL, Neon, etc.).

1. **Edit the secret** — set your real `DATABASE_URL` and generate a cookie
   secret:

   ```bash
   # deploy/kubernetes/base/secret.yaml
   DATABASE_URL: "postgresql+psycopg://user:pass@your-db-host:5432/omnigent"
   OMNIGENT_ACCOUNTS_COOKIE_SECRET: "$(openssl rand -hex 32)"
   ```

2. **Edit the Ingress** *(only if exposing via Ingress)* — replace
   `omnigent.example.com` in `base/ingress.yaml` with your actual domain, and make
   sure the `letsencrypt-prod` ClusterIssuer exists (see
   [Create a cert-manager issuer](#create-a-cert-manager-issuer)).

3. **Apply:**

   ```bash
   kubectl kustomize deploy/kubernetes/base/ | kubectl apply -f -
   ```

4. **Create the first admin.** Open the app (via your Ingress host, or
   port-forward for a quick check — see
   [Verify the deployment](#verify-the-deployment)). With the default `accounts`
   provider the first visitor claims the instance: the Setup screen prompts for
   a username + password, and whoever finishes it first becomes the admin.

## Deploy with in-cluster Postgres

The `overlays/postgres/` overlay adds a single-replica Postgres 16 StatefulSet
with its own 10 Gi PVC. Good for dev/testing clusters.

1. **Edit secrets** — in `overlays/postgres/secret-patch.yaml`, replace
   `changeme` with real passwords:

   ```bash
   POSTGRES_PASSWORD: "<strong-password>"
   DATABASE_URL: "postgresql+psycopg://omnigent:<strong-password>@postgres:5432/omnigent"
   OMNIGENT_ACCOUNTS_COOKIE_SECRET: "$(openssl rand -hex 32)"
   ```

2. **Edit the Ingress hostname** *(only if exposing via Ingress)* in
   `base/ingress.yaml`, and make sure the `letsencrypt-prod` ClusterIssuer exists
   (see [Create a cert-manager issuer](#create-a-cert-manager-issuer)).

3. **Apply:**

   ```bash
   kubectl kustomize deploy/kubernetes/overlays/postgres/ | kubectl apply -f -
   ```

## Verify the deployment

Check the rollout and reach the server without a public domain:

```bash
kubectl get pods -n omnigent          # omnigent (and, with the overlay, postgres) → Running
kubectl rollout status deploy/omnigent -n omnigent
kubectl logs -n omnigent deploy/omnigent          # server logs

# Port-forward the Service and open the app locally:
kubectl port-forward -n omnigent svc/omnigent 8000:80
# → http://localhost:8000   (health check: curl localhost:8000/health → {"status":"ok"})
```

The first boot runs database migrations before the server starts listening; the
pod may restart once if the liveness probe fires during that window (see
[Resource sizing](#resource-sizing)).

To exercise the **Ingress** path locally instead of port-forwarding, set the
Ingress host to a domain that already resolves to localhost — e.g.
`omnigent.localtest.me` or `<node-ip>.sslip.io` — pair it with the `selfSigned`
issuer above, and reach it through your Ingress controller's published port.

## Next steps: connect a host

The server is the control plane — agents run on **hosts** that register with it.
A brand-new deployment has none, so connect at least one machine:

```bash
omnigent login https://omnigent.example.com          # authenticate the CLI
omnigent host  --server https://omnigent.example.com # register this machine
```

The host then appears in the web UI when you start a new chat. See the
[main README](../../README.md) for the full host/auth reference.

## Use your own IdP instead (OIDC) — optional

Optional. The default `accounts` provider (username + password) works out of the
box; use this only to delegate authentication to an external OIDC provider. Add
OIDC env vars to the secret:

```bash
kubectl create secret generic omnigent-oidc -n omnigent \
  --from-literal=OMNIGENT_AUTH_PROVIDER=oidc \
  --from-literal=OMNIGENT_OIDC_ISSUER=https://github.com \
  --from-literal=OMNIGENT_OIDC_CLIENT_ID=<client-id> \
  --from-literal=OMNIGENT_OIDC_CLIENT_SECRET=<client-secret> \
  --from-literal=OMNIGENT_OIDC_REDIRECT_URI=https://omnigent.example.com/auth/callback \
  --from-literal=OMNIGENT_OIDC_COOKIE_SECRET=$(openssl rand -hex 32)
```

Then add `envFrom: [{secretRef: {name: omnigent-oidc}}]` to the Deployment
container spec (or merge the values into `omnigent-secrets`).

## Resource sizing

The server idles around ~275 MB RSS. The manifests request 512 Mi and limit at
1 Gi — adjust to taste. The first boot against a remote Postgres runs
migrations and takes ~1 minute; bump the liveness `initialDelaySeconds` to
~90s if you see the pod get killed during the first deploy.

## Scaling

The server uses an in-memory runner registry, so **only one replica is
supported**. Do not increase `replicas` unless the architecture is changed to
use a shared registry (e.g. Redis).
