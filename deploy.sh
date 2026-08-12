#!/usr/bin/env bash
# Build, push, and roll out a new image to ibrcm-appbe-prd002.
#
# Usage:
#   ./deploy.sh            # auto-picks the next vN tag
#   ./deploy.sh v7         # deploy under an explicit tag (e.g. for rollback: az cli only, see below)
#
# Rollback (no rebuild needed, image already sits in ACR):
#   az webapp config container set --name ibrcm-appbe-prd002 --resource-group intellibillrcm \
#     --container-image-name ibrcmacr001.azurecr.io/chargesheet:v1 \
#     --container-registry-url https://ibrcmacr001.azurecr.io \
#     --container-registry-user ibrcmacr001 \
#     --container-registry-password "$(az acr credential show --name ibrcmacr001 --query 'passwords[0].value' -o tsv)"

set -euo pipefail

RESOURCE_GROUP="intellibillrcm"
APP_NAME="ibrcm-appbe-prd002"
ACR_NAME="ibrcmacr001"
ACR_LOGIN_SERVER="${ACR_NAME}.azurecr.io"
IMAGE_REPO="chargesheet"
HEALTH_URL="https://ibrcm-appbe-prd002-gzetbwf2d4h8asah.centralus-01.azurewebsites.net/health"

cd "$(dirname "${BASH_SOURCE[0]}")"

if [ -n "${1:-}" ]; then
  TAG="$1"
else
  echo "==> Determining next version tag..."
  LAST_N=$(az acr repository show-tags --name "$ACR_NAME" --repository "$IMAGE_REPO" -o tsv 2>/dev/null \
    | grep -E '^v[0-9]+$' | sed 's/^v//' | sort -n | tail -1)
  NEXT_N=$(( ${LAST_N:-0} + 1 ))
  TAG="v${NEXT_N}"
fi

IMAGE="${ACR_LOGIN_SERVER}/${IMAGE_REPO}:${TAG}"
echo "==> Deploying as tag: $TAG ($IMAGE)"

echo "==> Building image for linux/amd64 (Azure's arch, not this Mac's)..."
docker buildx build --platform linux/amd64 -t "${IMAGE_REPO}:local" --load .

echo "==> Tagging..."
docker tag "${IMAGE_REPO}:local" "$IMAGE"
docker tag "${IMAGE_REPO}:local" "${ACR_LOGIN_SERVER}/${IMAGE_REPO}:latest"

echo "==> Logging in to ACR..."
az acr login --name "$ACR_NAME"

echo "==> Pushing..."
docker push "$IMAGE"
docker push "${ACR_LOGIN_SERVER}/${IMAGE_REPO}:latest"

echo "==> Pointing $APP_NAME at $IMAGE..."
ACR_PASSWORD=$(az acr credential show --name "$ACR_NAME" --query 'passwords[0].value' -o tsv)
az webapp config container set \
  --name "$APP_NAME" \
  --resource-group "$RESOURCE_GROUP" \
  --container-image-name "$IMAGE" \
  --container-registry-url "https://${ACR_LOGIN_SERVER}" \
  --container-registry-user "$ACR_NAME" \
  --container-registry-password "$ACR_PASSWORD" \
  -o none

echo "==> Waiting for the new container to come up..."
for i in $(seq 1 10); do
  CODE=$(curl -s -o /dev/null -w "%{http_code}" --max-time 15 "$HEALTH_URL" || true)
  echo "    attempt $i: HTTP $CODE"
  if [ "$CODE" = "200" ]; then
    echo "==> Deployed successfully as $TAG."
    curl -s "$HEALTH_URL"
    echo
    exit 0
  fi
  sleep 15
done

echo "==> Still not healthy after waiting. Check logs:"
echo "    az webapp log tail --name $APP_NAME --resource-group $RESOURCE_GROUP"
exit 1
