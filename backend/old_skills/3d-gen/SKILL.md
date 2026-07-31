---
name: 3d-gen
description: >
  Generate 3D model descriptions and reference views from text or images.
  Full 3D model generation (Meshy/Tripo/Luma integration) is coming soon.
  For now, generates high-quality reference images that external 3D tools
  can use as input. Triggers on requests for 3D models, 3D assets, 3D
  printing files, or game-ready models.
metadata:
  hermes:
    tags: [3d, model, generation, mesh, asset]
summary: >
  3D model reference generation from text/images. Full 3D API integration
  coming soon. For now, creates reference views that can be piped to
  external 3D tools like Meshy or Tripo.
---
# 3D Generation Skill

Generate 3D model references from text descriptions or reference images.

## Current Status: Reference Generation

Full 3D model generation is not yet available natively. Use this workflow:

1. Use `mm_image_gen` to create a clean reference image with `prompt="3D render, front view of [object], clean white background, orthographic view"`
2. Generate top/side/front views for complete coverage
3. Feed reference images to an external 3D service (Meshy, Tripo, Luma AI)

## When Full 3D is Available

The `mm_3d_gen` tool will support:
- Text-to-3D: `mm_3d_gen(description="A wooden treasure chest with brass fittings")`
- Image-to-3D: `mm_3d_gen(reference_image="https://...")`
- Output formats: GLB, OBJ, FBX

## Pro-Tip

For the best results today, be very specific about the object you want modeled,
and generate multiple orthographic views (front, side, top). This approach
works well with all major 3D generation services.
