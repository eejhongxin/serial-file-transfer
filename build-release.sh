#!/usr/bin/env bash
set -euo pipefail
source_dir="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
release_name='serial-transfer-gui-2.3.0'
work_dir="$(mktemp -d)"
trap 'rm -rf -- "$work_dir"' EXIT
mkdir -p -- "$work_dir/$release_name" "$source_dir/dist"
for name in serial_terminal.py app.py serial_transfer.py README.md requirements.txt install.sh uninstall.sh icon.svg; do
    cp -- "$source_dir/$name" "$work_dir/$release_name/$name"
done
if [ -d "$source_dir/compat" ]; then
    cp -a -- "$source_dir/compat" "$work_dir/$release_name/"
fi
if [ -d "$source_dir/wheelhouse" ]; then
    cp -a -- "$source_dir/wheelhouse" "$work_dir/$release_name/"
fi
chmod +x "$work_dir/$release_name/install.sh" "$work_dir/$release_name/uninstall.sh"
tar -czf "$source_dir/dist/$release_name.tar.gz" -C "$work_dir" "$release_name"
(
    cd -- "$source_dir/dist"
    sha256sum "$release_name.tar.gz" > "$release_name.tar.gz.sha256"
)
echo "安装包: $source_dir/dist/$release_name.tar.gz"
