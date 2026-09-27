# Public document samples

These files are not synthetic. They are publicly available documents used to demonstrate the workbench on
material nobody at this project produced. Each entry records where the file came from, who made it, the
licence it carries and any change made to it. The generated fixtures are described in `scripts/make_fixtures.py`.

## nasa_corrosion_risk.pdf

- **Title:** A Corrosion Risk Assessment Model for Underground Piping
- **Author:** Koushik Datta and Douglas R. Fraser, NASA Ames Research Center
- **Licence:** Public domain (work of the United States Government, 17 U.S.C. section 105)
- **Source:** https://ntrs.nasa.gov/citations/20090035820
- **Retrieved:** 2026-09-18
- **Size:** 625 KB
- **SHA-256:** `d9549ab9fee1d2c9e596d0c9554e157319660dd90d34f4934f64d78d138733e8`
- **Change made:** Downloaded unchanged. Five pages with a text layer, so it is read directly rather than through OCR.

A scanned P&ID sample used to sit here (`omre_pid_scan.jpg`, a real Organic Moderated Reactor
Experiment drawing). It has been replaced by synthetic scanned sheets generated in
`scripts/fixture_images.py` (`PID-CW-003_scan.jpg`, `PID-AM-002_scan.jpg`), which carry the sheet's
own tags rather than a reactor that has nothing to do with this project, and come with an OCR
sidecar the tag detector can be tested against.
