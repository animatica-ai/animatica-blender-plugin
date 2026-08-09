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

## Working with several models

A server may expose more than one model. Almost nothing in the addon is
model-aware by design — the **capabilities payload is the source of truth**,
cached process-wide by `mmcp_client` and read through
`mmcp_client.cached_model(settings.model_id)`. The model picker, the duration
hints, per-block seeds, and the visibility of **Generate Pose @ Frame** are
all derived from it, so a new server-side model needs no client release.

When adding UI that behaves differently per model, gate it on a capability
field (`fps`, `limits`, `supported_segments`, `supports_segment_seed`) rather
than on the model id. Two places legitimately need the id itself:

- `body_mesh.model_family_has_soma_body(model_id)` — the bundled SOMA77 body
  mesh only fits SOMA-family rest proportions. Joint-name overlap is *not* a
  sufficient test: ARDY's 27-joint Core skeleton matches 20/20 entries of
  `SOMA_BODY_JOINTS` while having different proportions, so the older
  `looks_like_kimodo_skeleton()` heuristic would happily skin the mesh onto
  the wrong body.
- The armature/model mismatch hint in `panels.py`, which compares the
  armature's `proscenium_canonical_model` marker (stamped at import) against
  the selected model. Generation still works across the mismatch — the server
  retargets — the hint is informational only.

Panel `draw()` callbacks must stay read-only; defer any state change to a
one-shot `bpy.app.timers` callback, as `properties.schedule_target_reset()`
does.

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
