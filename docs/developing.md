# Developing & contributing

For people working on the addon source or running a custom MMCP server.

## Build from source

```bash
git clone https://github.com/animatica-ai/animatica-blender-plugin
cd animatica-blender-plugin
make zip          # → dist/animatica-blender-X.Y.Z.zip
make install      # symlink into Blender addons (reload addon after edits)
make uninstall
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
MMCP HTTP calls in `mmcp_client.py`, on top of the vendored `motionmcp` client
in `vendor/motionmcp/`.
Posing and editing by hand are Animatica Marionette, a separate add-on;
`posing.py` is the one place this add-on reaches it, and every call there is a
no-op without it (`tests/test_without_pro.py`).

## Bundled packages

The addon talks MMCP through the public `motionmcp` package
([`motionmcp-sdk`](https://pypi.org/project/motionmcp-sdk/) on PyPI), which
is not kept in git. The build fetches the version pinned by the single
`motionmcp-sdk==X.Y.Z` line in `requirements-bundle.txt` into
`animatica_blender/vendor/motionmcp/` (git-ignored), the same way the
MotionBuilder and 3ds Max plugins bundle their dependencies. `mmcp_client.py`
calls into it for the MMCP wire format; the addon owns the offline gate, the
cloud session and the error codes, plus the `POST /retarget` call, an extension
route the package does not offer.

After cloning, and after every change to the pin, fetch it once:

```bash
python scripts/vendor_motionmcp.py --write    # or: make deps
```

`make zip`, `make install` and CI run this step themselves. Without it the
addon refuses to load with a message naming the command.

To bump the pinned version: edit `requirements-bundle.txt`, run the fetch,
run the tests, and restart Blender — a running Blender keeps the modules it
already imported.

`python scripts/vendor_motionmcp.py` with no flag only checks the fetched
tree against the pin (exit 0: in sync, 1: drift, 2: could not tell).

Tests: `python -m pytest tests/test_mmcp_client.py -q` runs without Blender
(needs `numpy` and `pytest`). The other files in `tests/` are scripts that run
inside Blender without a server, one per behaviour they guard. Headless:

```bash
blender -b --factory-startup --python tests/test_bake_object_rotation.py
```

Through the addon's own operators, in a window that closes itself (the
server is stood in for by a stub; the report and screenshots land in
`tests/out/gui`, and `--rig`, `--arm`, `--take` point it at a rig of yours
and a saved response for it):

```bash
blender --factory-startup --python tests/test_gui_object_rotation.py
```

## Protocol & servers

The addon is an MMCP client. Generation runs on the server; no model runs
inside Blender.

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
