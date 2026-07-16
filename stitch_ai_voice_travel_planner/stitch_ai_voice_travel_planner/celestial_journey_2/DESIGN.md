---
name: Celestial Journey
colors:
  surface: '#10141a'
  surface-dim: '#10141a'
  surface-bright: '#353940'
  surface-container-lowest: '#0a0e14'
  surface-container-low: '#181c22'
  surface-container: '#1c2026'
  surface-container-high: '#262a31'
  surface-container-highest: '#31353c'
  on-surface: '#dfe2eb'
  on-surface-variant: '#c0c7d4'
  inverse-surface: '#dfe2eb'
  inverse-on-surface: '#2d3137'
  outline: '#8b919d'
  outline-variant: '#414752'
  surface-tint: '#a2c9ff'
  primary: '#a2c9ff'
  on-primary: '#00315c'
  primary-container: '#58a6ff'
  on-primary-container: '#003a6b'
  inverse-primary: '#0060aa'
  secondary: '#67df70'
  on-secondary: '#00390d'
  secondary-container: '#27a640'
  on-secondary-container: '#00320a'
  tertiary: '#bfc7d3'
  on-tertiary: '#29313a'
  tertiary-container: '#9ca4af'
  on-tertiary-container: '#323a44'
  error: '#ffb4ab'
  on-error: '#690005'
  error-container: '#93000a'
  on-error-container: '#ffdad6'
  primary-fixed: '#d3e4ff'
  primary-fixed-dim: '#a2c9ff'
  on-primary-fixed: '#001c38'
  on-primary-fixed-variant: '#004882'
  secondary-fixed: '#83fc89'
  secondary-fixed-dim: '#67df70'
  on-secondary-fixed: '#002105'
  on-secondary-fixed-variant: '#005317'
  tertiary-fixed: '#dbe3ef'
  tertiary-fixed-dim: '#bfc7d3'
  on-tertiary-fixed: '#141c25'
  on-tertiary-fixed-variant: '#404751'
  background: '#10141a'
  on-background: '#dfe2eb'
  surface-variant: '#31353c'
typography:
  headline-lg:
    fontFamily: Manrope
    fontSize: 32px
    fontWeight: '700'
    lineHeight: 40px
    letterSpacing: -0.02em
  headline-md:
    fontFamily: Manrope
    fontSize: 24px
    fontWeight: '600'
    lineHeight: 32px
    letterSpacing: -0.01em
  headline-sm:
    fontFamily: Manrope
    fontSize: 20px
    fontWeight: '600'
    lineHeight: 28px
  body-lg:
    fontFamily: Inter
    fontSize: 18px
    fontWeight: '400'
    lineHeight: 28px
  body-md:
    fontFamily: Inter
    fontSize: 16px
    fontWeight: '400'
    lineHeight: 24px
  body-sm:
    fontFamily: Inter
    fontSize: 14px
    fontWeight: '400'
    lineHeight: 20px
  label-md:
    fontFamily: Inter
    fontSize: 12px
    fontWeight: '600'
    lineHeight: 16px
    letterSpacing: 0.05em
  label-sm:
    fontFamily: Inter
    fontSize: 11px
    fontWeight: '500'
    lineHeight: 14px
rounded:
  sm: 0.25rem
  DEFAULT: 0.5rem
  md: 0.75rem
  lg: 1rem
  xl: 1.5rem
  full: 9999px
spacing:
  base: 4px
  xs: 8px
  sm: 12px
  md: 16px
  lg: 24px
  xl: 32px
  gutter: 20px
  margin: 24px
---

## Brand & Style
The brand personality is sophisticated, focused, and immersive. It is designed for a premium B2C experience where the user is guided through complex information with ease and clarity. 

The aesthetic follows a **Modern Corporate** style with **Tonal Layering**. It utilizes a deep, nocturnal palette inspired by the vastness of space, creating a focused environment that reduces eye strain and highlights critical content. The UI feels reliable and calm, using subtle depth to organize information rather than aggressive borders or heavy shadows.

## Colors
The color palette is rooted in a deep navy and slate foundation to provide a rich, premium dark mode experience.

- **Primary**: A vibrant celestial blue used for active states, primary actions, and brand highlights.
- **Secondary**: A muted emerald green, reserved for success states and "passed" indicators.
- **Background**: A deep charcoal-navy (`#0D1117`) serves as the base canvas.
- **Surfaces**: We use a tiered approach to depth. Primary containers (cards) use a slightly lighter slate (`#161B22`), while interactive or nested elements use an even lighter tone (`#21262D`).
- **Typography**: Pure white is avoided for long-form text to prevent "halation." Instead, high-contrast light grays are used for readability.

## Typography
The typography system pairs **Manrope** for headlines with **Inter** for UI and body text. 

- **Headlines**: Use Manrope with tighter letter spacing and heavier weights to convey a modern, architectural feel.
- **Body**: Inter provides exceptional legibility in dark environments. 
- **Labels**: Small caps and increased letter spacing are used for metadata and category headers (e.g., "MORNING", "AFTERNOON") to create a clear structural hierarchy without needing large font sizes.

## Layout & Spacing
The layout follows a **fluid grid** model with a focus on vertical rhythm. 

- **Grid**: 12-column system for desktop, collapsing to 4 columns on mobile.
- **Gutters**: A consistent 20px gutter ensures clear separation between content cards.
- **Grouping**: Elements within a card should use the `sm` (12px) or `md` (16px) spacing units, while major sections use `xl` (32px) to provide "breathing room."
- **Alignment**: Left-aligned content is preferred to maintain a strong vertical scan line, especially in chat-like or itinerary interfaces.

## Elevation & Depth
In this design system, depth is communicated through **Tonal Layering** rather than shadows. 

1. **Canvas**: The lowest level, uses the darkest neutral color.
2. **Surface**: Main content containers sit on top of the canvas with a subtle contrast increase.
3. **Overlay**: Interactive elements, like buttons or active chat bubbles, use the highest contrast or the primary accent color.
4. **Borders**: Use ultra-low-opacity light borders (10% white) to define shapes without creating visual noise. This "ghost border" technique replaces shadows for a cleaner, more modern look.

## Shapes
The shape language is defined by **Rounded (8px)** corners. This balance avoids the rigidity of sharp corners while maintaining a professional, structured appearance compared to pill-shaped designs.

- **Small Components**: Checkboxes and small buttons use the base `rounded` (0.5rem / 8px).
- **Cards**: Large containers use `rounded-lg` (1rem / 16px) to soften the overall layout.
- **Chat Bubbles**: Follow the `rounded-lg` scale to feel approachable.

## Components

- **Buttons**: Primary buttons are solid `primary_color_hex` with white text. Secondary buttons are outlined with a subtle 1px border.
- **Cards**: Use `surface_primary` with an 8px or 16px border radius. For nested items (like itinerary stops), use `surface_secondary`.
- **Input Fields**: Dark backgrounds with a 1px border that glows with the primary color when focused.
- **Chips/Badges**: Small, low-contrast pills (e.g., "Sightseeing") with muted backgrounds and primary-colored text.
- **Lists**: Use subtle dividers (1px, 10% opacity) or simply rely on the `md` spacing unit to separate items.
- **Timeline Indicators**: Use thin vertical lines and circles in the primary color to connect chronological events, as seen in itinerary views.