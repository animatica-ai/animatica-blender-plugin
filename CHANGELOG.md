# Changelog

All notable changes to the Animatica for Blender addon are documented here.
The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

Entries for 0.4.0 and earlier describe the addon under its former name,
Proscenium, and keep the identifiers those releases actually shipped.

## [Unreleased]

### Added

- **See the plan before you generate.** The poses you key are the direction the
  model is given — each one becomes a full-body `pose_keyframe` constraint the
  motion has to pass through — and they were the one part of that direction you
  could not see. Scrub away from a key and nothing remained of it; nothing said
  which prompt block a pose belonged to; and a pose outside the generating range
  was dropped from the request in silence. The new **Ghosts** panel and viewport
  overlay show the plan itself: every key pose ghosted where it sits in
  the scene, **tinted with the colour of the prompt block it falls under** so the
  viewport and the timeline agree about which pose belongs to which instruction,
  **labelled with its frame**, and **greyed out and flagged when the request will
  not carry it**. The panel itself is just the switches for what to draw, plus
  the one thing worth interrupting for: the frames that will not be sent.

- **Motion trails, coloured by the plan.** The path the animation actually takes
  is drawn through the key poses that asked for it, sampled frame by frame and
  carrying the same colours — so the curve changes colour where the prompt blocks
  change, and you can see which stretch of motion belongs to which instruction. A
  dot per frame makes the timing readable at a glance (bunched is slow, spread is
  fast), larger diamonds mark your key poses on the curve, and a white one marks
  the playhead. It traces the joints the model is steered by — hands, feet, root
  and head — resolved through the same name matching effector pins use, so a
  namespaced, Mixamo or differently-spelled rig all land on the right bones, and
  a control rig is traced on its deform skeleton. Frames
  outside the generating range are drawn dim, like the poses there. The ghosts
  and the trail are independent — each has its own switch and its own cache, so
  showing one on its own costs nothing and switching one never disturbs or
  re-bakes the other.

- **The Autoposer is part of Animatica.** The neural control rig that was a
  separate addon is ported in: its panels live under the Animatica tab, its
  settings are a section of Animatica's preferences, and the inference package
  is vendored so nothing is fetched but the model itself. The data directory is
  unchanged, so a machine that already downloaded the model keeps using it.
  Disable the standalone addon — the preferences say so when both are enabled,
  since they register the same operators.

- **Motion curves are editable — drag one and the body follows.** Pull a point
  on a trail and that end effector moves at *that* frame, with the playhead
  staying where it is. The other traced joints stay pinned where they were, so
  a drag changes one effector rather than reinterpreting the pose; the
  Autoposer solves the body around it, and a skeleton ghost shows the pose the
  drag would commit before it is committed. Releasing keys that pose at that
  frame as one of yours, so it becomes a full-body constraint like any other
  key pose. The solve is a pure function of its effectors — it never reads the
  rig's current pose — which is what lets it answer for frame 40 while the
  artist is looking at frame 104. Measured: ~9 ms a solve (about 100 Hz), and
  the rig reaches the dragged position within 0.1 cm at that frame.

  One rule for every handle: a drag moves the joint you grabbed — the hips
  included, where it is a weight shift, the pelvis going over feet that stay
  put — and **Shift** moves the whole pose instead, every effector by one
  delta, so the character is carried bodily to a new place with its shape
  intact (measured: 0.00 cm of distortion under an 0.85 m move). Shift is read
  on every mouse move rather than latched at the press, so it can be taken up
  or dropped mid-drag; the header names it while dragging.

  **A click reaches for a key pose first.** The points are a dot per frame and
  at a normal zoom the neighbours sit less than a pixel apart — measured 0.6 px
  on a walk — so grabbing the frame next to the one you meant was the first
  thing to go wrong in real use. A key pose now wins from 18 px away, an
  in-between has to be hit almost exactly, and the frame you grabbed is named
  beside the cursor while you drag.

