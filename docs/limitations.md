# Tips & limits

Things that are useful to know up front — not bugs, just how Animatica works today.

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
