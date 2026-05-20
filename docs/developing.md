# Developing & contributing

For people working on the addon source or running a custom MMCP server.

## Build from source

```bash
git clone https://github.com/animatica-ai/proscenium-blender
cd proscenium-blender
make zip          # → dist/proscenium-blender-X.Y.Z.zip
make install      # symlink into Blender addons (reload addon after edits)
make uninstall
```

Default symlink path (macOS):  
`~/Library/Application Support/Blender/5.0/scripts/addons`

Override: `make install BLENDER_ADDONS_DIR=/path/to/scripts/addons`

## Repository layout

Python package: `proscenium_blender/` — operators in `operators.py`, UI in
`panels.py`, request assembly in `request_builder.py`, animation bake in
`gltf_to_blender.py`.

## Protocol & servers

The addon is an MMCP client (no ML in Blender). Generation runs on the server.

- [MMCP protocol](https://animatica.ai/mmcp)
- [MMCP implementations](https://animatica.ai/mmcp/docs/get-started/implementations)
- Reference self-hosted server: [motionmcp-kimodo](https://github.com/animatica-ai/motionmcp-kimodo)
- Animatica Cloud endpoint: `https://api.animatica.ai`

Releases: tag `vX.Y.Z` must match `bl_info["version"]` in `proscenium_blender/__init__.py`.

## Contributing

Issues and pull requests:  
[github.com/animatica-ai/proscenium-blender](https://github.com/animatica-ai/proscenium-blender)

License: [GPL-3.0-or-later](../LICENSE)

Changelog: [CHANGELOG.md](../CHANGELOG.md)
