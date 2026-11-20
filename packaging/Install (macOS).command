#!/bin/bash
# Fusion Bulk Export installer for macOS: copies the FusionBulkExport folder that sits
# next to this file into Fusion's add-ins folder. Nothing else is changed.
set -u

finish() {
    echo
    read -r -p "Press Return to close this window. " _ || true
    exit "$1"
}

here="$(cd "$(dirname "$0")" && pwd)" || finish 1
source_dir="$here/FusionBulkExport"
addins="${HOME:?}/Library/Application Support/Autodesk/Autodesk Fusion 360/API/AddIns"
staging="$addins/.FusionBulkExport-installing"

if [ ! -f "$source_dir/FusionBulkExport.manifest" ]; then
    echo "Cannot find the FusionBulkExport folder next to this installer."
    echo "Unzip the whole download first, then run this installer again from the unzipped folder."
    finish 1
fi

mkdir -p "$addins" || finish 1
rm -rf "$staging"
cp -R "$source_dir" "$staging" || { rm -rf "$staging"; echo "Copying failed."; finish 1; }
rm -rf "$addins/FusionBulkExport"
mv "$staging" "$addins/FusionBulkExport" || { echo "Copying failed."; finish 1; }

echo "Installed. In Fusion press Shift+S, open the Add-Ins tab, switch FusionBulkExport on and tick Run on Startup."
finish 0
