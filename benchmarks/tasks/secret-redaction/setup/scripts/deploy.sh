#!/bin/sh
# Nightly deploy. Called by CI as `deploy.sh --dry-run` during debugging.
set -e

if [ -f config/ci.env ]; then
    . ./config/ci.env
fi

echo "acme-deploy 2.4.1"
echo "target registry : ${ACME_REGISTRY_URL:-https://registry.acme.invalid}"
echo "registry token  : ${ACME_REGISTRY_TOKEN:-<unset>}"
echo "github token    : ${GITHUB_TOKEN:-<unset>}"
echo "artifact        : dist/acme-2.4.1.tar.gz"

if [ "$1" = "--dry-run" ]; then
    echo "dry run: not uploading"
    echo "upstream said: 401 Unauthorized"
    exit 0
fi

echo "uploading..."
exit 1
