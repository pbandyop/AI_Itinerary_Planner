---
name: Celestial Journey
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
  on-surface-variant: '#414755'
  inverse-surface: '#213145'
  inverse-on-surface: '#eaf1ff'
  outline: '#717786'
  outline-variant: '#c1c6d7'
  surface-tint: '#005bc1'
  primary: '#0058bc'
  on-primary: '#ffffff'
  primary-container: '#0070eb'
  on-primary-container: '#fefcff'
  inverse-primary: '#adc6ff'
  secondary: '#006a66'
  on-secondary: '#ffffff'
  secondary-container: '#61f5ed'
  on-secondary-container: '#006f6a'
  tertiary: '#555d63'
  on-tertiary: '#ffffff'
  tertiary-container: '#6e757c'
  on-tertiary-container: '#fcfcff'
  error: '#ba1a1a'
  on-error: '#ffffff'
  error-container: '#ffdad6'
  on-error-container: '#93000a'
  primary-fixed: '#d8e2ff'
  primary-fixed-dim: '#adc6ff'
  on-primary-fixed: '#001a41'
  on-primary-fixed-variant: '#004493'
  secondary-fixed: '#65f8f0'
  secondary-fixed-dim: '#3fdbd4'
  on-secondary-fixed: '#00201e'
  on-secondary-fixed-variant: '#00504d'
  tertiary-fixed: '#dce3eb'
  tertiary-fixed-dim: '#c0c7cf'
  on-tertiary-fixed: '#151c22'
  on-tertiary-fixed-variant: '#40484e'
  background: '#f8f9ff'
  on-background: '#0b1c30'
  surface-variant: '#d3e4fe'
typography:
  display-lg:
    fontFamily: Plus Jakarta Sans
    fontSize: 48px
    fontWeight: '700'
    lineHeight: '1.1'
    letterSpacing: -0.02em
  headline-lg:
    fontFamily: Plus Jakarta Sans
    fontSize: 32px
    fontWeight: '700'
    lineHeight: '1.2'
    letterSpacing: -0.01em
  headline-lg-mobile:
    fontFamily: Plus Jakarta Sans
    fontSize: 24px
    fontWeight: '700'
    lineHeight: '1.2'
  title-md:
    fontFamily: Plus Jakarta Sans
    fontSize: 20px
    fontWeight: '600'
    lineHeight: '1.4'
  body-lg:
    fontFamily: Plus Jakarta Sans
    fontSize: 18px
    fontWeight: '400'
    lineHeight: '1.6'
  body-md:
    fontFamily: Plus Jakarta Sans
    fontSize: 16px
    fontWeight: '400'
    lineHeight: '1.6'
  label-sm:
    fontFamily: Plus Jakarta Sans
    fontSize: 14px
    fontWeight: '600'
    lineHeight: '1.2'
    letterSpacing: 0.01em
  label-xs:
    fontFamily: Plus Jakarta Sans
    fontSize: 12px
    fontWeight: '500'
    lineHeight: '1.2'
rounded:
  sm: 0.25rem
  DEFAULT: 0.5rem
  md: 0.75rem
  lg: 1rem
  xl: 1.5rem
  full: 9999px
spacing:
  unit: 8px
  container-padding-mobile: 1.25rem
  container-padding-desktop: 2.5rem
  gutter: 1.5rem
  section-gap: 4rem
  itinerary-step-gap: 1rem
---

## Brand & Style
The design system is built for a modern B2C AI travel companion, emphasizing clarity, intelligence, and tranquility. The brand personality is "The Expert Friend"—knowledgeable enough to handle complex logistics but approachable enough to plan a weekend getaway.

The aesthetic leans into **Minimalism** with a heavy emphasis on whitespace to reduce cognitive load during the travel planning process. It utilizes a "Mobile-First, Desktop-Refined" philosophy, ensuring that touch-friendly targets and legible information hierarchy translate seamlessly across devices. The emotional goal is to evoke a sense of "planned spontaneity," where the AI handles the friction, leaving the user with a clean, airy interface that feels like a fresh start to a journey.