- **Click a ghost to edit that pose.** The ghosts show where the key poses are;
  clicking one makes it the handle you grab to change it. The playhead goes to
  that frame, the rig goes into pose mode, and where the Autoposer is driving
  that rig the pose is handed to it — controls seated on the pose that is there,
  then the rig taken over so the solve survives. **Set Keyframe** writes the
  edited pose back onto that frame's keyframe as authored keys, so what you
  changed is what the next generation is asked to hit, and hands the rig back.
  Clicks are
  hit-tested against the ghost's own geometry, so only the silhouette you can
  see is clickable, a click landing on the character in front of a ghost goes to
  the character as it always did, and a click that hits nothing passes straight
  through to selection.

- **Key poses are marked on the Animatica timeline lane.** A diamond per
  authored pose, drawn over the prompt strips, so you can see at a glance which
  block each pose lands in — and in red when it falls outside the generating
  range. Blender's own keyframe row cannot show this: after a generation it is a
  solid band of baked samples, one per frame.

- **Set Keyframe**, with the generate buttons. Keys the pose you are looking at
  and marks it as yours, so the next generation is asked to hit it. This is not
  a shortcut for pressing `I`:
  Blender keeps a keyframe's existing type when you key over an existing one, so
  posing on a frame a previous generation baked leaves a `GENERATED`-typed key —
  and the addon reads that type as "the model produced this" and leaves the pose
  out of the request. Poses added this way are typed as authored, so they
  survive **Reject** and are sent as constraints. It needs no server, so it is
  there whether or not you have connected. **Jump to Key Pose** (`F3` search)
  steps the playhead between your own poses — which Blender's keyframe jump
  cannot do once a generated take has put a key on every frame.

### Fixed

- **The inference runtime installs itself.** Solving a pose needs onnxruntime,
  and the addon used to stop at a message asking the artist to open the
  preferences and press Install Runtime. That step exists only because the
  download has to happen somewhere — it is a dependency of the addon, not a
  choice within it. It is now fetched in the background the first time it is
  missing, once per machine, and the preferences say so while it runs and offer
  the button back if it fails. Turn **Install runtime automatically** off for a
  machine that should not fetch it. The model is still explicit: it is
  account-gated, and only the artist has the token.

- **An Autoposer control wins the click over the motion curve.** They overlap
  by construction: the controls are re-seated onto their joints every frame and
  the trail runs through those same joints, so the trail's marker for the
  current frame sits exactly under the control that drives it. Measured before
  the fix, a click on any control was picked up as a curve drag instead — the
  hand control grabbed the hand curve, the foot control the foot curve. A
  control under the cursor now passes the click to Blender, which is where a
  click on a bone belongs; its grab radius comes from the control's own size on
  screen, so it holds at any zoom.

- **Live posing was on in name only, and keyed nothing.** Two faults met.
  `ap_live` is a scene property whose update callback fires when it *changes*,
  so a file load or an addon reload left it True with no timer behind it —
  live, dead, and no way to tell from the UI. And the debounced write cleared
  the captured pose one line before using it, so every auto-key through the
  timer wrote nothing: the solve appeared, then went at the next frame change.
  Tests had called the writer directly and never crossed the timer. Both fixed,
  and the timer is now restarted on load, on reload, and whenever the poser is
  made ready.

- **Live solving belongs to an edit, and only to one.** It used to be a timer
  running whatever was happening. Re-seating the controls on a frame change
  moves them, which reads as a control having been dragged — so scrubbing, and
  playback, and a generation sampling frame by frame, could each provoke a
  solve, and a solve is now keyed. It solves only while the artist is posing:
  pose mode, on this rig, not playing, not generating. Re-seating takes the new
  positions as its baseline rather than as an edit. Verified: scrubbing through
  five frames creates no key poses, while moving a control keys exactly one.

- **Dragging a control poses the body, without arming anything first.** Live
  solving was a switch that defaulted to off, so a control moved nothing until
  Solve was pressed — and the panel offered Solve, Key Pose and Snap, all three
  of which are now things that happen by themselves: the body follows a control
  as it moves, the pose is keyed where it was made, the controls re-seat on
  every frame change. The buttons are gone with the work they used to ask for
  (they remain in the search menu), and Live is held on wherever the poser is
  made ready. The engine also loads in the background once its pieces are on
  the machine, so the first drag is not the one that pays for it: 23 ms instead
  of 1657 ms.

