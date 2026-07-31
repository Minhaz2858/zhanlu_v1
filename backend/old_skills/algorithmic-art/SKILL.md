---
name: algorithmic-art
description: Creating algorithmic art using p5.js with seeded randomness and interactive parameter exploration. Use this when users request creating art using code, generative art, algorithmic art, flow fields, or particle systems.
---

# Algorithmic Art Skill

## Overview

Create generative art that expresses computational aesthetics through p5.js. This is a two-step process:
1. **Algorithmic Philosophy** — write a .md manifesto describing the generative movement
2. **Code Expression** — create .html + .js files implementing the philosophy

## Step 1: Algorithmic Philosophy

Name the movement (1-2 words) and articulate 4-6 paragraphs covering:
- Computational processes, emergent behavior, mathematical beauty
- Seeded randomness, noise fields, organic systems
- Particles, flows, fields, forces
- Parametric variation and controlled chaos

Emphasize craftsmanship: the algorithm should appear meticulously refined, as though by someone at the absolute top of their field.

## Step 2: Code Expression

Create p5.js sketches that are 90% algorithmic generation, 10% essential parameters.

### Implementation Requirements

- Use `let seed;` at the top of each sketch
- Capture seed from URL params: `seed = parseFloat(getURLParams().seed) || Math.floor(Math.random() * 10000);`
- Pass seed to all random functions: `random(seed, ...)` where available, or re-seed via `randomSeed(seed)` + `noiseSeed(seed)`
- Single HTML file with inline CSS + JS for portability
- Include at minimum: seed display, shuffle button, optional parameters panel
- Include instructions for remixing: "Adjust SEED for variations. Click SHUFFLE for a new seed."
- Only use CDN for p5.js library

### File Structure

Output three files:
1. `philosophy.md` — the algorithmic manifesto
2. `index.html` — standalone interactive viewer (loads p5.js from CDN)
3. `sketch.js` — the generative algorithm (can be inline in HTML)

### Anti-Patterns to Avoid

- No static images or templates masquerading as generative art
- No copying existing artists' work
- No over-engineered parameter UIs that distract from the generative experience
