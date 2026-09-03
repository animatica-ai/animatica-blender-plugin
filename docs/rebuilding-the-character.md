# Rebuilding the Animatic character asset

`animatica_blender/assets/animatic_character.blend` is derived from the Maya
FBX export (`Animatica_hero_v02.fbx`). It is not a straight import: Blender's
FBX importer leaves a Maya export in a state that is not usable as an
armature, so the asset is normalised on the way in. This is the procedure to
repeat when a new character version lands.

## What is wrong with a default import

Import the FBX with default settings and the armature is unusable:

| | Default import | After this procedure |
|---|---|---|
| Mean angle between a bone and its child | **89.3°** | 7.6° |
| Bones pointing >30° away from their child | **54 of 61** | 3 of 61 |
| Connected bones | 0 | 16 |
| Object transform | **scale 0.01, rot X +90°** | identity |
| Bone lengths | 1.5 – 43.4 (Maya cm) | 0.016 – 0.434 m |

Every bone points along +Y in armature space regardless of where its child
sits — Maya joints have no bone direction, and without being told to work it
out, the importer gives each bone an arbitrary fixed axis. Legs come in
pointing backwards (178.9° from their child). The rig is still riggable in the
sense that the *joint positions* are right, which is why generated motion
lands on it at all, but nobody can work with it and every rotation is
expressed against a meaningless local frame.

## Procedure

Bone names keep the `animatica:` namespace: the addon resolves bare MMCP joint
names through it (`gltf_to_blender.resolve_joint_bone`), and renaming would
also mean renaming all 77 vertex groups to keep the skinning attached.

1. **Import, aiming bones at their children.**

   ```python
   bpy.ops.import_scene.fbx(
       filepath=FBX,
       automatic_bone_orientation=True,  # THE fix — aim each bone at its child
       ignore_leaf_bones=False,          # keep *End bones: the 77 vgroups need them
       use_anim=False,
   )
   ```

   Then delete everything except the armature and its mesh. The FBX also
   carries the HIK control rig (~106 empties), a camera and assorted helper
   objects; none of it belongs in the asset, and nothing constrains the
   character to it.

2. **Reset the pose.** The FBX's stored joint transforms are not the bind
   pose — 25 bones import off-rest. Set every pose bone's location to zero,
   rotation to identity and scale to one. The rest pose is the T-pose.

3. **Bake the object transform into the data.** The importer's unit and axis
   conversion lands on the *object* (scale 0.01, rot X +90°), which leaves
   bone lengths and root motion in Maya centimetres.

   ```python
   M = arm.matrix_world.copy()
   arm.data.transform(M)
   mesh.data.transform(M)          # same matrix: the mesh's verts are in armature space
   arm.matrix_world = Matrix.Identity(4)
   mesh.matrix_world = Matrix.Identity(4)
   mesh.parent = arm
   mesh.matrix_parent_inverse = Matrix.Identity(4)
   ```

   Transform the **datablocks**, not the objects. `transform_apply()` on a
   parented, skinned mesh is fiddly to get right and silently misaligns the
   mesh from the armature. Verify by comparing world-space Z extents: the
   armature and the mesh must span the same range (~0 to 1.76 m).

4. **Name the datablocks** `Animatic` / `Animatic_body` /
   `Animatic_v03M` / `Animatic_diffuse_AO` / `Animatic_specular` /
   `Animatic_normal`, and pack every image. Do this in a file that does not
   already hold those names, or Blender appends `.001` and every importing
   file inherits the suffix.

5. **Write the partial .blend.**

   ```python
   bpy.data.libraries.write(DEST, {arm, mesh}, fake_user=True, compress=True)
   ```

   `fake_user=True` guarantees the datablocks are written; `load_character`
   clears the flag on the appended copies.

## Verifying

Do not trust the import — check it:

- Mean bone-to-child angle in single digits, and no bone off by ~90° or ~180°.
- Armature and mesh both at identity transform, world Z spans matching.
- Pose on rest (T-pose) with no action attached.
- 77 vertex groups matching 77 bone names.
- Then **generate onto it**. Bone rest orientations determine how the
  server's rotations resolve, so a rig that imports cleanly can still bake
  wrongly. A 40-frame "A person walks forward casually" should key 77
  namespaced bones with nothing in `animatica_skipped_joints`, put the hips
  near 0.95 m, and travel roughly 1.4 m/s.
