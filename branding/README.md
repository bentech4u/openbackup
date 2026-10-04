# Branding

`OpenBackup-Logo.png` is the source artwork. The web UI uses versions
derived from it in `frontend/public/`:

| File | Use |
|---|---|
| `logo.png` | Full logo with a transparent background, for dark surfaces (sidebar, dark login) |
| `logo-light.png` | Same with the white lettering in dark navy, for light surfaces |
| `logo-mark.png`, `favicon.png` | The icon alone |
| `apple-touch-icon.png` | Home-screen icon on the brand background (#0E1726) |

They were made by removing the background colour (#0E1726) with
colour-to-alpha, cropping, and resizing to 2x their display size.
