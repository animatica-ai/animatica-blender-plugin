# Developing & contributing

For people working on the addon source or running a custom MMCP server.

## Build from source

```bash
git clone https://github.com/animatica-ai/proscenium-blender
cd proscenium-blender
make zip          # → dist/animatica-blender-X.Y.Z.zip
make install      # symlink into Blender addons (reload addon after edits)
make uninstall
```

Default symlink path (macOS):  
`~/Library/Application Support/Blender/5.0/scripts/addons`

Override: `make install BLENDER_ADDONS_DIR=/path/to/scripts/addons`

## Repository layout

Python package: `animatica_blender/` — operators in `operators.py`, UI in
`panels.py`, scene-to-request translation in `core_adapter.py`, rig inspection
in `rig_probe.py`, animation bake in `gltf_to_blender.py`, server calls in
`client_shim.py`.

## Shared code with the SDK

The half of the addon that has nothing to do with Blender — assembling the
MMCP request, talking to the server, decoding the glTF response — is not
written here. It comes from `animatica_core`, the package the 3ds Max and
MotionBuilder plugins also use, and it lives in this repo as a **vendored
copy** at `animatica_blender/animatica_core/`.

`animatica_blender/CORE-VERSION` names the SDK commit that copy was taken
from. `make info` prints it.

```bash
make sync-core              # does the copy still match the pin?
make sync-core WRITE=1      # re-vendor at the pin
python scripts/sync_core.py --write --ref <sha>   # move the pin
```

**Never edit anything under `animatica_blender/animatica_core/`.** A fix made
there is silently lost at the next sync, and it leaves the three plugins
disagreeing about what the same request means. Fix it in
[motionmcp-client-sdk](https://github.com/animatica-ai/motionmcp-client-sdk),
then move the pin. CI fails the build if the copy has drifted.

What stays Blender's own, deliberately: the UI, the bake into actions and
NLA strips, the control-rig handling, the Y-up/Z-up conversion in `coords.py`,
and `core_adapter.py` — the one module that knows both sides, turning settings,
prompt blocks, the path curve, effector empties and pose keys into the shared
builder's vocabulary.

## Tests

```bash
pytest tests
```

They run without Blender: the adapter's pure half is loaded straight off disk
and never touches `bpy`. `tests/test_core_adapter_parity.py` rebuilds the
frozen A/B checkpoints through the shared builder and compares the canonical
request hash, which is what keeps a core bump from quietly changing what the
addon says to the server. It needs the SDK's goldens — point
`ANIMATICA_AB_GOLDEN` at `<sdk checkout>/tools/ab_suite/golden/blender/local`,
or it skips and tells you so.

## Protocol & servers

The addon is an MMCP client (no ML in Blender). Generation runs on the server.

- [MMCP protocol](https://animatica.ai/mmcp)
- [MMCP implementations](https://animatica.ai/mmcp/docs/get-started/implementations)
- Reference self-hosted server: [motionmcp-kimodo](https://github.com/animatica-ai/motionmcp-kimodo)
- Animatica Cloud endpoint: `https://api.animatica.ai`

Releases: tag `vX.Y.Z` must match `bl_info["version"]` in `animatica_blender/__init__.py`.

## Contributing

Issues and pull requests:  
[github.com/animatica-ai/proscenium-blender](https://github.com/animatica-ai/proscenium-blender)

License: [GPL-3.0-or-later](../LICENSE)

Changelog: [CHANGELOG.md](../CHANGELOG.md)
