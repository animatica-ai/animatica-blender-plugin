# Changelog

All notable changes to the Animatica for Blender addon are documented here.
The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

Entries for 0.4.0 and earlier describe the addon under its former name,
Proscenium, and keep the identifiers those releases actually shipped.

## [Unreleased]

### Added

- **The Animatic character is what "Import rig" loads.** The rigged, textured
  hero body ships at `assets/animatic_character.blend` and is appended whole —
  armature, skinned mesh and material — instead of a bare skeleton being built
  from joint data. The SOMA30 rig and the model's canonical skeleton remain
  selectable on the import operator.

### Fixed

- **Namespaced rigs are driven instead of silently skipped.** MMCP joint names
  are bare (`Hips`); rigs exported from Maya / MotionBuilder carry the source
  scene's namespace on every bone (`animatica:Hips`), so the bake's exact-name
  lookup matched nothing and every channel was dropped without an error. The
  bake now resolves through a whole-rig namespace when one is present
  (`gltf_to_blender.resolve_joint_bone`), and the pose-bake selection filter
  strips it on the way back out. Unprefixed rigs are unaffected.

### Changed

- **The addon shares its non-Blender half with the other Animatica plugins.**
  Assembling the MMCP request, talking to the server and decoding the glTF
  response are no longer written here: they come from `animatica_core`, the
  package the 3ds Max and MotionBuilder plugins also use, vendored into
  `animatica_blender/animatica_core/` at the commit named in `CORE-VERSION`.
  What stays Blender's own is the UI, the bake into actions and NLA strips,
  the control-rig handling and the axis conversion. Measured against the
  cross-host A/B checkpoints, the requests the addon sends are byte-identical
  to the ones it sent before, and a baked clip differs by at most 2.4e-06
  degrees of rotation.

  Four things change for users:

  - **A clip longer than the model allows is refused before it is sent**,
    instead of being rejected by the server after the round trip. The panel
    warns as it always did.
  - **The builder's warnings now appear in Blender.** An effector pin with no
    body context, or a scene running at a different frame rate than the model,
    used to be noted only in the panel or not at all.
  - **A path curve drawn without per-point timing sends its control points**
    spread evenly across the range, rather than a densely resampled polyline.
    Curves that carry their own per-point frames are unaffected.
  - **Pose generation respects the CFG panel.** The pose request never carried
    guidance settings before, so those sliders did nothing for it.

- **Renamed from Proscenium to Animatica.** The rename reaches every
  identifier, not just the labels: the Python package is now
  `animatica_blender`, operators are `bpy.ops.animatica.*`, scene settings
  live on `bpy.context.scene.animatica`, panel classes are `ANIMATICA_PT_*`,
  and the sidebar tab reads **Animatica**.

  **Breaking for external scripts.** Anything calling `bpy.ops.proscenium.*`
  or reading `scene.proscenium` must be updated; the old names are gone
  rather than aliased.

  **Existing .blend files keep working.** Blender treats the renamed package
  as a new addon, so it needs enabling once and its preferences (server,
  sign-in) re-entered. Scene data is carried forward by a new `migrate.py`,
  which runs on file load and rewrites the `proscenium_*` custom properties
  and the old scene-settings block to their new keys. Motion-bake actions
  and NLA tracks are deliberately *not* renamed — the prefix filters accept
  the old `Proscenium_` spellings alongside the new ones, so the datablocks
  users can see in the outliner are left alone and action references by name
  stay valid.

## [0.4.0] — 2026-05-31

### Added

- **Rigify control-rig support.** Generated deform-bone motion now bakes
  onto Rigify control armatures — FK / IK chains and the hip / chest /
  neck / head master controls — not just Mixamo-style rigs. A new
  `rigify_bake.py` uses a helper-bone retargeting strategy: each control
  gets a helper at its own rest reference, parented to the source DEF
  bone, and Blender's depsgraph evaluates the constraint stack during the
  bake, so Rigify's rest-orientation offsets and MCH / ORG / tweak chain
  composition resolve correctly by construction. Rig-agnostic baking
  helpers (matrix overrides, action-slot management, fcurve handling) are
  now shared between the Mixamo and Rigify paths via the new
  `_bake_common.py`.
- **Per-block regeneration (re-roll one block).** New
  `proscenium.regenerate_block` operator regenerates a single timeline
  prompt block in place while preserving the surrounding motion —
  keyframes outside the block's frame range are kept and the fresh GLTF is
  spliced into the range (`splice_gltf_into_action`,
  `sample_pose_at_frame`), so neighboring blocks stay continuous.
- **Seed controls.** Randomize the generation seed from the main panel
  (`proscenium.randomize_seed`) or the Generate Pose dialog
  (`proscenium.randomize_pose_seed`), and lock the current seed
  (`proscenium.lock_global_seed`). Per block, pin the last-used seed into a
  block (`proscenium.reuse_block_seed`) or clear it
  (`proscenium.clear_block_seed`); seed-pinned blocks now show a visual
  indicator in the timeline overlay.
