# Developing & contributing

For people working on the addon source or running a custom MMCP server.

## Build from source

```bash
git clone https://github.com/animatica-ai/animatica-blender-plugin
cd animatica-blender-plugin
make zip          # → dist/animatica-blender-X.Y.Z.zip
make install      # symlink into Blender addons (reload addon after edits)
make uninstall
make zip-with-model MODEL_DIR=<bundle>   # a test build that carries the Autoposer model
```

A linked install is a development checkout: the built-in updater refuses to
touch it, so update it with git.

Default symlink path (macOS):  
`~/Library/Application Support/Blender/5.0/scripts/addons`

Override: `make install BLENDER_ADDONS_DIR=/path/to/scripts/addons`

## Repository layout

Python package: `animatica_blender/` — operators in `operators.py`, UI in
`panels.py`, request assembly in `request_builder.py`, animation bake in
`gltf_to_blender.py`, updates in `updater.py`, example scenes in `examples.py`,
the Autoposer in `autoposer/`, MMCP HTTP calls in `mmcp_client.py`, on top of
the vendored `motionmcp` client in `vendor/motionmcp/`.

## Bundled packages

`animatica_blender/vendor/motionmcp/` is a vendored copy of the public
`motionmcp` package ([`motionmcp-sdk`](https://pypi.org/project/motionmcp-sdk/)
on PyPI), pinned by the single `motionmcp-sdk==X.Y.Z` line in
`requirements-bundle.txt`. `mmcp_client.py` calls into it for the MMCP wire
format; the addon owns only the offline gate, the cloud session and error
codes.

To bump the pinned version:

```bash
# edit the version in requirements-bundle.txt, then:
python scripts/vendor_motionmcp.py --write
python -m pytest tests -q
```

Restart Blender afterwards — hot-swapping the addon does not reload modules
already imported by a running Blender, so a stale `vendor/motionmcp/` can stay
loaded until restart.

Run `python scripts/vendor_motionmcp.py` on its own to check the vendored
tree against the pin without changing anything (exit 0: in sync, 1: drift, 2:
could not tell).

Run the test suite with `python -m pytest tests -q` (needs `numpy` and
`pytest`; no Blender required).

## Protocol & servers

The addon is an MMCP client. Generation runs on the server; the only model
that runs inside Blender is the optional Autoposer (onnxruntime, downloaded on
request).

- [MMCP protocol](https://animatica.ai/mmcp)
- [MMCP implementations](https://animatica.ai/mmcp/docs/get-started/implementations)
- Reference self-hosted server: [motionmcp-kimodo](https://github.com/animatica-ai/motionmcp-kimodo)
- Animatica Cloud endpoint: `https://api.animatica.ai`

Releases: tag `vX.Y.Z` must match `bl_info["version"]` in `animatica_blender/__init__.py`.
The updater reads releases from `animatica-ai/animatica-blender-plugin` (`REPO` in
`updater.py`); a pre-release (flagged on GitHub, or a tag with a suffix such as `-preview7`) is offered only with **Include previews** on.

## Contributing

Issues and pull requests:  
[github.com/animatica-ai/animatica-blender-plugin](https://github.com/animatica-ai/animatica-blender-plugin)

License: [GPL-3.0-or-later](../LICENSE)

Changelog: [CHANGELOG.md](../CHANGELOG.md)
