# Changelog

All notable changes to the Animatica for Blender addon are documented here.
The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

Entries for 0.4.0 and earlier describe the addon under its former name,
Proscenium, and keep the identifiers those releases actually shipped.

## [Unreleased]

### Changed

- **Posing and editing by hand are now Animatica Autoposer Pro, a separate
  add-on.** This add-on makes the motion and shows it: prompts and blocks,
  key poses, waypoints and pins, Generate, Redo, versions, Loop, In Place,
  Copy/Paste Motion, the ghosts and the onion skin, and the root trajectory
  as a view. Pro has everything that edits it by hand: the Autopose tool,
  the handle picker and Reset to T-Pose, the motion trail (drawn, dragged,
  Smooth), the zoetrope, Lock in Place, the reach of an edit (Reach and
  Intensity), clicking a ghost to edit its pose, and Edit Root Trajectory.
  With Pro installed these appear on the bar and in the panels as before.
  Without it, the bar starts at the Key group and ends with the onion skin,
  and you key poses the usual way: pose the rig in Blender, or use Pose This
  Frame in Words.
- **The onion skin's Frames mode shows Before and After.** Without Pro it
  shows the poses Before and After the playhead, Step frames apart, as
  Grease Pencil's onion skin does. With Pro it still shows the frames an
  edit reaches (Reach), or the zoetrope.
- **Settings that moved.** Reach, Intensity, the zoetrope's Step and Spacing,
  and the trail's switches are now Pro's. A file saved with them set opens
  with Pro's defaults. Locks keep working, and are stored on the rig as
  before.

### Added

- **Copy Motion / Paste Motion.** Select keys of a character in the Timeline
  and click Copy on the bar: one key copies a pose, several copy a stretch of
  motion, and with none selected every key is copied. Copy lights up and the
  line above the bar says what was copied.
- **Pasting.** Select another character, put the playhead where the motion
  should start, and click Paste. Animatica Cloud retargets the motion onto
  that character's skeleton and keys it into the character's own animation:
  a key on each copied key's frame, the first on the playhead. Keys already
  in that stretch are replaced and the rest of the animation is kept.
- **What it works on.** Mixamo characters, custom skeletons and Rigify rigs.
  Bones animated in Euler stay in Euler.