- **Documentation pages.** New `docs/` set — installation, usage,
  configuration, developing, and limitations — covering model connection
  in the Proscenium panel, timeline prompt-block management (add / edit /
  resize), and local developer setup.

### Changed

- **T-pose → A-pose handled in the request builder.** Bone rotations are
  corrected via `t_pose_q_matrix` in `request_builder.py` during GLTF
  baking, and the redundant T-pose correction in `gltf_to_blender.py` is
  removed — one place owns the transform and the bake is streamlined.
- **Consistent deform-bone selection.** Pose-keyframe sampling now draws
  from a shared `emitted_deform_bones` set (excluding face and Rigify
  tweak bones) so the joints referenced in pose keyframes match
  server-side validation; `sample_pose_keyframes` iterates the
  pre-filtered list.
- **Updated UI terminology** in canonical-skeleton operator error
  messages to match the current panel wording.

### Fixed

- **Regenerate no longer corrupts the source action.** The Generate-again
  / regenerate path rebuilds its request from scratch merges, and
  `merge_preview_keyframes_into_source` now skips generated keyframes
  (identified via `_is_motion_bake_action`), so motion-bake samples are
  never merged back onto your authored source action. Temporary scratch
  actions are tracked and cleared to keep memory tidy.

## [0.3.2] — 2026-05-14

### Added

- **Generate Pose: apply to all bones or selected bones.** New *Apply pose
  to* option on the dialog (*All bones* / *Selected bones*). Selected
  scope uses pose-bone selection; on Mixamo-style control rigs, IK /
  control handles expand to the driving deform joints. Scripting:
  `pose_apply_scope='SELECTED'` on `proscenium.generate_pose`.
- **`blender_compat` helpers** for pose-bone selection across Blender
  versions (`PoseBone.select` on Blender 5 vs. legacy `Bone.select`).
