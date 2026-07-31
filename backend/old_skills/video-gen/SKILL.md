---
name: video-gen
description: >
  Generate videos from text descriptions using MiniMax Hailuo video models.
  Use when the user wants to create video clips, animations, or visual
  scenes from a description. Supports async (return task_id) and sync
  (wait for result) modes. Video generation takes 1-5 minutes.
metadata:
  hermes:
    tags: [video, generation, AI, animation, creative]
summary: >
  Generate videos from text using MiniMax Hailuo-2.3. Async by default,
  returns downloadable URL when complete. Describe the scene, action,
  and style in detail. Takes 1-5 minutes per video.
---
# Video Generation Skill

Generate videos from text descriptions using MiniMax's Hailuo video models.

## Quick Start

```
mm_video_gen(prompt="A drone flying over a tropical beach at sunrise",
             model="MiniMax-Hailuo-2.3-Fast", wait=True)
```

## Models

| Model | Speed | Quality | Best For |
|-------|-------|---------|----------|
| `MiniMax-Hailuo-2.3` | Normal (~3 min) | Best | Professional scenes |
| `MiniMax-Hailuo-2.3-Fast` | Fast (~1 min) | Good | Quick previews |

## Prompt Tips

1. **Describe motion**: "A ballerina spinning gracefully" > "A ballerina"
2. **Set the scene**: Include time of day, weather, location
3. **Camera**: "Slow motion", "tracking shot", "close-up", "aerial view"
4. **Duration**: Videos are typically 5-6 seconds

## Async Workflow

For batch generation, use `wait=False` and poll status:

```
# Start generation
result = mm_video_gen(prompt="Ocean waves", wait=False)
task_id = result["task_id"]

# The task keeps generating on the server.
# You can report the task_id to the user for later retrieval.
```

## Limitations

- Cannot generate videos longer than ~6 seconds
- No real-person video generation
- No audio track — video only
- First frame image input not yet available in API
