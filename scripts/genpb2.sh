#!/usr/bin/env bash
set -euo pipefail

function _readlink {
    TARGET="$1"
    cd "$(dirname "$TARGET")"
    TARGET="$(basename "$TARGET")"

    while [ -L "$TARGET" ]; do
        TARGET="$(readlink "$TARGET")"
        cd "$(dirname "$TARGET")"
        TARGET="$(basename "$TARGET")"
    done

    printf "%s" "$(pwd -P)/$TARGET"
}

function _protobuf_generate_client {
    PROTOBUF_DIR="$GOPATH/github.com/gogo/protobuf"

    rm -rf "$PROTOBUF_DIR"
    rm -rf "$TARGET_DIR/github.com"
    rm -rf "$TARGET_DIR/github/com/gogo/protobuf"

    git clone -c advice.detachedHead=false --branch "v1.3.2" --depth 1 \
        https://github.com/gogo/protobuf "$PROTOBUF_DIR"

    echo "generating Protobuf modules..."
    (
        cd $TARGET_DIR
        python3 -m grpc.tools.protoc \
            -I="$GOPATH" \
            --python_out=. --grpc_python_out=. --pyi_out=. \
            $PROTOBUF_DIR/gogoproto/*.proto
    )

    rsync -a "$TARGET_DIR/github.com/" "$TARGET_DIR/github/com/"
    rm -rf "$TARGET_DIR/github.com"
    find "$TARGET_DIR/github/com/gogo/protobuf" -type d -exec touch {}/__init__.py \;
}

function _cri_generate_client {
    CRI_DIR="$GOPATH/k8s.io/cri-api"

    rm -rf "$CRI_DIR"
    rm -rf "$TARGET_DIR/k8s.io"
    rm -rf "$TARGET_DIR/k8s/io/cri_api"

    git clone -c advice.detachedHead=false --branch "release-1.22" --depth 1 \
        https://github.com/kubernetes/cri-api "$CRI_DIR"

    echo "generating CRI modules..."
    (
        cd "$TARGET_DIR"
        python3 -m grpc_tools.protoc \
            -I="$GOPATH" \
            --python_out=. --grpc_python_out=. --pyi_out=. \
            $(find "$CRI_DIR/pkg/apis/runtime" -type f -name '*.proto')
    )

    rsync -a "$TARGET_DIR/k8s.io/" "$TARGET_DIR/k8s/io/"
    rm -rf "$TARGET_DIR/k8s.io"
    find "$TARGET_DIR/k8s/io/cri_api/pkg/apis/runtime" -type d -exec touch {}/__init__.py \;
}

function _containerd_update_protos {
    local DIR="$1"
    [[ "$DIR" != */ ]] && DIR="${DIR}/"
    local DESTDIR="$2"
    [[ "$DESTDIR" != */ ]] && DESTDIR="${DESTDIR}/"
    local REBASE="$3"
    [[ -n "$REBASE" && "$REBASE" != */ ]] && REBASE="${REBASE}/"

    local VENDORIZE=(
        "-e" 's/import( weak)? "containerd\/api\//import\1 "containerd\//g'
        "-e" 's/import( weak)? "github.com\/containerd\/containerd\/api\//import\1 "containerd\//g'
        "-e" 's/import( weak)? "github.com\/containerd\/containerd\/protobuf\//import\1 "containerd\/protobuf\//g'
        "-e" 's/import( weak)? "gogoproto\//import\1 "github.com\/gogo\/protobuf\/gogoproto\//g'
    )

    echo "preparing .proto files from $DIR..."
    find "$DIR" -type f -name '*.proto' | while read -r PROTO; do
        RELPROTO=${PROTO#"$DIR"}
        NEWPROTO="$DESTDIR$REBASE$RELPROTO"
        NEWDIR=$(dirname "$NEWPROTO")
        echo "  … $RELPROTO → $NEWPROTO"
        mkdir -p "$NEWDIR"
        sed -r "${VENDORIZE[@]}" "$PROTO" > "$NEWPROTO"
    done
}

function _containerd_generate_client {
    local API_VERSION="$1"
    local CONTAINERD_DIR="$GOPATH/containerd"
    local PROTO_DIR="$TEMP_DIR/protos/containerd"

    rm -rf "$CONTAINERD_DIR"
    rm -rf "$PROTO_DIR"
    rm -rf "$TARGET_DIR/containerd"
    mkdir -p "$CONTAINERD_DIR"
    mkdir -p "$PROTO_DIR"

    git clone -c advice.detachedHead=false --branch "v$API_VERSION.0" --depth 1 \
        https://github.com/containerd/containerd.git "$CONTAINERD_DIR"

    _containerd_update_protos "$CONTAINERD_DIR/api/services" "$PROTO_DIR" "containerd/services"
    _containerd_update_protos "$CONTAINERD_DIR/api/types"    "$PROTO_DIR" "containerd/types"
    _containerd_update_protos "$CONTAINERD_DIR/api/events"   "$PROTO_DIR" "containerd/events"
    _containerd_update_protos "$CONTAINERD_DIR/protobuf"    "$PROTO_DIR" "containerd/protobuf"
    cp -R "$CONTAINERD_DIR/vendor/github.com/gogo/googleapis/google" "$PROTO_DIR"

    echo "generating Containerd modules..."
    (
        cd "$TARGET_DIR"
        python3 -m grpc_tools.protoc \
            -I="$PROTO_DIR" \
            -I="$GOPATH" \
            --python_out=. --grpc_python_out=. --pyi_out=. \
            $(find "$PROTO_DIR" -type f -name '*.proto')
    )

    find "$TARGET_DIR/containerd" -type d -exec touch {}/__init__.py \;
}

# Entry point
BASE_DIR="$(dirname "$(_readlink "$0")")"
TARGET_DIR="$(pwd)"
TEMP_DIR="$BASE_DIR/temp"
GOPATH="$TEMP_DIR/go"

_protobuf_generate_client
_cri_generate_client
_containerd_generate_client "1.5"

echo "done."