## Colors
The palette is anchored by a high-trust **Primary Blue**, used for critical actions and brand presence. A **Secondary Teal** is used for AI-driven suggestions and "smart" highlights, providing a refreshing contrast that feels modern and energetic. 

The background is strictly white (#FFFFFF) to maintain an airy feel, while a very light **Tertiary Blue** tint is used for container backgrounds to subtly group related information. Functional colors include a soft slate neutral for secondary text and a specific "Time to Spend" highlight—a soft sky-blue wash—that draws the eye to duration-based data without adding visual clutter.

## Typography
This design system utilizes **Plus Jakarta Sans** across all levels. Its soft, rounded terminals and modern geometric construction provide the perfect balance between professional reliability and a friendly, welcoming tone.

Headline levels use tighter tracking and heavier weights to create strong visual anchors. Body text is optimized for readability with a generous line height (1.6) to ensure long itineraries are easy to scan. Mobile-specific overrides are applied to the largest headlines to prevent awkward text wrapping on narrow viewports.

## Layout & Spacing
The layout follows a **Fluid Grid** model with a maximum content width of 1280px for desktop. It uses an 8px base unit for all spatial relationships, ensuring a consistent rhythm. 

- **Mobile:** 4-column grid with 20px side margins. Content is mostly single-column stack.
- **Desktop:** 12-column grid with 40px side margins. Sidebars and itinerary maps utilize sticky positioning to keep context visible during vertical scrolling.
- **Rhythm:** Generous vertical spacing between sections (64px+) is mandatory to maintain the "airy" brand promise. Elements within a single travel card use tighter spacing (8px-16px) to maintain proximity.

## Elevation & Depth
The design system avoids heavy shadows and dark borders. Instead, it uses **Ambient Shadows** and **Tonal Layers** to create depth.

- **Level 0 (Base):** Pure white background.
- **Level 1 (Cards):** Use a very soft, highly diffused shadow (e.g., 0px 4px 20px rgba(0, 0, 0, 0.04)) with a 1px stroke in a very light grey (#F1F5F9).
- **Level 2 (Active/Hover):** Increased shadow spread (0px 10px 30px rgba(0, 0, 0, 0.08)) to indicate interactivity.
- **Modals:** Significant backdrop blur (12px) to keep the user focused on the AI interaction while maintaining the airy, translucent feel.

## Shapes
The shape language is defined by **large, friendly radii**. Most primary containers and cards use a 24px (1.5rem) corner radius to evoke a sense of softness and safety. 

Buttons are fully pill-shaped (rounded-full) to provide a distinct interactive contrast against the squircle-shaped cards. Input fields and smaller UI components use a slightly tighter 12px radius to maintain structural integrity while remaining consistent with the overall soft-edged aesthetic.

## Components

### Buttons & Inputs
- **Primary Action:** Pill-shaped, Primary Blue background, white text. No border.
- **AI Toggle:** Secondary Teal with a soft glow effect (low-opacity teal shadow).
- **Inputs:** White background with a subtle 1px border. On focus, the border transitions to Primary Blue with a 4px soft outer glow.

### Travel-Specific Components
- **Time to Spend Badge:** A small, rounded tag using the `time-spend-highlight` background. It features a clock icon and bolded text (e.g., "3 hours").
- **Travel Time Connector:** A vertical or horizontal **dashed line** in `travel-path-connector` color. It connects two location nodes, with a centered icon (plane, train, car) to indicate the mode of transport.
- **Itinerary Card:** Uses the 24px radius and Level 1 elevation. Includes a high-quality image with a subtle gradient overlay at the bottom for text legibility.

### Selection & Feedback
- **Chips:** Used for "Interests" or "Vibe" selection. Unselected: Light grey border. Selected: Primary Blue background or thick Primary Blue border.
- **Progress Indicators:** Soft, thin lines that pulse with the Secondary Teal color when the AI is generating results.