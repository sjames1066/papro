# PAPro 1.1.2

- Landing PA is built for the airport nearest to where you touch down (within 30 km); the planned airport's terminal choice is used if you land there.
- Diversion to an airport with several recordings: the plain / _GEN recording is used; you are asked only if there is neither.
- PAPro always asks which recording to use when an airport has more than one. "Remember my choice" and "Forget terminal choices" are removed.
- A flight filed in SimBrief while you are airborne is ignored until you are parked with the engines off; PAPro then fetches the new plan.
- Boarding music: all tracks are merged into one BoardingMusic.ogg. Non-Fenix aircraft loop it from the start of boarding until boarding is complete and repeat the boarding PA every 3-7 minutes.
- Build EXE.bat now bundles airport_coords.json.
- **Crew introduction (Pack Studio).** The pre-safety briefing can be built from pieces: intro parts, the number of flight attendants (set per aircraft type and said with the clock's hour clips), a base (the departure airport if recorded, else random) and random crew names, in an order you choose. Includes a Random name button (50/50 male/female) and a new *Crew introduction settings* card.
