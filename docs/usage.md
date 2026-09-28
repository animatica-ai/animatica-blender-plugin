# Using Animatica

**Videos first?** [YouTube tutorial playlist](https://www.youtube.com/watch?v=Wc349qOwjfM&list=PLAJ2UfUYhFQKZpFS8eh1eGUWJ0PAys1n1)

## Before you generate

1. [Install](installation.md) and [sign in](configuration.md)
2. In the 3D View, open the **N** panel → **Animatica** tab — it connects on
   its own

The tab has four panels, top to bottom:

- **Animatica** — the shot: model, character, prompt, Generate, and the review
- **Pose** — the character's pose: the Autoposer, Set Keyframe, fingers
- **Constraints** — waypoints (where to stand, when) and pins (a hand or foot held in place)
- **Settings** — set once and left alone, with **Viewport**, **Posing** and **Advanced** inside

## The Animatica panel

### Model and character

The **Model** and **Armature** are always at the top, so you can change either
at any time.

**Starting fresh?**
Click **Add Ready-Made Character**, then animate that character. It downloads
the first time. You can switch to your own rig later.

**Already have an armature?**
Pick it under **or animate your own rig**, or in the **Armature** field.

**Want to see a finished shot first?**
The file icon in the panel's header opens an **example scene**: a character on
a set, with the prompt already in place, ready for **Generate Motion**. The
examples climb from a single gesture to a sequence of key poses. Opening one
asks first, because it replaces your current file; it opens with scripts off.
Use **Save As** to keep your changes somewhere of your own.

**Several characters?**
With **Follow Selection** on (the arrow icon next to the Armature field, on by
default), selecting a character — its armature or its body — makes it the one
Animatica animates, with its own prompts and key poses. While a take is being
made or reviewed, the character stays until you Accept or Reject; the review
says *Accept or Reject to switch to …*.

With **Animatica Cloud** you can generate onto many custom armatures, not only
the ready-made character.

### Prompt and Generate

Type what the character should do in **Prompt** (for example "walks forward
sadly"). This is the selected Timeline block's prompt; more blocks go on the
Timeline (see [Prompt blocks](#prompt-blocks-on-the-timeline)).

Press **Generate Motion**. It is usually under 30 seconds; the first run after
a while can take a minute while the cloud wakes up. The panel shows
**Working… Ns** and a **Cancel** button.

If something stops a take being sent, the button greys out and the line under
it says why. The grey line below the options says how long the take is, which
frames, and how many key poses it will hit. A key pose outside that range is
named in red: it will not be sent.

### Options

Under **Generate Motion**:

- **Loop** — the model makes the block a seamless cycle: a walk, a run, an
  idle. It plays on past its end, and ticking it ticks **In place** too, so
  the cycle stays on the spot. Loop needs a single prompt block, and cycles
  work best at 2–4 seconds: the block's length is the loop's length. It only
  appears when the model supports it.
- **In place** — keeps the character on the spot. Only the travel is taken
  out: the path it moves along (a line, an arc, or a curve). The body's sway
  and bounce, jumps and crouches all stay. You can set it before generating
  or switch it on and off while reviewing, without generating again.
- **Variations** — how many versions of the take one Generate makes: the same
  prompts and poses, performed differently. It only appears when the model
  supports it.

### Reviewing a take

A new take waits for you in the **Reviewing take** box at the top of the panel.
Play it in the viewport, then decide.

- **Variation 1 of N** — with Variations above 1, the arrows flip between the
  versions without generating again. Accept keeps the one showing.
- **Seed N** and **Lock** — the seed this take used. Lock it to get this take
  again next time; otherwise every Generate is a new take.
- **In place** — switch it on or off for this take. Switching it off brings
  the travel back.
- **Generate Again** — a new take with the same direction.
- **Regenerate Active Block** — with two or more prompt blocks, makes the
  selected block again and keeps its neighbours.

**Accept** keeps the take.

Each accepted take is added to the NLA as its own named clip, on its own
track (*Animatica: <prompt>*), above the takes you accepted earlier, which stay
where they are. A take with several prompt blocks becomes one clip per block,
each on its own track, so every block exports as its own animation. The action
you had before you generated is kept too, stashed on a muted *[Action Stash]*
track, and your keys either side of the take keep playing. A kept loop keeps
cycling to the end of the scene. The one exception is a take generated into a
gap between your own keys: that one is filled into your action instead.

In NLA tweak mode, Generate, Accept and Reject wait: press **Tab** in the NLA
editor to leave it first.

**Reject** throws the take away and puts back exactly what you had: your
action, your pose and the scene's frame range. Keys you added or changed
during the review are kept. Takes you accepted earlier stay.

A few things you can rely on:

- Accept, Reject and Generate are one undo step each.
- Saving the file during a review is safe. Reopen it and the take is still
  waiting, and Reject still knows what to put back.
- A loop keeps cycling after you accept it.
- What In place took out is kept with the accepted take as its root motion,
  so it is not lost.

### Several characters at once

Select two or more characters and the panel offers **Generate N Characters**,
with a line saying how many generations it will cost. Choose how they are
directed:

- **Each Their Own** — every character uses its own prompts, key poses,
  waypoints and pins. For a scene with distinct actors.
- **Shared** — every character uses the active character's prompts, each with
  its own seed, starting from where it stands: variations on one action, for a
  crowd. Waypoints and pins are left out.

They are sent together, in one request when the server takes batches. The
review then has a row per character, with its own ✓ (keep), ✗ (throw away)
and ↻ (generate that one again). **Accept All** and **Reject All** act on all
of them, and **Regenerate All** makes every waiting take again. With
**Variations** above 1, each row has its own arrows, and **All: next
variation** flips the whole crowd. **In place** acts on every take waiting. A
character that failed is listed with the reason and does not stop the rest.

## The Pose panel

### Autoposer

The **Autoposer** lets you drag a hand, foot or the hips and have the body
follow. It is optional and runs on your machine.

- The first time, press **Download Autoposer** (about 225 MB, once). Nothing
  downloads until you do. See [the Autoposer](configuration.md#the-autoposer).
- Then **Start the Autoposer** gives your character its handles.
- The handles are listed by name (**Hips**, **Chest**, **L hand**, **Head** and
  so on): click one to switch it on or off, or **+** to add one.
- **Slack** is how far a joint may stray from its handle. Low puts the joint
  exactly where you put the handle; high lets the poser keep the body natural.
  The icon next to it shows each handle's own settings.

The handles follow the playhead, so scrub to any frame and grab one. With the
record button next to **Set Keyframe** on (the default), posing with the
handles keys the pose where you made it. Turn it off to try a pose out without
keying it.

### Set Keyframe

**Set Keyframe** keys the pose you are looking at and marks it as yours, so
the next take is asked to hit it. The line under it says how many key poses
the next take will hit. It works with or without the Autoposer, and with no
server connected.

> **Use Set Keyframe rather than `I` on top of a generated take.** Blender
> keeps a keyframe's existing type when you key over one, so pressing `I` on a
> frame a take already keyed leaves a key the addon reads as the model's own —
> it gets no ghost and is left out of the next request. **Set Keyframe** marks
> the pose as yours.

**Jump to Key Pose** (in the `F3` search) steps the playhead between your own
poses, which Blender's keyframe jump cannot do once a take has keyed every
frame.

### Generate Pose at Frame

**Generate Pose at Frame N** makes one pose at the current frame — handy for
blocking or fixing a single key pose. It won't replace your whole action. It
only appears when the model supports single poses (Animatica Cloud).

### Fingers

The model does not move fingers, so each hand gets a shape laid over every
take: **Relaxed** (the default), **Gripping** (closed around a handle) or
**Straight** (as generated). Set **Left Hand** and **Right Hand** separately.

## See the plan — ghosts and the trail

Your key poses are the plan: each one is a full-body pose the motion has to
pass through. **Settings → Viewport** controls what is drawn.

**Ghosts.** Each pose you keyed appears as a ghost where it sits in the
scene, tinted with the colour of the prompt block it falls under and labelled
with its frame number. On the timeline, a diamond marks each pose in the
**Animatica** lane. A pose outside the generating range is greyed out in the
viewport and red on the timeline; widen a prompt block, or move the pose, to
bring it back.

**The trail.** Running through them is the motion trail: the path the take
actually follows, frame by frame, in the same colours. There is one dot per
frame, so spacing is timing (bunched is slow, spread is fast). The large
diamonds are your key poses and the white one is the playhead. It traces the
**hips**, **head**, **hands** and **feet**; switch each on or off in
**Settings → Viewport**.

**Click a ghost to edit that pose.** The playhead goes to its frame and the
rig goes into pose mode, with the Autoposer's handles on that pose if you use
it. When you are happy, **Set Keyframe** writes it back onto that frame.

**Drag the trail to repose the body.** Pull a point and that hand, foot or
the hips moves at that frame; the playhead stays where it is. The other joints
stay put and the Autoposer solves the body around the one you moved, with a
yellow skeleton showing the pose. Let go and it is keyed there as one of your
key poses. Dragging the hips shifts the weight over feet that stay planted.
**Hold Shift to move the whole pose** instead. The label by the cursor names
the frame you grabbed; **Esc** cancels. A click reaches for a key pose first;
to bend the curve between keys, click exactly on the dot you want. A handle
always wins the click over the trail.

**The root trajectory.** Tick **Root Trajectory** to draw the path the
character travels along, on the floor, coloured by speed. It is what In place
takes out. **Edit Root Trajectory** turns it into a curve you can edit: with
In place off, the character follows the new path as you edit it; with In place
on, the pose stays and the curve is the take's root motion. Press **Tab** to
finish, and the reset button next to it to go back to the original.

## The Constraints panel

- **Waypoint at N** — pins where the character stands at the current frame.
  Drag the circle to where it should be; the route between waypoints is the
  model's to plan. Each waypoint is a row: its frame (edit it in place), its
  **Facing** (**Along path**, or **Set** with an angle), a button to go to it,
  and one to remove it. **Face along the path** also tells the model to face
  the next waypoint at each one; it is off by default, since the model faces
  the way it walks.
- **Pin** — holds a hand or foot on an empty, so it stays on an object.

Waypoints and pins belong to the character that was active when you made
them. A root-path curve from an older file still works, and **To waypoints**
converts it.

## The Settings panel

- **Seed** — 0 means a new take every time. The button next to it picks a
  random seed; **Lock** keeps the one the last run used.
- **Quality** — **Best**, **Faster**, **Draft**, or **Custom** steps.
- **Motion Cleanup** — stops feet sliding and hits key poses and pins more
  exactly. On by default; adds a second or two.

**Viewport** (its header checkbox turns the whole overlay off):

| Setting | What it does |
|---|---|
| **Ghosts** | Draw the body at each pose you keyed |
| **Trail** | Trace the path the motion takes; **Hips**, **Head**, **Hands** and **Feet** pick the joints |
| **Root Trajectory** | Draw the travel path on the floor, with **Edit Root Trajectory** |
| **Frame Numbers** | Label each pose with its frame |
| **X-Ray** | Draw poses through the character instead of behind it |
| **Ghost Style** | *Auto* uses the skinned character if the rig has one, the skeleton otherwise; force *Mesh* or *Bones* |
| **Auto Refresh** | Re-read the plan when you key a pose or move the rig. Turn off on a heavy character and use **Refresh Ghosts** |
| **Rig In Front** / **Hide Skeleton** | How the rig itself is drawn |

**Posing:** **Solid Floor** stops the Autoposer putting any joint below the
floor; **Rest Pose** clears the pose back to the rest pose and re-seats the
handles.

**Advanced:** **Guidance** with its **Text Weight** (how literally the motion
follows the prompt) and **Constraint Weight** (how tightly it sticks to your
poses, waypoints and pins), and **Blend Between Blocks** (frames blended where
one prompt block meets the next; 0 is a hard cut). The same number of frames
blends a take into your own keys where they carry on past the first or last
block; with no keys of yours there, the take starts and ends exactly at the
blocks.

## Prompt blocks on the timeline

Prompt blocks live on Blender's **Timeline** as coloured strips in the
**Animatica** lane:

- **Click** a block to select it; its prompt shows in the sidebar
- **Drag** a block to move it; **drag its edges** to resize it
- **Drag** the top edge of the lane to make the strips taller
- **Double-click** an empty part of the lane to add a block
- **Double-click** a block to type its prompt
- **Delete** / **Backspace** removes the block under the cursor
- The **+ / −** buttons in the Timeline header add and remove blocks too

**Right-click** a block for its menu: **Edit Prompt**, **Enable** / **Disable**,
**Regenerate Range** (while reviewing a take), **Delete Strip**, the seed it
was generated with (**Reuse seed** pins it to that block), **Add Strip Between
Keyframes** and **Add Strip in Gap**.

A block with no prompt is hatched ("Double-click to describe the motion"): the
model fills that stretch on its own. A disabled block is skipped entirely.

> Non-Latin text (for example CJK) can't be typed in the on-strip editor — use
> right-click → **Edit Prompt**.

## Help

- [Tutorial videos](https://www.youtube.com/watch?v=Wc349qOwjfM&list=PLAJ2UfUYhFQKZpFS8eh1eGUWJ0PAys1n1)
- [Discord community](https://discord.com/invite/A8CrURBewz)
- The **?** in the Animatica panel's header

See also: [Tips & limits](limitations.md)
