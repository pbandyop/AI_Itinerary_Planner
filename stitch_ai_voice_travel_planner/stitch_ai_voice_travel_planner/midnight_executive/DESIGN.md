---
name: Midnight Executive
colors:
  surface: '#f8f9ff'
  surface-dim: '#cbdbf5'
  surface-bright: '#f8f9ff'
  surface-container-lowest: '#ffffff'
  surface-container-low: '#eff4ff'
  surface-container: '#e5eeff'
  surface-container-high: '#dce9ff'
  surface-container-highest: '#d3e4fe'
  on-surface: '#0b1c30'
  on-surface-variant: '#44474e'
  inverse-surface: '#213145'
  inverse-on-surface: '#eaf1ff'
  outline: '#75777f'
  outline-variant: '#c5c6cf'
  surface-tint: '#4e5e81'
  primary: '#031635'
  on-primary: '#ffffff'
  primary-container: '#1a2b4b'
  on-primary-container: '#8293b8'
  inverse-primary: '#b6c6ef'
  secondary: '#4d5e81'
  on-secondary: '#ffffff'
  secondary-container: '#c6d7ff'
  on-secondary-container: '#4c5d7f'
  tertiary: '#11181d'
  on-tertiary: '#ffffff'
  tertiary-container: '#262c32'
  on-tertiary-container: '#8d939b'
  error: '#ba1a1a'
  on-error: '#ffffff'
  error-container: '#ffdad6'
  on-error-container: '#93000a'
  primary-fixed: '#d8e2ff'
  primary-fixed-dim: '#b6c6ef'
  on-primary-fixed: '#081b3a'
  on-primary-fixed-variant: '#364768'
  secondary-fixed: '#d7e2ff'
  secondary-fixed-dim: '#b5c7ee'
  on-secondary-fixed: '#071b3a'
  on-secondary-fixed-variant: '#364768'
  tertiary-fixed: '#dde3eb'
  tertiary-fixed-dim: '#c1c7cf'
  on-tertiary-fixed: '#161c22'
  on-tertiary-fixed-variant: '#41474e'
  background: '#f8f9ff'
  on-background: '#0b1c30'
  surface-variant: '#d3e4fe'
typography:
  display-lg:
    fontFamily: manrope
    fontSize: 48px
    fontWeight: '700'
    lineHeight: 56px
    letterSpacing: -0.02em
  headline-lg:
    fontFamily: manrope
    fontSize: 32px
    fontWeight: '600'
    lineHeight: 40px
    letterSpacing: -0.01em
  headline-lg-mobile:
    fontFamily: manrope
    fontSize: 24px
    fontWeight: '600'
    lineHeight: 32px
  body-md:
    fontFamily: inter
    fontSize: 16px
    fontWeight: '400'
    lineHeight: 24px
  body-sm:
    fontFamily: inter
    fontSize: 14px
    fontWeight: '400'
    lineHeight: 20px
  label-caps:
    fontFamily: jetbrainsMono
    fontSize: 12px
    fontWeight: '500'
    lineHeight: 16px
rounded:
  sm: 0.125rem
  DEFAULT: 0.25rem
  md: 0.375rem
  lg: 0.5rem
  xl: 0.75rem
  full: 9999px
spacing:
  base: 4px
  xs: 4px
  sm: 8px
  md: 16px
  lg: 24px
  xl: 48px
  gutter: 24px
  margin-mobile: 16px
  margin-desktop: 64px
  max-width: 1280px
---

## Brand & Style

The design system is built on a foundation of **Modern Corporate** aesthetics with a focus on authority, precision, and clarity. It targets a professional audience that values reliability and streamlined workflows. 

The primary visual driver is a sophisticated contrast between high-density dark tones and expansive, clean light surfaces. The atmosphere is intentional and grounded, avoiding unnecessary decoration in favor of functional elegance. We employ a "Quiet Luxury" approach to UI—where the quality of typography and the rhythm of spacing communicate the brand's premium nature rather than loud colors or complex effects.

## Colors

The palette is anchored by a deep, dark navy blue (#1a2b4b), which serves as the primary driver for all critical brand expressions and interactive states. 

- **Primary:** Used for high-emphasis actions, active states, and brand-defining moments.
- **Secondary:** A mid-tone slate blue used for supporting information and secondary interactions.
- **Neutral:** A range of greys used for borders, subtle text, and non-interactive iconography.
- **Background:** Pure white is maintained for the main canvas to ensure maximum readability and a spacious feel, while off-white surfaces are used to differentiate container levels.

## Typography

This design system utilizes a tiered typographic approach to ensure a clear hierarchy. 

**Manrope** is used for headlines to provide a modern, slightly geometric feel that remains professional. **Inter** is the workhorse for all body copy, chosen for its exceptional legibility and systematic appearance. **JetBrains Mono** is utilized sparingly for labels and metadata to inject a sense of technical precision and "pro-tool" character.

- Use negative letter spacing on larger display sizes to maintain tension.
- Mobile headlines scale down to prevent awkward line breaks while maintaining bold weights.

## Layout & Spacing

The design system follows a **Fixed Grid** philosophy for desktop to maintain a controlled reading experience, transitioning to a **Fluid Grid** for mobile devices.

- **Desktop:** 12-column grid with a 1280px max-width, 24px gutters, and 64px side margins.
- **Tablet:** 8-column grid with 24px gutters and 32px side margins.
- **Mobile:** 4-column grid with 16px gutters and 16px side margins.

A strict 4px baseline grid governs all internal component spacing (padding/margins) to ensure mathematical harmony across the UI.

## Elevation & Depth

Depth in this design system is achieved through **Tonal Layers** and **Low-Contrast Outlines**. We avoid heavy drop shadows in favor of subtle surface color shifts and thin, 1px borders.

- **Level 0 (Background):** Pure white (#ffffff).
- **Level 1 (Surface):** Light grey-blue (#f8fafc) used for cards or sidebars.
- **Level 2 (Interaction):** Subtle 1px borders in #e2e8f0.
- **Elevation Shadow:** When necessary (e.g., dropdowns), use a single, highly diffused shadow: `0 10px 15px -3px rgba(26, 43, 75, 0.05)`. The shadow color is tinted with the primary Navy Blue to maintain color harmony.

## Shapes

The shape language is **Soft** and restrained. We use a 0.25rem (4px) corner radius for standard components like buttons and inputs. This provides a modern touch without appearing too consumer-oriented or playful. Larger containers like cards may scale up to 0.5rem (8px) to soften the overall layout.

## Components

### Buttons
- **Primary:** Solid #1a2b4b background with white text. No gradient. 
- **Secondary:** Transparent background with a #1a2b4b 1px border and text.
- **States:** On hover, primary buttons darken slightly; secondary buttons gain a very light navy tint (#1a2b4b at 5% opacity).

### Input Fields
- **Default:** White background, 1px border in #e2e8f0.
- **Focus:** Border changes to #1a2b4b with a subtle 2px outer glow in the same color at 10% opacity.

### Chips & Tags
- Used for categorization. Small, height of 24px, using the `label-caps` typography. Backgrounds should be very light versions of the primary color.

### Cards
- White background with a 1px #e2e8f0 border. No shadow by default. Sharp vertical stacking for list-style data.

### Lists
- High-density layouts with subtle dividers. Hover states on list items should use #f8fafc to indicate interactivity without visual clutter.