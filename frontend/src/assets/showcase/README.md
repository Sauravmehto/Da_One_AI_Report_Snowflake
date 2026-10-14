# Login page showcase

Screenshots rotated by the carousel on `/login`.

Drop image files here and they appear — no code change. `showcase-carousel.tsx`
globs this folder at build time.

- **Order** is filename order. Prefix with digits: `01-…`, `02-…`.
- **Caption** comes from the filename: `02-fraud-risk.png` → "Fraud risk".
- **Aspect** the frame is 16:10 and crops from the top. Capture at 1600×1000
  (or any 16:10 size) so nothing important is cut.
- **Format** png, jpg, webp or avif.

These images are served publicly to anyone who can reach the login page —
before adding one, check the dashboard it shows contains no real client data.
