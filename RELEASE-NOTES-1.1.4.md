# PAPro 1.1.4

## FSLabs A32X
- **Works with FSLabs.** PAPro detects the FSLabs A319/A320/A321 and swaps its PAs into the aircraft's own airline pack (FSLabs decides when each one plays). The airline pack is chosen from the livery loaded in the sim.
- **Safety briefing intro** goes into the folder of the aircraft type you are flying (A319, A320, A321, and the neo variants).
- **Boarding music, descent, turbulence and deboarding PAs** are linked too.
- **Originals are backed up** (Fenix and FSLabs) the first time PAPro changes a pack. Settings, Restore original packs puts them back.
- Every file PAPro writes is listed in the log.

## Dashboard
- **Waiting for simulator** / aircraft detected indicator. Hover a supported aircraft for "Native in-sim playback supported".
- **Seat belt and no smoking signs** light up with the sim's switches (supported aircraft).
- **Reset** button (under Refresh) sends the phase back to Preflight, with a confirmation.
- **Log** button at the bottom.

## Flight phase and PAs
- Parked at the departure airport is always Preflight, never Taxi in / Deboarding.
- "Arm doors" waits until the doors are closed.
- Other aircraft: the turbulence PA plays when the seat belt sign is switched on in cruise (not while descending), including iFly, iniBuilds and others where the sign can be read.
- A new neo variant uses the matching A320/A321 files.

## Fixes
- Reconnects to MSFS when the sim was still loading at start-up.
- An aircraft with no name no longer leaves the dashboard stuck on "Waiting for simulator".
