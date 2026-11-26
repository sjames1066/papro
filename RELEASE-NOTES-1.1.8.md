# PAPro 1.1.8

## New
- **Aircraft versions (plane icon) on every announcement line** in Pack Studio, so any PA can have a take for particular aircraft types.
- **Unrecorded only** filter for Airports in Pack Studio.
- **Flight complete** phase when parked and shut down after the flight.
- **Descent started PA timing** for aircraft PAPro plays for: with a readable seat belt sign it plays when the sign goes ON (at 10,000 ft AGL at the latest); with no readable sign it plays through 18,000 ft AGL.
- Boarding welcome rows renamed "Welcome, morning / afternoon / evening".

## Fixes
- Reloading on the ground with both engines running at the departure airport now shows Taxi out (near the destination: Taxi in).
- The phase now works out correctly on FSLabs and Fenix when PAPro is opened part way through a flight (it stayed on Preflight).
- Taxi out is recognised after a pushback at 2 kt forward, not only 5 kt. Phase changes are now written to the log.
- Time of day for greetings now follows the sim's local time, not the PC clock.
- PAPro asks which airport recording to use once per flight, not on every apply.
