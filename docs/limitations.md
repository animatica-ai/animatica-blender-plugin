# Tips & limits

Things that are useful to know up front — not bugs, just how Proscenium works today.

## Multiple models

Your server may offer more than one model in the **Model** dropdown (e.g.
`kimodo-soma-rp` and `ardy-core-rp`). They differ in ways the UI reflects
automatically:

- **Frame rate and clip length** — each model generates at its own fps and
  advertises its own duration limits; the duration hint under the seed row
  follows the selected model (ARDY tops out at 8 seconds).
- **Reference skeleton** — each model has its own canonical skeleton
  (SOMA's 30 joints vs ARDY Core's 27). You can keep working on an armature
  imported for another model — the server retargets — but the panel will
  point this out. The **body mesh** ships only for SOMA-family skeletons.
- **Features** — buttons like **Generate Pose @ Frame** appear only when the
  selected model supports them.
- **Prompt-block timing on ARDY** — ARDY generates in fixed windows (2 s
  for `ardy-core-rp`), and the prompt switches at the window boundary
  nearest each block boundary. Blocks much shorter than 2 s may share a
  window with a neighbor. Per-block seeds work — a pinned seed makes its
  block reproducible without freezing the blocks before it.

## Your character vs. the reference skeleton

If you use a **self-hosted** server on your own machine, it works best with
Animatica's **imported skeleton** or a rig that matches it closely.

**Animatica Cloud** can generate onto many of your own armatures. If the rig is
very different from the model's skeleton, results may vary — try the imported
skeleton first to learn the workflow.

## One result per generation

Each **Generate Motion** run gives you one animation to preview. If you want
another variation, adjust your prompts or poses and generate again.

## Generation takes a moment

Blender waits while the server works. On Animatica Cloud, the first run after
a long break can take a minute or more while GPUs wake up. Later runs are
usually faster. Don't close Blender mid-generation — use **Cancel** if you
need to stop.
