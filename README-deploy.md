# Deploying to IBM Code Engine

## Prerequisites
- IBM Cloud CLI with Code Engine plugin: `ibmcloud plugin install code-engine`
- Docker (or Podman) for local build/push
- Access to an IBM Container Registry (ICR) namespace
- A Code Engine project already created

---

## 1. Add the SSH key to your Git host (for private repo access)

The generated keypair (`id_rsa` / `id_rsa.pub`) lets Code Engine pull source
from a private Git repository.

```bash
# Print the public key and paste it into GitHub / GitLab → Settings → SSH Keys
cat id_rsa.pub
```

Register the private key with Code Engine:

```bash
ibmcloud ce secret create \
  --name genpact-pdf-git-ssh \
  --format ssh \
  --key-path ./id_rsa
```

> **Keep `id_rsa` secret — never commit it.** It is listed in `.gitignore`.

---

## 2. Store WatsonX credentials as a Code Engine secret

```bash
ibmcloud ce secret create \
  --name watsonx-credentials \
  --from-literal WATSONX_API_KEY="<your-api-key>" \
  --from-literal WATSONX_PROJECT_ID="<your-project-id>" \
  --from-literal WATSONX_MODEL_ID="ibm/granite-vision-3-2-2b" \
  --from-literal WATSONX_URL="https://us-south.ml.cloud.ibm.com"
```

---

## 3. Build and push the container image

```bash
# Log in to IBM Container Registry
ibmcloud cr login

# Tag for your ICR namespace
export ICR_IMAGE="icr.io/<your-namespace>/genpact-pdf-to-excel:latest"

docker build -t "$ICR_IMAGE" .
docker push "$ICR_IMAGE"
```

---

## 4. Create (or update) the Code Engine application

### First deploy

```bash
ibmcloud ce application create \
  --name genpact-pdf-to-excel \
  --image "$ICR_IMAGE" \
  --port 8080 \
  --min-scale 0 \
  --max-scale 3 \
  --cpu 1 \
  --memory 4G \
  --env-from-secret watsonx-credentials
```

### Re-deploy after a code change

```bash
docker build -t "$ICR_IMAGE" . && docker push "$ICR_IMAGE"

ibmcloud ce application update \
  --name genpact-pdf-to-excel \
  --image "$ICR_IMAGE"
```

---

## 5. Get the public URL

```bash
ibmcloud ce application get --name genpact-pdf-to-excel --output url
```

Open `<URL>/ui` in your browser to access the frontend.

---

## File reference

| File | Purpose |
|------|---------|
| `Dockerfile` | Multi-stage build — UBI9 minimal, non-root uid 1001 |
| `.dockerignore` | Excludes `.venv`, `.env`, job artefacts from the image |
| `id_rsa` | **Private** SSH key — register with Code Engine, never commit |
| `id_rsa.pub` | Public SSH key — add to GitHub/GitLab deploy keys |