- **The Autoposer works at whatever frame you are on.** Its controls are free
  bones that stay where they were last put, while the joints they drive move
  with the animation — so scrubbing anywhere left them behind. Measured on a
  walk: 1.7 to 2.5 metres from their joints. Grab one there and the solve does
  what it is told, which is to drag the body back to where the handle is, and
  posing anywhere but the frame the controls happened to be seated on looked
  broken. They are now re-seated on every frame change, about 2 ms, so the
  Autoposer is simply available where the playhead is. A ghost bake walks a
  hundred frames with that muted and re-seats them once at the end, rather than
  a hundred times on the way through.

- **Posing with the controls keys the pose, so nothing is detached any more.**
  A solve lives in `matrix_basis`, which the next animation evaluation
  overwrites, and the Autoposer's answer was **Take Over Rig**: detach the
  action, hold the pose, hand it back afterwards. That is a mode to be in, to
  remember being in, and to get out of — and it invited the worst failure this
  addon has had, where generating while held meant giving back swapped the
  result for the action from before. The motion-curve drag never needed any of
  it, because it writes keys instead of posing the rig. Control posing now does
  the same: a solve is captured as it happens and keyed at the frame it was
  made for once the drag settles, so the action carries the pose. Measured on a
  rig with an action bound: 0.06 cm of drift through a frame change, against
  434 cm for a pose that was keyed a quarter of a second too late — the capture
  has to be synchronous, the write does not. The Take Over and Give Back
  buttons are gone from the Autoposer panel with the problem they solved; a rig
  left detached by an older session can still be handed back from the main
  panel.

- **One character, not two.** The Autoposer carried a rig picker of its own, so
  it was possible to pose one armature and generate another — and to wonder why
  editing a key pose changed nothing. It now works on Animatica's target
  armature, which is the only place a character is chosen; its panel shows
  which rig that is rather than offering a second choice, and switching the
  target carries it along. The control bones are built the first time a pose is
  opened for editing, so there is nothing to press first.

- **A build can carry the model.** `make zip-with-model MODEL_DIR=<bundle>`
  stages the weights into `autoposer/model/` inside the zip, and a model that
  ships with the addon is used ahead of fetching one — so a test build needs no
  Hugging Face account, no token and no download. A release carries none and
  behaves exactly as before. The weights are staged in a temp tree, never in
  the working copy, and the path is in `.gitignore` besides.

- **The Autoposer can always be given the rig back.** Taking over detaches the
  action — that is how a solved pose survives a frame change — but the only
  ways out of that state were committing a pose or the Autoposer's own panel,
  so a rig could sit held with the overlay reading "editing" and no obvious
  way to leave. The main panel now says when the Autoposer is holding the rig
  and offers **Give Back Rig** beside it.

  Giving back is careful about which action wins. If something has bound one
  since the take-over — a generation's result, or poses keyed into a new
  action — re-attaching the stash would swap that work out for what was there
  before, so in that case it lets go and keeps what is bound. Keys are written
  to whatever is bound too, for the same reason: writing into a stashed action
  while another one is playing puts the pose where nothing is looking.

  The "editing" state no longer sticks either: it is the playhead being on the
  frame that was opened, nothing more, so scrubbing away ends it.

- **The ghosts no longer go out when you press Generate.** A rebake is held
  while a generation runs or the animation plays — it steps the playhead, which
  would fight both — and Generate swaps the rig's action, so the overlay went
  stale at the exact moment it could not refresh. It blanked itself and stayed
  blank until Refresh was pressed, which looked like the feature breaking
  whenever it was used. The last bake is now drawn while a refresh is pending:
  briefly a frame or two out of date, rather than gone. Switching to a
  different armature still clears it, since those poses are somewhere else
  entirely, and the panel says when a refresh is waiting and on what. The same
  hold was behind trail and pose toggles sometimes needing a Refresh.

  Playback no longer holds a refresh at all: a bake restores the frame it
  started on, and the player cannot advance while it holds the main thread, so
  it resumes exactly where it was. Measured: 91 ms for four ghosts and a
  110-frame trail, mid-playback, frame 28 in and frame 28 out. Only a running
  generation still waits, because it owns the playhead while it samples.

