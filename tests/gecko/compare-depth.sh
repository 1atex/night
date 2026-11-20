set -euo pipefail

source_root=$(cd "$1" && pwd)
repo=$(cd "$(dirname "$0")/../.." && pwd)
workspace="$source_root/gfx/wr"
example="$workspace/webrender/examples/night_depth.rs"

mkdir -p "$(dirname "$example")"
cp "$repo/tests/gecko/swgl_depth.rs" "$example"
cd "$workspace"
export CARGO_PROFILE_DEV_DEBUG=0
if [[ ${RUNNER_OS:-} == Windows || ${OS:-} == Windows_NT ]]; then
    export CC=clang-cl CXX=clang-cl
fi
probe=(cargo run --locked -q -p webrender --features sw_compositor --example night_depth --)

upstream_control=$("${probe[@]}")
upstream_mixed=$("${probe[@]}" --scrollbars)
git -C "$source_root" apply --whitespace=error "$repo/scripts/gecko/patches/swgl-depth-tile.patch"
night_control=$("${probe[@]}")
night_mixed=$("${probe[@]}" --scrollbars)

printf 'SWGL peak bytes\n%-12s %12s %12s\n' engine page 'page+scroll'
printf '%-12s %12d %12d\n' upstream "$upstream_control" "$upstream_mixed" night "$night_control" "$night_mixed"
(( night_control == upstream_control ))
(( night_mixed == night_control ))
(( upstream_mixed - night_mixed >= 2000000 ))
