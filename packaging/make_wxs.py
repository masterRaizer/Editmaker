"""Generate a WiX v4/v5 source that installs every file of the PyInstaller folder.

python packaging/make_wxs.py dist/EditMaker build/EditMaker.wxs 1.0.0
wix build -arch x64 build/EditMaker.wxs -o dist/EditMaker-1.0.0-x64.msi
"""
from __future__ import annotations

import hashlib
import sys
from pathlib import Path
from xml.sax.saxutils import quoteattr

UPGRADE_CODE = "6C1B3F0E-7A55-4E4B-9C61-2D8E5B7A9F13"  # never change: lets new MSIs replace old ones


def _id(prefix: str, rel: str) -> str:
    return prefix + hashlib.sha1(rel.encode("utf-8")).hexdigest()[:24]


def build(src: Path, version: str) -> str:
    src = src.resolve()
    dirs: dict[str, str] = {"": "INSTALLFOLDER"}
    tree: dict[str, list[str]] = {"": []}
    for d in sorted(p for p in src.rglob("*") if p.is_dir()):
        rel = d.relative_to(src).as_posix()
        dirs[rel] = _id("d", rel)
        parent = d.parent.relative_to(src).as_posix() if d.parent != src else ""
        tree.setdefault(parent, []).append(rel)
        tree.setdefault(rel, [])

    def dir_xml(rel: str, indent: str) -> str:
        out = []
        for child in tree.get(rel, []):
            name = Path(child).name
            inner = dir_xml(child, indent + "  ")
            out.append(f'{indent}<Directory Id="{dirs[child]}" Name={quoteattr(name)}>{inner}\n{indent}</Directory>'
                       if inner else f'{indent}<Directory Id="{dirs[child]}" Name={quoteattr(name)} />')
        return ("\n" + "\n".join(out)) if out else ""

    comps = []
    for f in sorted(p for p in src.rglob("*") if p.is_file()):
        rel = f.relative_to(src).as_posix()
        parent = f.parent.relative_to(src).as_posix() if f.parent != src else ""
        fid = "EditMakerExe" if rel == "EditMaker.exe" else _id("f", rel)
        extra = ""
        if rel == "EditMaker.exe":
            extra = ('\n        <Shortcut Id="StartMenuShortcut" Directory="ProgramMenuFolder" Name="EditMaker" '
                     'WorkingDirectory="INSTALLFOLDER" Advertise="no" />'
                     '\n        <Shortcut Id="DesktopShortcut" Directory="DesktopFolder" Name="EditMaker" '
                     'WorkingDirectory="INSTALLFOLDER" Advertise="no" />\n      ')
        comps.append(f'    <Component Id="{_id("c", rel)}" Directory="{dirs[parent]}">\n'
                     f'      <File Id="{fid}" Source={quoteattr(str(f))} KeyPath="yes">{extra}</File>\n'
                     f'    </Component>')
    return f'''<?xml version="1.0" encoding="UTF-8"?>
<Wix xmlns="http://wixtoolset.org/schemas/v4/wxs">
  <Package Name="EditMaker" Manufacturer="EditMaker" Version="{version}" UpgradeCode="{UPGRADE_CODE}"
           Scope="perMachine" Compressed="yes" Language="1033">
    <MajorUpgrade DowngradeErrorMessage="A newer version of EditMaker is already installed." />
    <MediaTemplate EmbedCab="yes" CompressionLevel="high" />
    <Property Id="ARPNOMODIFY" Value="1" />
    <StandardDirectory Id="ProgramFiles64Folder">
      <Directory Id="INSTALLFOLDER" Name="EditMaker">{dir_xml("", "        ")}
      </Directory>
    </StandardDirectory>
    <StandardDirectory Id="ProgramMenuFolder" />
    <StandardDirectory Id="DesktopFolder" />
    <Feature Id="Main" Title="EditMaker" Level="1">
      <ComponentGroupRef Id="AppFiles" />
    </Feature>
  </Package>
  <Fragment>
    <ComponentGroup Id="AppFiles">
{chr(10).join(comps)}
    </ComponentGroup>
  </Fragment>
</Wix>
'''


if __name__ == "__main__":
    src, out, version = Path(sys.argv[1]), Path(sys.argv[2]), sys.argv[3]
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(build(src, version), encoding="utf-8")
    print(f"{out}: {sum(1 for p in src.rglob('*') if p.is_file())} files")