- **The pose-keyframe count in Constraints counts poses, not curve points.** It
  summed every keyframe point on every rotation channel, so a rig carrying a
  generated take reported tens of thousands of "pose keyframes" — one per bone
  per channel per frame — where the request sends one constraint per authored
  frame. It now reads the authored frames, so the number matches what goes out.

## [0.5.3] — 2026-09-16

### Fixed

- **A prompt block now generates only its own stretch of the timeline.** The
  generation window was the union of the enabled blocks *and* the source
  action's entire keyframe span, so blocking out poses across 200 frames and
  then asking for one 40-frame block generated all 200: the request carried
  ~160 frames of `unconditioned` segment, every authored keyframe outside the
  block was sent as a pose constraint, and the bake overwrote the work the
  user had deliberately left alone. Blocks now win outright — the window is
  their span widened by `transition_frames` on each side, which is the margin
  the server needs to blend the splice. With no blocks the old behaviour
  stands, since there is then no statement of intent to honour.
- **Generating over a block keeps the motion either side of it.** The bake
  writes an action covering only the generated window, so once that window was
  correctly narrowed to the block, everything authored before and after it
  vanished from the preview — the source action was safely stashed for Reject,
  but on screen it read as the keyframes having been thrown away. Those keys
  are now carried into the preview, so Generate splices rather than replaces:
  the original motion outside, the new motion inside, in one action. They keep
  their original keyframe type, so Reject still strips only what the bake
  produced and restores the source exactly.

- **Generating over a gap fills it in place, in the action you are working
  in.** It used to bake a new action and, on Accept, push it to an NLA strip
  while detaching yours — so a splice handed back a differently-named strip
  instead of the animation you were editing. When the rig already carries
  motion either side of the window, the frames are now spliced straight into
  that action: Accept keeps it, Reject removes the generated samples and the
  gap comes back exactly. A fresh bake is still what happens when there is
  nothing to preserve, and control rigs keep the old path, which is the only
  one that does the control-rig hand-off.
- **Poses in an edited generated take anchor the next generation.** Both the
  pose-keyframe sampler and the frame-range fallback skipped an action whose
  *name* began with `Animatica_Motion:`, to avoid feeding a previous bake back
  in. That silenced every pose a user had authored in such an action — the
  normal state of a spliced or hand-edited take — so a generation went out
  with nothing anchoring it and wandered metres away from where it had to end
  up. The sampler now filters by keyframe *type*, which is the precise signal:
  bakes tag their dense samples `GENERATED` and leave authored keys alone.

## [0.5.2] — 2026-09-12

### Fixed

- **Effector pins work on rigs that don't use canonical bone names.** The pin
  popup matched the canonical joint names (`LeftHand`) against the armature's
  bone names, so on any rig that namespaces its bones — the bundled Animatic
  character (`animatica:LeftHand`), anything out of Maya or MotionBuilder,
  Mixamo — it offered nothing at all and a pin could not be created. The joint
  a pin names now comes from the skeleton the request actually sends, which is
  what the request builder validates against and what the server retargets
  from, and the popup still labels it canonically. Rigs following other
  conventions resolve too (`hand.L`, `hand_ik.L`, `DEF-hand.L`, `L_Hand`); on a
  control rig only the deform bone is offered, since the control bone is never
  part of the request.

### Added

- **Pin any bone, not just the four end effectors.** The effector-pin dialog
  gains an *Any bone* mode with a bone picker, for rigs whose naming the addon
  cannot match and for pinning something other than a limb tip. The pick is
  checked against the skeleton the request sends, so a bone the server would
  never see is refused at the dialog rather than at generation — and on a
  control rig, picking the control the animator grabs (`hand_ik.L`) names the
  deform bone it drives (`DEF-hand.L`) in the error.

