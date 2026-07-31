---
name: image-gen
description: >
  Generate images from text descriptions using MiniMax image-01 model.
  Use when the user wants to create images, illustrations, concept art,
  or visual content from a description. Supports aspect ratio and batch
  generation. For DALL-E/OpenAI images, use the `image_generation` tool.
metadata:
  hermes:
    tags: [image, generation, AI, creative, visual]
summary: >
  Generate images from text prompts using MiniMax image-01. Supports
  custom aspect ratios (1:1, 16:9, etc.) and batch generation (1-9 images).
  Use `mm_image_gen` tool with a detailed prompt.
---
# Image Generation Skill

Generate AI images from text descriptions using MiniMax's image-01 model.

## Quick Start

Use the `mm_image_gen` tool with a detailed prompt:

```
mm_image_gen(prompt="A majestic dragon flying over a mountain lake at sunset",
             aspect_ratio="16:9", n=1)
```

## Prompt Engineering Tips

1. **Be specific**: "A golden retriever puppy sitting in a wicker basket" > "A dog"
2. **Include style**: Add terms like "oil painting", "photorealistic", "anime", "watercolor"
3. **Describe lighting**: "Golden hour", "cinematic lighting", "soft studio light"
4. **Composition**: "Close-up shot", "wide angle", "bird's eye view"
5. **Mood**: "Energetic", "serene", "mysterious", "whimsical"

## Aspect Ratios

| Use Case | Ratio |
|----------|-------|
| Social media (Instagram) | 1:1 |
| Widescreen / video thumbnails | 16:9 |
| Portrait / Story | 9:16 |
| Standard photo | 4:3 |
| Tall portrait | 3:4 |

## Limitations

- Cannot generate images of real people (celebrities, politicians)
- Cannot create NSFW or violent content
- Resolution capped at model limits
- No inpainting or editing — use `mm_effects` for style transfer
