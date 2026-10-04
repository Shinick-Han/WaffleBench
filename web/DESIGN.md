---
name: Falsify Lab synthetic inspection extension
description: Dense wafer evidence inspection within the accepted workbench world
colors:
  primary: "#276449"
  failure: "#963f34"
  inconclusive: "#916414"
  ink: "#202b28"
  muted: "#596860"
  paper: "#f3f5f0"
  surface: "#ffffff"
  line: "#dce3da"
typography:
  headline:
    fontFamily: '"Segoe UI", "Apple SD Gothic Neo", "Malgun Gothic", sans-serif'
    fontSize: "29px"
    fontWeight: 650
    lineHeight: 1.25
  body:
    fontFamily: '"Segoe UI", "Apple SD Gothic Neo", "Malgun Gothic", sans-serif'
    fontSize: "14px"
    lineHeight: 1.55
rounded:
  control: "5px"
components:
  button-selected:
    backgroundColor: "{colors.primary}"
    textColor: "{colors.surface}"
    height: "44px"
  button-default:
    backgroundColor: "{colors.surface}"
    textColor: "{colors.ink}"
    height: "44px"
---

## Overview

**Creative North Star: "Scientific atlas workstation"**

This document covers only `synthetic-wafers.html/css/js`. It extends the accepted
workbench identity with an inspection map, a detail column and a flat die table.
The original research application and mockup keep their existing design rules.

**Key Characteristics:**

- A physical-scale dense wafer is the main data surface.
- Korean copy distinguishes generated truth, simulated detection and observed bins.
- Native controls and semantic tables support an alternative to tiny map targets.

## Colors

Primary green carries actions and passing bins. Red carries failure and generator
defect labels; amber marks missing-evidence bins. Restrained multicolor film
gradients occur only inside the wafer. Passing fills are partially transparent.
All classifications are also available as text in details and table rows.

**The Evidence Label Rule.** Color is a projection of stored data, not a claim of
physical manufacture or a unique root cause.

## Typography

The incumbent Korean system sans is retained. Table and metadata text uses tabular
numerals. Measurement precision varies by unit, while the JSON preserves raw values.
Titles stay compact and controls readable; no decorative display typography is added.

## Layout

Desktop uses a large wafer to the left and die details to the right. At 850px the
columns stack; at 600px the filters and downloads stack. The die table scrolls inside
its labeled container, without document overflow. Tested widths are 360 and 1440px.

## Elevation & Depth

Flat section boundaries and table hairlines separate content. There are no floating
metric cards or decorative shadows. Reduced-motion mode disables smooth scrolling.

## Shapes

The wafer uses exact data-coordinate rectangles and a bottom notch. Ordinary controls
use modest corners and 44px minimum height. Selected dies use a dual map outline.

## Components

Wafer selection uses pressed native buttons. Filters use labeled native selects.
Canvas arrow navigation has a textual selected-die description and Enter moves to
details; each paginated table row has a full-sized selection button. Downloads include
an exact-byte JSON export and a deterministic raw-data archive. Catalog entries retain
source links, and loading failures expose a retry button.

**The Oracle Boundary Rule.** Generated labels are visibly named as such. Blinded
agent/model inputs come from `observed-only.json`, not the joined viewer export.

## Do's and Don'ts

- Do keep raw source values and generated assumptions explicit.
- Do retain the table and keyboard alternative to physically small dies.
- Do keep rainbow material limited to the wafer.
- Don't import these examples into the frozen PVT benchmark.
- Don't promote three generated wafers into an actual yield or generalization claim.