## [0.5.1] — 2026-09-11

### Fixed

- **A keyframe on one bone no longer poses the whole character.** Authorship was
  tracked by frame rather than by bone, so keying a single bone marked every
  bone at that frame as authored. On a 77-bone rig, two hips-only keys became
  616 keyframe points where the user had made 8; the next generation then read
  77 user-edited bones and pinned a whole body at the rig's rest pose, which
  looks like the character snapping to a T-pose. Anchors now carry the bone each
  key sits on, so a partial keyframe survives the bake as a partial keyframe.
- **Reject keeps a partial keyframe partial.** Stripping the preview's generated
  samples promoted every channel at an authored frame to a real key, and Reject
  then merged all of them into the user's own action — turning a hips-only
  keyframe into a 77-bone one. The promotion exists so unkeyed bones do not drop
  to rest, which only matters when the preview becomes the user's action
  outright; it is now asked for only in that case. This also fed the request
  builder, so the widening reached the next generation as well.

## [0.5.0] — 2026-09-10

### Added

- **Client attribution on generation requests.** The request that starts a
  generation now carries `X-Animatica-Client: blender`, the addon build
  (`X-Animatica-Client-Version`), the Blender it is running in
  (`X-Animatica-Host-Version`) and a session id generated once per addon
  launch, so the API can report DCC mix and failure rate per host version.
  Attribution only — a missing or wrong value never affects a request. The
  `202` poll follow-ups and `GET /capabilities` are deliberately not
  attributed; neither starts a generation.
- **The Animatic character is what "Import rig" loads.** The rigged, textured
  hero body — armature, skinned mesh and material — instead of a bare skeleton
  built from joint data. The SOMA30 rig and the model's canonical skeleton
  remain selectable on the import operator.
- **The character is downloaded on first use, not shipped in the addon.** It
  comes from `animatica-assets-public` (v003), pinned to a commit and to the
  sha256 that Git LFS already records for it, and cached in Blender's per-user
  datafiles so it survives addon upgrades. This takes ~12 MB out of the
  repository and the release zip. First import costs about a second; every one
  after that reads the cache. With no network the import says so and falls
  back to the SOMA30 rig.

### Fixed

- **Namespaced rigs are driven instead of silently skipped.** MMCP joint names
  are bare (`Hips`); rigs exported from Maya / MotionBuilder carry the source
  scene's namespace on every bone (`animatica:Hips`), so the bake's exact-name
  lookup matched nothing and every channel was dropped without an error. The
  bake now resolves through a whole-rig namespace when one is present
  (`gltf_to_blender.resolve_joint_bone`), and the pose-bake selection filter
  strips it on the way back out. Unprefixed rigs are unaffected.

### Changed

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

[0.5.3]: https://github.com/animatica-ai/animatica-blender-plugin/releases/tag/v0.5.3
[0.5.2]: https://github.com/animatica-ai/animatica-blender-plugin/releases/tag/v0.5.2
[0.5.1]: https://github.com/animatica-ai/animatica-blender-plugin/releases/tag/v0.5.1
[0.5.0]: https://github.com/animatica-ai/animatica-blender-plugin/releases/tag/v0.5.0
[0.4.0]: https://github.com/animatica-ai/proscenium-blender/releases/tag/v0.4.0
[0.3.2]: https://github.com/animatica-ai/proscenium-blender/releases/tag/v0.3.2
[0.3.1]: https://github.com/animatica-ai/proscenium-blender/releases/tag/v0.3.1
[0.3.0]: https://github.com/animatica-ai/proscenium-blender/releases/tag/v0.3.0
[0.2.0]: https://github.com/animatica-ai/proscenium-blender/releases/tag/v0.2.0
[0.1.0]: https://github.com/animatica-ai/proscenium-blender/releases/tag/v0.1.0
