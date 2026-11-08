# Examples

Tool calls are shown as `codex_images` arguments. The CLI equivalent follows where there is one.

## One image with a transparent background

```json
{"prompt": "a matte black ceramic mug, three-quarter view, soft studio light", "transparent": true}
```

```bash
codex-images-mcp generate "a matte black ceramic mug, three-quarter view, soft studio light" --transparent
```

A warning appears in `warnings` if the model returned an opaque image anyway.

## A labeled spec

```json
{"prompt": {
  "primary_request": "hero illustration for a coffee subscription landing page",
  "style_medium": "flat vector illustration",
  "palette": "cream, espresso brown, sage green",
  "composition": "subject on the right third, empty space on the left for copy",
  "avoid": "text, logos"
}}
```

## Variants

```json
{"prompt": "app icon: a paper plane made of light", "batch_mode": "variants", "count": 4}
```

```bash
codex-images-mcp generate "app icon: a paper plane made of light" -n 4
```

## A consistent set (branded-parallel)

The anchor is generated first. Every other image references it.

```json
{
  "anchor": "brand mascot: a friendly round robot, teal and white, flat style",
  "batch_mode": "branded-parallel",
  "prompt": [
    "the mascot waving, for a welcome page",
    "the mascot reading a book, for a docs page",
    "the mascot holding a wrench, for a settings page"
  ]
}
```

## A sequence (chain)

Each image references the anchor and the previous one.

```json
{
  "anchor": "storyboard frame 1: a fox at the edge of a snowy forest at dusk",
  "batch_mode": "chain",
  "prompt": ["frame 2: the fox steps onto a frozen lake", "frame 3: the fox looks back at the forest"]
}
```

## Edit with a reference and a mask

```json
{
  "prompt": "replace the sky with a starry night",
  "references": [{"source": {"type": "inline_blob", "data": "<base64 photo>"}, "role": "content"}],
  "mask": {"source": {"type": "inline_blob", "data": "<base64 mask>"}}
}
```

```bash
codex-images-mcp generate "replace the sky with a starry night" -r photo.jpg --mask sky-mask.png
```

The backend treats the mask as a hint and may adjust the whole frame.

## Exact size for a social card

```json
{"prompt": "minimal social card, bold geometric shapes", "output": {"format": "jpeg", "width": 1200, "height": 630, "size_policy": "cover"}}
```

```bash
codex-images-mcp generate "minimal social card, bold geometric shapes" --format jpeg --width 1200 --height 630 --size-policy cover
```

## Reuse a generated image as a reference

```json
{
  "prompt": "same scene, but in watercolor",
  "references": [{"source": {"type": "resource", "uri": "mcp://codex-images/jobs/<job>/artifacts/0"}}]
}
```

## Fetch an artifact

```json
{"uri": "mcp://codex-images/jobs/<job>/artifacts/0"}
{"uri": "mcp://codex-images/jobs/<job>/artifacts/0", "delivery": "download"}
```
