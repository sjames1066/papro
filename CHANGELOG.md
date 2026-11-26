# Changelog: FCAC 1.0.7 → PAPro 1.1.5

FCAC is now **PAPro**. Fenix A320 still gets its PAs by file swap; other aircraft (PMDG and more) now get in-app playback.

## 1.1.5
### Fixes
- **Install / Update buttons greyed out.** A flight in progress could leave PAPro's update lock held, so pack Install and Update stayed greyed out until you restarted PAPro.
- **Pack Studio: airline name reverted to the code.** Reopening a pack from your library now keeps the airline name it was saved with.
- **Pack Studio: exporting a single sound did nothing** in PAPro's own window. File downloads are now allowed.

### Aircraft
- **FSS E-Jets (E170/E190):** the seat belt sign and the beacon are now read from the aircraft, so the turbulence PA and Boarding complete work.

### Dashboard
- The seat belt and no smoking signs are half the size and sit under the Active switch and Settings cog.

## 1.1.4
### FSLabs A32X
- **Works with FSLabs.** PAPro detects the FSLabs A319/A320/A321 and swaps its PAs into the aircraft's own airline pack (FSLabs decides when each one plays). The airline pack is chosen from the livery loaded in the sim.
- **Safety briefing intro** goes into the folder of the aircraft type you are flying (A319, A320, A321, and the neo variants).
- **Boarding music, descent, turbulence and deboarding PAs** are linked too.
- **Originals are backed up** (Fenix and FSLabs) the first time PAPro changes a pack. Settings, Restore original packs puts them back.
- Every file PAPro writes is listed in the log.

### Dashboard
- **Waiting for simulator** / aircraft detected indicator. Hover a supported aircraft for "Native in-sim playback supported".
- **Seat belt and no smoking signs** light up with the sim's switches (supported aircraft).
- **Reset** button (under Refresh) sends the phase back to Preflight, with a confirmation.
- **Log** button at the bottom.

### Flight phase and PAs
- Parked at the departure airport is always Preflight, never Taxi in / Deboarding.
- "Arm doors" waits until the doors are closed.
- Other aircraft: the turbulence PA plays when the seat belt sign is switched on in cruise (not while descending), including iFly, iniBuilds and others where the sign can be read.
- A new neo variant uses the matching A320/A321 files.

### Fixes
- Reconnects to MSFS when the sim was still loading at start-up.
- An aircraft with no name no longer leaves the dashboard stuck on "Waiting for simulator".

## 1.1.3
### New
- **Community packs.** Pack Manager has a Community list of packs made by other users. Share your own from Pack Studio (Share with the community): the airline, code, character name and voice artist come from Voice details, and you only fill in what is empty and agree that the files are your own work. New packs are reviewed before they appear. Updates to a pack you already had approved go live straight away.
- **Sample.** Community packs have a Sample button that plays 10 seconds of the pack's After Landing PA, built with its first airport and a local time of 3:45 PM.
- **Ratings.** Rate community packs out of 5 stars; the average shows beside the pack name.
- **After Landing parts.** New packs build After Landing from Part 1, Airport, Part 2, Time and Part 3, and more parts can be added.
- **Pack Studio buttons.** Delete (with a red confirm), Open project folder, Upload to Community, Save, Export.
- **Author key.** Settings, Advanced: copy your author key as a backup and restore it on a new PC, or use a transfer code from the reviewer to move a pack to a new PC.
- **Pack Manager.** Your installed packs and the Community list are on one tab, both collapsible, with a search box. The tab now comes before Pack Studio.
- **PAPro's own window.** No more Edge window: PAPro opens in its own window with its own taskbar icon and remembers where you left it. (Needs Microsoft WebView2, built into Windows 11; otherwise the browser window is used.)
- **Skip PA and Replay PA** under the volume knob. Opening PAPro part way through a flight no longer plays a PA: the one for your phase is ready on Replay.

### Aircraft other than the Fenix
- **Audio.** PAs play cleanly at your output device's own sample rate. One sound at a time: boarding music pauses during a PA and resumes where it left off. The boarding music track is random each time PAPro loads. PAPro shows as "PAPro" in the Windows volume mixer.
- **Flight phase.** On opening, PAPro looks at the sim for a few seconds, then shows the right phase (in the air, taxi in, deboarding).
  - Cabin service plays on reaching cruise altitude.
  - After Landing plays once you are off the runway (at most 90 s after touchdown).
  - Disarm doors plays 5 s after all engines are off.
  - The deboarding PA waits for GSX to start deboarding.
  - The phase stays on Deboarding until a different flight plan is loaded, and the welcome never plays after a flight.
