# The Animatic character asset

The character is **not** shipped in the addon. It is fetched on first use from
[`animatica-assets-public`][repo] and cached per user, and the addon
normalises it on the way in. This page says why the normalisation exists and
how to move to a new character version.

[repo]: https://github.com/animatica-ai/animatica-assets-public/tree/main/assets/animatica-hero

## What is fetched, and what is not

`assets/animatica-hero/source/animatica-hero.fbx` — one file, ~12 MB, with
the textures embedded, no control rig and no animation.

The sibling `blender/animatica-hero.blend` is deliberately **not** used, even
though it is smaller and would need no import step. It is a default FBX import
saved as-is, so it carries the defect below, and its textures are linked by
relative path into `source/` — using it would mean downloading four files in a
mirrored directory layout to get a worse armature.

## Why the addon normalises it

Maya joints carry no bone direction. Unless Blender's FBX importer is told to
work one out, it gives every bone the same arbitrary axis, and the importer's
unit and axis conversion is left sitting on the object:

| | Default import | What the addon does |
|---|---|---|
| Mean angle between a bone and its child | **89.9°** | 7.9° |
| Bones pointing >30° away from their child | **54 of 61** | 4 of 61 |
| Connected bones | 0 | 20 |
| Object transform | **scale 0.01, rot X +90°** | identity |
| Bone lengths | 1.5 – 43.4 (Maya cm) | 0.016 – 0.434 m |

Legs come in pointing backwards. The joint *positions* are right either way,
which is why generated motion lands on the raw rig at all — but every rotation
is then expressed against a meaningless local frame, and nobody can pose it by
hand.

`canonical_skeleton.load_character` fixes both halves, and the whole
download-plus-import costs about a second on a cold cache and a tenth of a
second on a warm one:

1. **Import with `automatic_bone_orientation=True`**, which aims each bone at
   its child. `ignore_leaf_bones` stays off — the `*End` bones carry vertex
   groups, and dropping them detaches part of the skin.
2. **Bake the object transform into the datablocks** (`_normalise_transform`).
   Both the armature and the mesh take the same matrix, because the mesh's
   vertices are in armature space. Transform the **data**, not the objects:
   `transform_apply()` on a parented, skinned mesh silently misaligns it.
3. **Reset the pose** (`clear_pose`). Pose-bone transforms live in a file
   independently of any action, so clearing `animation_data` does not unpose
   a rig.

Bone names keep their `animatica:` namespace so the asset still round-trips to
the Maya/MotionBuilder pipeline; the bake resolves bare MMCP joint names
through it (`gltf_to_blender.resolve_joint_bone`). Renaming the bones would
also mean renaming all 77 vertex groups to keep the skinning attached.

## Moving to a new character version

Everything that pins a version lives at the top of `animatica_blender/remote_asset.py`:

```python
ASSET_REF     = "df2d87cd76c9fb3d12b22d6c4541ddca4fdf1e1c"   # commit, not a branch
ASSET_VERSION = "v003"
ASSET_SHA256  = "631a8e3e…"    # == the Git LFS oid == the integrity check
ASSET_BYTES   = 12610444
```

The asset repository keeps these files in Git LFS, and an LFS pointer's `oid`
*is* the sha256 of the content — so the pin and the integrity check are the
same number, and no separate manifest is needed. To read it for a new version:

```bash
curl -sL https://raw.githubusercontent.com/animatica-ai/animatica-assets-public/<ref>/assets/animatica-hero/source/animatica-hero.fbx
```

which returns the pointer, not the file. Fetching the actual bytes needs the
media host (`media.githubusercontent.com/media/…`), which is what
`ASSET_URL` is built from.

`ASSET_SHA256` is part of the cache filename, so bumping the pin makes a new
cache entry rather than reusing the old download. Old entries are not cleaned
up automatically; they live in Blender's per-user datafiles
(`…/datafiles/animatica/assets/`) and are safe to delete by hand.

## Verifying a version bump

Do not trust the import — check it:

- Mean bone-to-child angle in single digits, and no bone off by ~90° or ~180°.
- Armature and mesh both at identity transform, world Z spans matching
  (~0 to 1.76 m).
- Pose on rest (T-pose), no action attached.
- 77 vertex groups matching 77 bone names, and all 30 SOMA30 joint names
  present once the `animatica:` namespace is stripped — otherwise generated
  motion will not land.
- Then **generate onto it**. Bone rest orientations decide how the server's
  rotations resolve, so a rig that imports cleanly can still bake wrongly. A
  40-frame "A person walks forward casually" should key 77 namespaced bones
  with nothing in `animatica_skipped_joints`, put the hips near 0.95 m, and
  travel roughly 1.4 m/s.