- **Need help?** Button at the top of the Proscenium sidebar opens the
  [Animatica Discord](https://discord.gg/A8CrURBewz) in your browser
  (`proscenium.open_discord_help`).

### Changed

- **Canonical import with SOMA77 body.** The reference body mesh is
  shaded smooth, the armature uses **In Front** in the viewport so bones
  read through the surface, and after a successful body import the view
  switches to **Pose** mode on the new rig (best-effort; ignored without a
  3D View context).

### Fixed

- **Target armature after delete.** Deleting the rig (including multi-object
  delete) clears the picker, preview / source-action bookkeeping, timeline
  prompt strips, and dangling pointers. Centralized
  `reset_target_armature_state`; depsgraph validation treats unlinked
  armatures as gone (`users_collection`); panel and timer fallbacks when
  RNA updates do not fire.
- **Generate / Regenerate after a deleted rig.** Stale preview flags no
  longer send merges onto the wrong action when you pick a new character.
- **Timeline strip delete.** Deletes the strip under the cursor when
  possible, persists removals onto the target armature’s stored blocks, and
  clears strips when there is no live target armature.
- **Regenerate (Generate again) with a stashed source action.** Before
  building the motion request, generated sample keys are stripped from the
  preview action and surviving keys are merged onto the source action, then
  the source is made active again — keys added or edited during preview are
  no longer dropped when you click **Generate** a second time.
- **Reject after motion preview.** Same strip-and-merge path: removes
  generated motion samples from the preview while preserving authored
  keyframes, merges them onto the saved source action when present, and
  restores that action (avoids T-pose gaps on channels that only had
  generated keys).
- **Generate Pose keyframe tags.** Keys written at the pose frame are tagged
  as authored so the Dopesheet does not treat them like inherited
  **GENERATED** tags from a motion-bake preview.

## [0.3.1] — 2026-05-08

### Added

- **In-place motion (preview).** Scene setting pins the root bone’s
  horizontal translation with a `Limit Location` constraint during
  preview so the character plays vertically in place. F-curves are left
  intact; **Push to NLA** zeros root X/Z keys on the committed actions and
  removes the constraint so the NLA data is genuinely travel-free.
- **Agent skill (`skills/proscenium/SKILL.md`).** Cursor-oriented guide for
  driving Proscenium via Blender MCP: async operators, polling, prompt
  blocks, constraints, and known caveats.

### Changed

- **Root-path MMCP sampling.** Root-path curves share a world-space
  polyline helper; heading is derived from the tangent in MMCP XZ with
  `atan2(tx, tz)` so facing matches walk direction along the path.
  Single-frame generation windows are supported instead of returning no
  constraint. The auto **start anchor** can include `heading_radians`
  when the curve has **Follow direction** enabled, so frame 0 is not sent
  as translation-only with an arbitrary facing.

### Fixed

- **Snap to path vs. preview bake.** While `is_generating` or
  `is_previewing`, path snap no longer rewrites the root’s horizontal
  location curves from sparse Bezier control points, so dense glTF root
  translation from `/generate` is not replaced (which looked like snapping
  to the guide curve and foot sliding).

## [0.3.0] — 2026-05-01

### Added

- **Push to NLA workflow.** The Generate panel's preview action splits into
  one action per prompt block on commit, then assembles them on a single
  shared `Proscenium: Motion` NLA track. Previously-named "Accept" is now
  "Push to NLA" — the operator's `bl_idname` (`proscenium.accept`) is
  preserved for keymap / external compatibility.
- **Per-block action names from prompts.** Generated motion actions are now
  named after the user's prompt — `Proscenium_Motion: a person jumps` —
  with truncation to fit Blender's 63-char action-name limit.
- **Generation window from authored content.** The new
  `request_builder.compute_frame_range` derives the request's frame range
  from the union of enabled prompt blocks and source-action keyframes
  instead of the scene's `frame_start..frame_end`. Short edits no longer
  pay the cost of a full timeline.
- **Pose-generator: Preserve height option.** New checkbox in the Generate
  Pose dialog (default off → height matches the generated pose). When on,
  only the rig's local rotations apply, leaving the world XY *and* world Z
  untouched. When off, the model's height is applied while world XY stays
  pinned.
- **Pose-generator: persistent prompt.** The dialog pre-fills with the most
  recent prompt the user submitted (`scene.proscenium.last_pose_prompt`),
  so iterating on phrasings doesn't require retyping.
- **Effector pin: end-effector restriction.** The pin-joint dropdown now
  only offers the four canonical end-effectors (`LeftHand`, `RightHand`,
  `LeftFoot`, `RightFoot`); pinning interior chain joints would
  over-constrain the IK solver.

### Changed

- **Action naming.** `Proscenium_Generated` → `Proscenium_Motion: <prompt>`
  for full-motion bakes. `Proscenium_Poses` → `Proscenium_Pose` for the
  pose generator's output. The internal `_GENERATED_ACTION_PREFIXES` tuple
  catches both legacy and new names so back-compat with older scenes is
  preserved.
- **Anchor-frame tagging.** All source-action fcurve keyframes (rotation,
  location, and other channels) are now collected into the `KEYFRAME` tag
  set, not just rotation-bearing pose keyframes. Hand-authored Hips paths
  and other location-only keys keep their dopesheet styling after a bake.

### Fixed

- `AttributeError: 'NoneType' object has no attribute 'action'` in the
  Generate / Reject operators when an armature had no `animation_data`
  block yet (common after orphan-purge or a fresh canonical-skeleton
  import). Both call sites now `animation_data_create()` before assigning.
- Effector-pin and root-path samplers ship the wrong (previous-frame)
  world position when the empty / armature is parented or driven. Fixed
  by forcing a `view_layer.update()` after every `scene.frame_set` in
  `sample_effector_target` and `_root_keyframe_points`, matching the
  defensive flush already present in `sample_pose_keyframes`.
- The Generate Pose dialog appearing empty on first open after install
  (missing `BoolProperty` import; non-Property type annotations on
  `_thread`/`_result`/etc. tripping Blender's annotation resolver under
  `from __future__ import annotations`).
- NLA strips created via `track.strips.new` defaulting to `influence=0`
  in Blender 5.x — strips were silently producing zero contribution.
- Per-block bakes that switch the active action mid-bake leaving Blender
  5.x's NLA evaluator in a stale state where strips referencing the
  touched-then-detached actions silently produced zero animation. The
  split path now writes fcurves directly via the layered Action API
  (`action.layers.new` → `strip.channelbag(slot, ensure=True)
  .fcurves.new`), so the active action is never switched during writes.

### Internal

- `bake_gltf_to_actions_per_block` (`gltf_to_blender.py`): layered-Action
  bake that writes N actions in one pass with per-block frame filtering;
  retained for future surgical-regen use even though the live "preview
  then split on Push to NLA" path now goes through `_split_action_into_blocks`.
- `_block_ranges_for_split`, `_push_actions_to_nla`,
  `_clear_proscenium_nla_tracks`, `_split_action_into_blocks` (`operators.py`).
- `_is_orphan(action)` helper accounting for `use_fake_user` so the Reject
  cleanup loop correctly identifies per-block actions whose only reference
  is the fake user.

## [0.2.0]

- Bundled SOMA77 body mesh, skinned to the imported canonical armature.

## [0.1.0]

- Initial public release.

[0.4.0]: https://github.com/animatica-ai/proscenium-blender/releases/tag/v0.4.0
[0.3.2]: https://github.com/animatica-ai/proscenium-blender/releases/tag/v0.3.2
[0.3.1]: https://github.com/animatica-ai/proscenium-blender/releases/tag/v0.3.1
[0.3.0]: https://github.com/animatica-ai/proscenium-blender/releases/tag/v0.3.0
[0.2.0]: https://github.com/animatica-ai/proscenium-blender/releases/tag/v0.2.0
[0.1.0]: https://github.com/animatica-ai/proscenium-blender/releases/tag/v0.1.0