- **Cost and where to find it.** You need to be signed in, and it costs no
  generations. It is on the floating bar, in the sidebar ("Copy & Paste
  Motion") and in the viewport's right-click menu.
- **Fingers.** Fingers are not transferred yet. The target's own finger
  animation is kept.

### Fixed

- **Ghosts keep up with your keys.** Keys deleted, moved or retimed now show
  on the onion skin (and Pro's trail) within moments.
  That includes edits made while the animation plays, and edits made while
  the ghosts are still redrawing after the previous one. Before, either case
  could leave them showing the old motion until you pressed Refresh.
- **Rigify rigs read as one skeleton.** A Rigify rig's deform bones hang off
  its ORG and MCH bones, and the add-on used to guess their hierarchy from
  where the bones sit, which could root the skeleton at a thigh. The
  hierarchy is now read from the ORG bones, so the server sees the rig's real
  tree.

## [0.7.0] — preview

### Added

- **A floating toolbar in the viewport, in the order of the work.** Set
  keys, Generate, Accept, then fine-tune, left to right on one bar: the
  Autopose tool and the handle picker; step between key poses, Set Key and
  Auto Key; Waypoint and Pin; the prompt under the playhead, with a button
  inside the field that switches it to this frame's pose in words (Generate
  becomes Generate Pose); Loop and Generate, or Redo, Key This Frame, the
  variations and Discard while a take is judged; the onion skin, the
  zoetrope, the trail and the reach of an edit; Options; the model. Anything the
  connected model can't do is greyed out, here and in the sidebar. A hint
  above the bar names the next step and why.
- **Lock in Place.** Pick a hand or foot (its trail point or its handle)
  and the padlock on the bar holds it on one spot for a stretch of frames,
  so a planted foot stops sliding. The span starts as the contact around
  the playhead; move the mouse to set its end (Ctrl: its start), then click.
  The leg bends to hold it, easing in and out, the hips untouched. Locks are
  listed in the Pose panel; a new take over them is locked again, and a
  drag inside one holds the locked joint.
- **No Accept.** A take is yours when it arrives: while you judge it the
  bar offers Redo, Key This Frame, the variations and Discard (and Generate
  while blocks are left to make). Your first fine-tuning edit keeps it and
  locks its blocks, without turning the edit into key poses.
- **Loop on the bar.** Loop sits beside Generate. In a loop the zoetrope is
  a ring, a drum seen from above standing behind the character with each
  slice spun in place, and edits reach round the seam and keep the cycle
  closed.
- **The Autopose tool.** The Autoposer's handles on your character, no
  control rig: drag one and the body follows, G and R with axis locks,
  Blender's move gizmo on a picked handle, Ctrl-click to switch one on or
  off, and Add Handle for elbows and knees.
- **Onion skins and the zoetrope.** The motion around this frame as ghosts
  (Frames, Keyframes or All Keys, with Grease Pencil's controls), drawn
  during playback too. The zoetrope spreads them into a tunnel through time,
  every frame an edit reaches, with the motion trail running through them;
  each ghost can be posed where it is.
- **Edits that carry through time.** Drag or turn a handle, on the
  character, a ghost or a zoetrope slice, and the frames around follow with
  a falloff. Reach and Intensity are a curve on the bar you drag, and the
  ghosts and the trail re-pose live as you edit. On a take an edit reshapes
  its motion where it is keyed and adds no keyframes; the preview shows
  exactly what the curves will give.
- **Lock a block to keep its motion.** Accept keeps the take as your own
  action, keys you can edit, and locks the blocks it covered. Generate and
  Redo then leave a locked block as it is, and the blocks next to it are
  made to run into it, starting and ending on its pose. Work through a
  sequence a block at a time; unlock one (right-click it, or the padlock in
  the sidebar) to make it again. A take no longer goes onto the NLA as one
  track per block.
- **A handle picker for the Autoposer.** A card in the viewport with your
  character in T-pose and the Autoposer's handles on it. Pick one or
  several (Shift), switch them on or off (or double-click one), give them
  their rotation, set how strictly the pose keeps to them, remove them, or
  add one from the handles the rig can still have, shown where they go.
- **Drops to lower ground.** A take follows the ground under the route, so
  a character can jump from one roof down onto another. Waypoints sit on
  the ground, and the Autoposer and Describe stand the pose on it (Describe
  has **Stand on the ground**, on by default; off keeps a jump in the air).
  Needs a server with MMCP 1.3, as the Animatica cloud has.

### Changed

- **The timeline lane, redrawn.** Blocks as tiles with their colour as a
  swatch; the block under the playhead highlighted, as the toolbar's Prompt
  and Redo act on it. Frame numbers show on the block you hover, select or
  drag, with its length while you drag. A row of key poses above the
  blocks: drag one to move the pose to another frame, click it to go there.
  A row of waypoints below: drag one to retime it. The take under review is
  marked under its blocks, and a take being made sweeps across them.
- **A new block is 50 frames long** instead of filling the whole gap.

### Fixed

- **Clicking a block works when the scene has markers.** The marker row no
  longer takes the clicks meant for the lane.
- **A block can be shortened from the left** where it meets another one.
- **The add-on no longer stays stuck "working"** after a generation stops
  without finishing.
- **Fewer freezes on macOS.** The bar, the ghosts and the trails no longer
  build fresh GPU buffers on every redraw, and the ghost cache is capped.
- **A drag that is interrupted cleans up.** Loading a file or an error in
  the middle of a drag no longer leaves the add-on thinking a drag is on;
  Esc puts back the handles a drag switched on.
- **Handles can't be grabbed during playback**, when they are hidden.
- **Editing a travelling loop on a later repeat** no longer makes the
  character jump back to the first cycle while you drag.
- **The first fine-tuning edit no longer clears the ghosts.**
- **Dragging on a sparse take no longer lags** on a wide reach.
- **G or R on handles with no place in the scene** no longer raises an error.
- **A first take is quicker.** The hint asks for a prompt first, then says
  Ready: Generate; key poses are offered as the way to steer it, so a first
  session no longer starts with the 225 MB Autoposer download. A greyed
  Generate says "Type a prompt first", and clicking it opens the field.
- **Greyed buttons say why** when clicked or hovered (a tool the model can't
  use, Use Selected Rig with nothing selected).
- **Offline, the way in stays.** Use Selected Rig, Examples and a downloaded
  character stay on the bar without a connection; an unreachable server
  says so with Retry. Allow Online Access asks before it changes Blender's
  setting.
- **Failures in plain words**, kept on the hint with a retry, instead of
  protocol codes in a status line that faded. Bar buttons that fail no longer
  print a Python traceback.
- **A key pose outside the blocks is no longer left out silently.** Setting
  it warns, and the hint offers to stretch the block over it.
- **Esc no longer throws a generation away by accident:** press it twice.
- **The sidebar matches the bar** while a take is judged: Redo, Key This
  Frame and Discard, and Generate makes the rest (no Accept, no Generate
  Again).
- **The first Autopose click no longer fails** on a fresh install (the
  download prompt raised an error).

## [0.6.4] — 2026-09-30

### Fixed

- **A take lands where the server put it, whichever way the armature is
  turned.** The bake folded the armature object's rotation about Z into
  every bone, so a character turned 180° in object mode walked backwards
  with its knees bending the wrong way, and one turned 30° walked at a 30°
  skew. Generate, Regenerate Block, Pose and control rigs (Mixamo, Rigify)
  now all place the motion in world space as the server generated it, and
  a pose you key on a turned rig goes out facing the way you see it.
- **A Rigify rig moved or turned away from its metarig bakes where it
  stands.** The bake onto the controls read the metarig where it was
  generated, and nudged the torso by a world-space offset as if the rig
  were not turned, so a Rigify rig moved or turned in object mode came out
  displaced. On a turned Rigify rig the neck may still twist and the
  forearms roll a little: Rigify's follow settings blend the torso's turn
  into bones the bake does not key.

## [0.6.3] — 2026-09-30

### Changed

- **Running out of generations opens a dialog.** When a generation is refused
  because the monthly allowance is used up, a **Generation limit reached**
  dialog says so and offers **Get more generations**, which opens the page for
  credit packs and Pro. It replaces the error line in the status bar, and a
  batch opens it once, not once per character. The banner in the sidebar now
  appears straight away, too; before, it only showed once the mouse passed
  over the sidebar.

## [0.6.2] — 2026-09-29

### Changed

- **The Autoposer works on any humanoid rig.** It finds the hips, spine,
  arms, legs and head from the skeleton's shape instead of its bone names, so
  Mixamo, Unreal and other humanoid rigs get handles without renaming
  anything. A-pose rigs, rigs exported Y-up or in centimetres, and rigs with
  extra spine or twist bones are posed correctly. **Skeleton** in the **Pose**
  panel shows which bone plays each joint and lets you change it. On a control
  rig the Autoposer now says why it can't start instead of posing nothing.

## [0.6.1] — 2026-09-28

### Fixed

- **No extra frames after your blocks.** A take covers exactly your prompt
  blocks: it no longer adds about five frames of motion after (and before)
  them unless there is animation of yours there to blend into.
- **Regenerate Block keeps the take's length.** Regenerating the last block
  grew the take by one frame; it now ends where it did.

## [0.6.0] — 2026-09-28

You can now pose the character by dragging it, see the plan in the viewport,
get several takes from one Generate, animate several characters together and
make game-ready loops. The addon also updates itself.

### Added

- **The Autoposer.** Drag a hand, a foot or the hips and the whole body
  follows. It lives at the top of the **Pose** panel, works on the character
  you generate for, and follows the playhead, so you can pose at any frame.
  Posing with its handles keys the pose where you made it (the record button
  next to **Set Keyframe** turns that off). **Slack** sets how strictly a
  joint sticks to its handle. It is optional: it runs on your machine and
  needs a one-off download of about 225 MB, which only happens when you press
  **Download Autoposer**. The standalone Autoposer addon is no longer needed.
- **See the plan.** Your key poses appear as ghosts in the viewport, tinted
  with the colour of their prompt block and labelled with their frame, and a
  motion trail shows the path the take follows through the hips, head, hands
  and feet. Poses outside the generating range are greyed out and named in
  the panel, so nothing is dropped silently. Key poses are also marked with
  diamonds on the Animatica timeline lane. All of it is in **Settings →
  Viewport**.
- **Drag the trail to repose the body.** Pull a point on the trail and that
  joint moves at that frame while the Autoposer solves the rest; let go and
  it is keyed as your pose. Hold **Shift** to move the whole pose. Click a
  ghost to jump to that pose and edit it.
- **Set Keyframe**, which keys the pose you see and marks it as yours, so the
  next take is asked to hit it (pressing `I` over a generated key does not).
  **Jump to Key Pose** steps between your own poses.
- **Waypoints.** **Waypoint at N** in the new **Constraints** panel pins where
  the character stands at a frame; drag the circle to place it, and choose
  which way it faces there. Old root-path curves convert with **To
  waypoints**. Waypoints and pins belong to the character they were made for.
- **Variations.** Set **Variations** and one Generate makes several versions
  of the take. Flip between them in the review and Accept the one you want.
- **Several characters at once.** Select two or more characters and press
  **Generate N Characters**, each with its own prompts (**Each Their Own**) or
  all sharing the active one's (**Shared**, for a crowd). The review has a
  row per character to keep, throw away or regenerate it, plus **Accept All**,
  **Reject All** and **Regenerate All**.
- **Loop.** Tick **Loop** and the model makes the block a seamless cycle — a
  walk, a run, an idle — for games. Shown when the model supports it.
- **Root trajectory.** **Settings → Viewport → Root Trajectory** draws the
  path the character travels on, coloured by speed. **Edit Root Trajectory**
  turns it into a curve you can reshape to send the take along a new path.
- **Follow Selection.** Select a character, or its body, and it becomes the
  one Animatica animates. On by default; the toggle is next to the Armature
  field.
- **Example scenes.** The icon in the Animatica panel's header opens a
  finished scene, ready to generate. The examples climb from a single gesture
  to a sequence with key poses.
- **Fingers.** The model doesn't move fingers, so each hand now gets a shape:
  **Relaxed** (default), **Gripping** or **Straight**.
- **Built-in updates.** The addon checks GitHub for a new release at most once
  a day and installs it when you press **Update**. **Include previews** offers
  pre-releases too.
- **It connects by itself** on startup, after opening a file and after
  signing in, and tries again on its own if the server was unreachable.

### Changed

- **Accept keeps every take.** Each accepted take is added to the NLA as its
  own named clip on its own track (one per prompt block), and earlier takes
  stay. The action you had before is kept on a muted *[Action Stash]* track.
  Kept loops keep cycling to the end of the scene. A take generated into a
  gap between your own keys is still filled into your action. **Replace
  Kept** is gone, since nothing is replaced any more.
- **NLA tweak mode:** Generate, Accept and Reject wait until you leave it.
- **In place removes only the travel.** It used to pin the root, which also
  took out the body's sway and surge. Now it takes out just the path the
  character moves along (a line, an arc or a curve) and keeps everything the
  body does, including height. You can set it before generating or switch it
  on the take you are reviewing; switching it off brings the travel back.
- **A new sidebar**, ordered by how often you reach for things:
  **Animatica** (model, armature, prompt, Generate, options, review),
  **Pose** (Autoposer, Set Keyframe, fingers), **Constraints** (waypoints and
  pins) and **Settings** (seed, quality, Motion Cleanup, with **Viewport**,
  **Posing** and **Advanced** inside).
- **Every run is a new take.** The seed defaults to 0, so **Generate Again**
  gives something different; the review shows the seed a take used, with
  **Lock** to keep it.
- **Your first prompt can be typed in the sidebar**, and the first block is
  four seconds long rather than the whole scene.
- **Plainer words throughout:** quality is **Best / Faster / Draft**, the
  poser's tolerance is **Slack**, guidance and block blending live in
  **Settings → Advanced**, and the panel says how long a take will be and how
  many key poses it will hit.
- **Opening an example scene always asks first**, since it replaces your
  current file.
- **The preferences fit on one screen:** Account, Server and Poser, with the
  details folded away.
- **Blender's Allow Online Access is respected everywhere.** With it off,
  nothing goes online: the panels say so and offer **Open Preferences**. A
  self-hosted server on `localhost` still works.

### Fixed

- **Your work is safe during a review.**
  - Saving, or an autosave, while a take was waiting could delete your
    action. It can't any more, and a saved review reopens intact.
  - **Reject** puts back exactly what you had — your action, the pose of
    every bone and the frame range — plus only the keys you added or changed
    during the review. It no longer overwrites your keys with the model's, and
    it still finds your action if you renamed it.
  - Reject no longer throws away takes you accepted earlier.
  - A bake that failed no longer leaves an empty take with no way back.
  - Accept, Reject and Generate are one undo step each.
- **No more stuck "Working…".** A file saved mid-generation used to reopen
  stuck on "Working…" with no way out.
- **The viewport keeps working after you open a file.** Ghosts, the trail,
  root trajectory edits and the Autoposer stopped after opening a file or
  **File → New**.
- **Generating into a gap leaves the rest alone.** Bones you never keyed no
  longer move outside the gap, a bone posed in Euler keeps its pose either
  side, and such a take is not made a loop.
- **In place keeps your root keys.** A crouch or lean keyed with In place on
  is no longer lost when In place goes off, and lands where the character had
  walked to.
- **Generate works on rigs with long bone names**, such as namespaced Mixamo
  rigs.
- **Updates can't lose the addon.** A download that isn't a complete, working
  build is refused before anything is touched. If a new version won't load,
  the previous one is put back and enabled again, with your settings kept.
  Updating refuses on a linked development checkout, and waits for a running
  generation.
- **Your Animatica session only goes to Animatica Cloud**, over HTTPS. It was
  sent to a self-hosted server too, and followed redirects to other hosts.
  Signing in and renewing the session always go to the cloud.
- **Example scenes are pinned and can't run scripts.** They come from one
  fixed commit, each file must match its checksum, and they open with scripts
  off even if you have Auto Run on.
- **The Autoposer downloads only when you ask**, from a pinned model and a
  hash-checked onnxruntime 1.23.2. It used to download about 225 MB at
  startup. A Hugging Face token found on your machine is no longer sent to the
  public model. An out-of-date or damaged model offers the download again.
- **Linked characters say why** instead of failing half way: Build Rig, In
  place and key poses refuse with a short message, and Edit Root Trajectory
  works on a linked or library-override rig.
- **Build Rig no longer hides the skeleton** when it cannot build.
- **Set Keyframe works on a fresh rig**, and poses keyed with `I` show up in
  the plan.
- **An Autoposer handle wins the click** over the motion trail under it.
- **The ghosts no longer go out** when you press Generate or play the
  animation.
- **The empty block a new timeline starts with** no longer splits an
  accepted take in two.

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

[Unreleased]: https://github.com/animatica-ai/animatica-blender-plugin/compare/v0.6.4...HEAD
[0.6.4]: https://github.com/animatica-ai/animatica-blender-plugin/releases/tag/v0.6.4
[0.6.3]: https://github.com/animatica-ai/animatica-blender-plugin/releases/tag/v0.6.3
[0.6.2]: https://github.com/animatica-ai/animatica-blender-plugin/releases/tag/v0.6.2
[0.6.1]: https://github.com/animatica-ai/animatica-blender-plugin/releases/tag/v0.6.1
[0.6.0]: https://github.com/animatica-ai/animatica-blender-plugin/releases/tag/v0.6.0
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
