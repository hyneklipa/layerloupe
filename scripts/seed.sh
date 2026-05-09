#!/busybox/sh
# Seed a freshly-started ``registry:2`` with a handful of small public
# images so the LayerLoupe UI has something interesting to show on first run.
#
# Run inside the ``gcr.io/go-containerregistry/crane:debug`` image — the
# ``:debug`` tag bundles a busybox shell, while the binary lives at
# /ko-app/crane. We pull each image from Docker Hub and copy it into the
# in-stack registry on plain HTTP.
#
# Idempotent: re-running ``docker compose up`` re-pushes the same digests,
# which the registry deduplicates. Failed first-time pulls (e.g. you're
# offline) leave the stack functional, just with an empty registry.

set -eu

REGISTRY="${REGISTRY:-registry:5000}"

# Mix of single-arch + multi-arch + tiny — covers the manifest variants
# the UI was designed around.
IMAGES="
alpine:3.20
alpine:latest
hello-world:latest
busybox:1.36
"

echo "▶ Seeding $REGISTRY with public test images..."
echo "  (this fetches a few MB from docker.io on first run)"

for image in $IMAGES; do
    echo "  • $image  →  $REGISTRY/$image"
    # ``--insecure`` lets crane talk plain HTTP to the destination, which is
    # what ``registry:2`` serves over the compose network.
    if ! /ko-app/crane copy --insecure "$image" "$REGISTRY/$image"; then
        echo "  ⚠ Failed to mirror $image (network issue?). Continuing." >&2
    fi
done

echo "✓ Seed complete. Open http://localhost:8080 in your browser."