- **GSX.** Boarding completed or aborted in GSX triggers Boarding Complete. Manual boarding has a Boarding Complete button. GSX is read automatically; the setting is gone.
- **PMDG 737.** The seat belt sign is read through the PMDG SDK. The seat belt off PA plays only when it can be read (not yet verified in the sim).

### Settings
- Removed: play through PAPro, how the pieces join (Natural flow is used) and the GSX switch (always on).
- Web address, Fenix packs folder and log are under Advanced.

## 1.1.2
- **Landing PA from the nearest airport.** When you touch down, PAPro finds the closest airport (within 30 km) and builds the landing PA for it. If you land at the planned airport, your terminal choice from setup is used.
- **Diversions.** If you divert to an airport with several recordings (e.g. EGLL T3/T5), the plain or `_GEN` recording is used. PAPro only asks if neither exists.
- **Terminal choice is always asked.** When an airport has more than one recording you are asked every time. "Remember my choice" and "Forget terminal choices" are removed.
- **Prefiling the next flight.** A SimBrief plan filed while you are airborne is ignored. PAPro fetches it once you are parked with the engines off.
- **Boarding music.** All tracks are merged into a single `BoardingMusic.ogg` (1.5 s crossfades). Non-Fenix aircraft loop it from the start of boarding until boarding is complete, and the boarding PA repeats every 3–7 minutes. Fenix gets the merged file; whether Fenix loops it is Fenix's own behaviour (unverified).
- Build EXE now bundles `airport_coords.json`.
- **Crew introduction (Pack Studio).** The pre-safety briefing can be built from pieces: intro parts, the number of flight attendants (set per aircraft type and said with the clock's hour clips), a base (the departure airport if recorded, else random) and random crew names, in an order you choose. Includes a Random name button (50/50 male/female) and a new *Crew introduction settings* card.

## 1.1.1
- **EFBx.** The flight plan is read from EFBx (`localhost:35810/efbx/simbrief`) first, falling back to SimBrief.
- **Web page works at startup.** The server starts as soon as Windows launches PAPro in the tray, so the page and EFBx address work without opening the app first.
- **Pack Studio**
  - Skip a PA as *Default sound* or *No sound* (a 1 s silent `.ogg` overrides the default pack's sound).
  - Skipped items are remembered, including airports, variations, bolt-ons and aircraft versions.
  - Airport search/filter.
  - Boarding music volume slider (default 50% of the announcements).
  - New intro text, *Start New Pack* on the left, and the new-pack form appears only after clicking it.
  - Saving no longer bumps the pack version: you set it yourself, and it is stored in `voice.json`.
- **Playback behaviour**
  - Nothing plays while the Pack Studio tab is open. Due PAs are held and play when you return to the Dashboard.
  - Joining a flight part way through no longer plays announcements already behind you.
  - With no pack for the flight's airline, leftover PAs from an earlier flight are cleared so nothing plays.
- **Dashboard.** Route line is `DEP | name → ARR | name` with a magenta arrow and muted departure code. Installed packs show their version.
- **Manage pack.** Removed the local-time checkbox (now in Pack Studio) and *Add a voice*. Removing the last voice uninstalls the pack.
- **Browser / other devices.** PC-only settings are hidden. Network access is always on (toggle removed). No right-click menu, long-press callout, text selection or dragging in the app.
- **Cleaner.** Windows notifications removed (PAPro stays in the background). Credit link removed (support link kept). Shorter tray text, e.g. "PAs loaded for BAW/SHT for EGCC".
- **New app icon**: cabin window with sunset wing, in the tray, header, title bar, EXE and installer.
- Fixes: "vimported" in the install message, duplicate remove buttons on aircraft-version rows, and the extra gap above the Pack Studio heading.

## 1.0.8 – 1.1.0 (changes between the releases)
- **In-app playback** for aircraft PAPro can't swap files for (PMDG and others): PAPro plays the PAs itself on a chosen output device, with the knob volume and mute applied.
- **Flight phases** (Preflight → Boarding → … → Deboarding) move forward only. GSX integration is its own setting, and the Dashboard has *Start boarding* and *Disarm Doors PA* buttons.
- **Languages.** Pack Studio has a required primary language and an optional secondary one, joined with a 2 s pause.
- **Pack Studio**
  - A "+ variation" on every PA and *Check clip volume / Match volume* for imported clips.
  - Variations on greetings, Generic Boarding and refuelling.
  - Section renames: Landing → After Landing, Descent Started, Seatbelt Sign On (Turbulence).
  - Sub-headings for Boarding and After Landing.
  - The Aircraft names section was removed.
- **Local time is a per-pack setting.** It is in Manage and Pack Studio, no longer in general Settings.
- **Test tab.** Green *Go* button (Enter works) and a playback volume slider.
