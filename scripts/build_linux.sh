#!/bin/sh
set -eu
ROOT=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
OUTPUT="$ROOT/.build/linux-output"
CONTAINER="mhm-linux-build-$$"
ARCH=${MHM_LINUX_ARCH:-$(docker info --format '{{.Architecture}}')}
case "$ARCH" in aarch64|arm64) ARCH=arm64 ;; x86_64|amd64) ARCH=amd64 ;; *) echo 'Supported Linux architectures: amd64 and arm64' >&2; exit 1 ;; esac
REQUIREMENTS_HASH=$(cat "$ROOT/native-host/requirements-runtime.txt" "$ROOT/native-host/requirements-build.txt" "$ROOT/scripts/build_linux.sh" | shasum -a 256 | cut -c1-16)
CACHE_IMAGE="mhm-linux-builder:$ARCH-$REQUIREMENTS_HASH"
mkdir -p "$OUTPUT"
cleanup() {
  docker cp "$CONTAINER:/out/." "$OUTPUT/" >/dev/null 2>&1 || true
  docker rm -f "$CONTAINER" >/dev/null 2>&1 || true
}
trap cleanup EXIT INT TERM
if docker image inspect "$CACHE_IMAGE" >/dev/null 2>&1; then IMAGE="$CACHE_IMAGE"; else IMAGE=python:3.12-slim-bookworm; fi
docker run -d --name "$CONTAINER" --platform "linux/$ARCH" -e MHM_CONTAINER_TEST=1 "$IMAGE" sleep infinity >/dev/null
docker exec "$CONTAINER" mkdir -p /work /out /requirements
if [ "$IMAGE" != "$CACHE_IMAGE" ]; then
  docker cp "$ROOT/native-host/requirements-runtime.txt" "$CONTAINER:/requirements/requirements-runtime.txt"
  docker cp "$ROOT/native-host/requirements-build.txt" "$CONTAINER:/requirements/requirements-build.txt"
  docker exec "$CONTAINER" sh -ec '
    apt-get update -qq
    apt-get install -y -qq --no-install-recommends binutils dpkg-dev nodejs gnupg libsecret-tools dbus gnome-keyring ca-certificates >/dev/null
    python -m pip install -q -r /requirements/requirements-build.txt
  '
  docker commit "$CONTAINER" "$CACHE_IMAGE" >/dev/null
fi
cd "$ROOT"
COPYFILE_DISABLE=1 tar --exclude=__pycache__ --exclude=.DS_Store --exclude='._*' -cf - native-host setup scripts tests extension protocol product.json Makefile README.md LICENSE | docker exec -i "$CONTAINER" tar xf - -C /work
docker exec -e MHM_GITHUB_TOKEN "$CONTAINER" sh -ec '
  cd /work
  mkdir -p .build/validation
  python -c "from setup import runtime as r; from pathlib import Path; import tempfile; a=r.resolve_mihomo_asset(); t=Path(tempfile.gettempdir())/a[\"name\"]; r._download_verified(a[\"url\"],t,a[\"sha256\"]); r._extract_mihomo(t,a[\"archiveType\"],Path(\".build/validation/mihomo\"))"
  python -B -m unittest discover -s tests -v
  python scripts/build_setup.py
  python scripts/build_deb.py --output /out --mihomo .build/validation/mihomo
  python scripts/build_apt_repository.py /out/*.deb --output /out/apt
  apt-get install -y -qq /out/*.deb >/dev/null
  mhm-setup --version
  /usr/lib/mhm/mihomo -v
  /usr/lib/mhm/native_host/native_host </dev/null
  python scripts/smoke_deb.py
  python scripts/smoke_apt_repository.py
  cp dist/mhm-setup-*-linux-* /out/
'
