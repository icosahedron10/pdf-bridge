#!/bin/sh
set -eu

if [ -z "${PDF_BRIDGE_STORAGE_ROOT:-}" ]; then
    echo "PDF_BRIDGE_STORAGE_ROOT is required" >&2
    exit 64
fi

if [ -z "${PDF_BRIDGE_SESSION_SECRET:-}" ] || [ -z "${PDF_BRIDGE_JOB_TOKEN:-}" ]; then
    echo "PDF_BRIDGE_SESSION_SECRET and PDF_BRIDGE_JOB_TOKEN are required" >&2
    exit 64
fi

case "$PDF_BRIDGE_SESSION_SECRET:$PDF_BRIDGE_JOB_TOKEN" in
    *CHANGE_ME*|development-only-change-me:*)
        echo "replace all placeholder/development secrets before starting PDF Bridge" >&2
        exit 64
        ;;
esac

if [ "$PDF_BRIDGE_SESSION_SECRET" = "$PDF_BRIDGE_JOB_TOKEN" ]; then
    echo "session and Jenkins secrets must be different" >&2
    exit 64
fi

case "$PDF_BRIDGE_STORAGE_ROOT" in
    /*) ;;
    *)
        echo "PDF_BRIDGE_STORAGE_ROOT must be an absolute path" >&2
        exit 64
        ;;
esac

case "$PDF_BRIDGE_STORAGE_ROOT" in
    /app|/app/*)
        echo "PDF_BRIDGE_STORAGE_ROOT must be outside the application directory" >&2
        exit 64
        ;;
esac

mkdir -p "$PDF_BRIDGE_STORAGE_ROOT"
if [ ! -w "$PDF_BRIDGE_STORAGE_ROOT" ]; then
    echo "PDF_BRIDGE_STORAGE_ROOT is not writable by uid $(id -u)" >&2
    exit 73
fi

alembic upgrade head

exec "$@"
