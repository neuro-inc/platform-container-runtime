#!/usr/bin/env bash

shopt -s globstar
set -e
set -u

function _readlink {
    TARGET="$1"

    cd "$(dirname "$TARGET")"
    TARGET="$(basename "$TARGET")"

    while [ -L "$TARGET" ]
    do
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
            --python_out=. --grpc_python_out=. \
            $PROTOBUF_DIR/gogoproto/*.proto
    )

    rsync -a $TARGET_DIR/github.com/ $TARGET_DIR/github/com/
    rm -rf "$TARGET_DIR/github.com"

    for package in $(find $TARGET_DIR/github/com/gogo/protobuf -type d) ; do
        touch ${package}/__init__.py
    done
}

function _cri_generate_client {
    CRI_DIR="$GOPATH/k8s.io/cri-api"

    rm -rf "$CRI_DIR"
    rm -rf "$TARGET_DIR/k8s.io"
    rm -rf "$TARGET_DIR/k8s/io/cri_api"

    git clone --depth 1 https://github.com/kubernetes/cri-api "$CRI_DIR"

    echo "generating CRI modules..."
    (
        cd $TARGET_DIR
        python3 -m grpc.tools.protoc \
            -I="$GOPATH" \
            --python_out=. --grpc_python_out=. \
            $CRI_DIR/pkg/apis/runtime/**/*.proto
    )

    rsync -a $TARGET_DIR/k8s.io/ $TARGET_DIR/k8s/io/
    rm -rf "$TARGET_DIR/k8s.io"

    for package in $(find $TARGET_DIR/k8s/io/cri_api/pkg/apis/runtime -type d) ; do
        touch ${package}/__init__.py
    done
}

function _containerd_update_rotos {
    # Make sure that the directory path to the source .proto files always has
    # a trailing "/". Same for the destination directory.
    DIR=$1
    [[ "$DIR" != */ ]] && DIR="$DIR/"
    DESTDIR=$2
    [[ "$DESTDIR" != */ ]] && DESTDIR="$DESTDIR/"
    REBASE=$3
    [[ -n "$REBASE" && "$REBASE" != */ ]] && REBASE="$REBASE/"

    VENDORIZE=(
        "-e" 's/import( weak)? "containerd\/api\//import\1 "containerd\//g'
        "-e" 's/import( weak)? "github.com\/containerd\/containerd\/api\//import\1 "containerd\//g'
        "-e" 's/import( weak)? "github.com\/containerd\/containerd\/protobuf\//import\1 "containerd\/protobuf\//g'
        "-e" 's/import( weak)? "gogoproto\//import\1 "github.com\/gogo\/protobuf\/gogoproto\//g'
    )

    echo "preparing .proto files from $DIR..."

    for PROTO in $DIR**/*.proto; do
        RELPROTO=${PROTO#"$DIR"}
        NEWPROTO="$DESTDIR$REBASE$RELPROTO"
        NEWDIR=${NEWPROTO%/*}
        echo "  ... $RELPROTO --> $NEWPROTO"
        mkdir -p $NEWDIR
        sed -r "${VENDORIZE[@]}" "$PROTO" > "$NEWPROTO"
    done
}

function _containerd_generate_client () {
    API_VERSION="$1"

    CONTAINERD_DIR="$GOPATH/containerd"
    PROTO_DIR="$TEMP_DIR/protos/containerd"

    rm -rf "$CONTAINERD_DIR"
    rm -rf "$PROTO_DIR"
    rm -rf "$TARGET_DIR/containerd"
    mkdir -p "$CONTAINERD_DIR"
    mkdir -p "$PROTO_DIR"

    # Fetches a specificly tagged release version of containerd and clones into
    # our temporary working area. Usually, we will only need vx.y.0 releases, as
    # containerd keeps its service API locked during the lifecycle of a specific
    # minor version, regardless of the patch series. See also:
    # https://github.com/containerd/containerd/blob/master/api/README.md
    git clone -c advice.detachedHead=false --branch "v$API_VERSION.0" --depth 1 \
        https://github.com/containerd/containerd.git "$CONTAINERD_DIR"

    _containerd_update_rotos "$CONTAINERD_DIR/api/services" "$PROTO_DIR" "containerd/services"
    _containerd_update_rotos "$CONTAINERD_DIR/api/types" "$PROTO_DIR" "containerd/types"
    _containerd_update_rotos "$CONTAINERD_DIR/api/events" "$PROTO_DIR" "containerd/events"
    _containerd_update_rotos "$CONTAINERD_DIR/protobuf" "$PROTO_DIR" "containerd/protobuf"
    # We need the google/rpc .proto definitions in order to compile everything,
    # but we don't want to get packages for them generated, as these are to be
    # supplied by the existing pip grpcio and protobuf packages instead.
    cp -R "$CONTAINERD_DIR/vendor/github.com/gogo/googleapis/google" "$PROTO_DIR"

    echo "generating Containerd modules..."
    (
        cd $TARGET_DIR
        python3 -m grpc.tools.protoc \
            -I="$PROTO_DIR" \
            -I="$GOPATH" \
            --python_out=. --grpc_python_out=. \
            $PROTO_DIR/containerd/**/*.proto
    )

    for package in $(find $TARGET_DIR/containerd -type d) ; do
        touch ${package}/__init__.py
    done
}

BASE_DIR="$(dirname $(_readlink $0))"
TARGET_DIR="$(pwd)"
TEMP_DIR="$BASE_DIR/temp"
GOPATH="$TEMP_DIR/go"

_protobuf_generate_client
_cri_generate_client
_containerd_generate_client "1.5"

echo "done."
