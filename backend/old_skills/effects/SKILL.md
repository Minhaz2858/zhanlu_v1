---
name: effects
description: >
  Apply visual effects to images: style transfer, upscale, inpaint, and
  outpaint. Use when the user wants to change an image's artistic style,
  enhance resolution/quality, edit specific regions, or extend the canvas.
  Triggers on requests like "make this photo look like Van Gogh", "enhance
  image quality", "remove the background", "extend this image".
metadata:
  hermes:
    tags: [image, effects, style-transfer, upscale, inpaint]
summary: >
  Apply image effects (style transfer, upscale, inpaint, outpaint) using
  MiniMax image-to-image. Provide an image URL and describe the desired
  effect with the `mm_effects` tool.
---
# Image Effects Skill

Apply visual effects to images using MiniMax's image-to-image capabilities.

## Effects

### Style Transfer
Transform an image's artistic style:
```
mm_effects(effect="style_transfer",
           prompt="In the style of Monet's water lilies, soft impressionist brushstrokes",
           image_url="https://example.com/photo.jpg")
```

### Upscale
Enhance image resolution and quality:
```
mm_effects(effect="upscale",
           prompt="Sharpen details, enhance facial features, increase resolution to 4K",
           image_url="https://example.com/photo.jpg")
```

### Inpaint
Edit specific regions of an image:
```
mm_effects(effect="inpaint",
           prompt="Remove the street sign in the background and fill with sky",
           image_url="https://example.com/photo.jpg")
```

### Outpaint
Extend the image canvas outward:
```
mm_effects(effect="outpaint",
           prompt="Extend this landscape to the left and right, continuing the mountain range",
           image_url="https://example.com/photo.jpg")
```

## Tips

1. **Style transfer**: Reference specific artists or movements (e.g., "Studio Ghibli", "cyberpunk", "watercolor")
2. **Upscale**: Mention the target resolution and specific details to enhance
3. **Inpaint**: Be precise about which regions to edit
4. **Outpaint**: Describe what should appear in the new canvas areas
